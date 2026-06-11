"""Recorder — orchestrates writer, transcriber, describer.

The Recorder owns the per-process state (writer + lazy-init'd
transcriber and describer) and provides the single callback Hermes
binds into:

* :meth:`Recorder.on_pre_gateway_dispatch` — sync callback wired to the
  ``pre_gateway_dispatch`` plugin hook. Records every inbound gateway
  message (any platform) to the vault. For voice/image events it
  transcribes / describes the media and rewrites ``event.text`` so the
  agent has usable content. It does NOT make wake decisions — whether
  the agent replies is governed by Hermes's native settings (mention
  gating, allowed users, etc.).

On the first ``pre_gateway_dispatch`` the recorder also fires its
one-shot ``wire_gateway_once`` callback. That binding is how the
plugin reaches the live platform adapters (to wrap ``send`` for
outbound recording) — Hermes's ``on_session_start`` hook doesn't
receive the gateway, so adapter wiring can't happen there. See
``docs/DESIGN.md`` for the storage format and concurrency model.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from hermes_chat_recorder.config import RecorderConfig
from hermes_chat_recorder.describer import ImageDescriber, ImageDescriberError
from hermes_chat_recorder.events import EventInfo
from hermes_chat_recorder.events import extract as extract_event
from hermes_chat_recorder.name_resolver import NameResolver, looks_like_id
from hermes_chat_recorder.transcriber import Transcriber, TranscriberError
from hermes_chat_recorder.types import Section
from hermes_chat_recorder.writer import VaultWriter

logger = logging.getLogger(__name__)


# Type alias for the media-download callable. plugin.py binds this to
# the live Matrix adapter's media-download method as a legacy fallback;
# the primary media source is the adapter-cached local file on the
# event itself. None means "no fallback available".
DownloadMedia = Callable[[str], bytes]


class Recorder:
    """Records every gateway message to the vault.

    The recorder is intentionally NOT a gate. It writes everything it
    sees and lets Hermes's native settings decide whether the agent
    wakes. For voice/image events it rewrites ``event.text`` to the
    transcript / description so the agent has usable content if it does
    wake.
    """

    def __init__(
        self,
        *,
        config: RecorderConfig,
        writer: VaultWriter,
        resolver: NameResolver | None = None,
        transcriber: Transcriber | None = None,
        describer: ImageDescriber | None = None,
        bot_mxid: str = "",
        download_media: DownloadMedia | None = None,
        wire_gateway_once: Callable[[Any], None] | None = None,
    ) -> None:
        self.config = config
        self.writer = writer
        # Resolver defaults to a bare instance with no Matrix lookups
        # wired — per-event hints and ID-derived fallbacks still work.
        # The gateway-wiring callback (below) plumbs the live Matrix
        # client through on the first pre_gateway_dispatch.
        self.resolver = resolver if resolver is not None else NameResolver()
        self._transcriber = transcriber
        self._describer = describer
        self.bot_mxid = bot_mxid
        self._download_media = download_media
        # Hermes's ``on_session_start`` hook only receives ``session_id``
        # — not the gateway — so we can't reach the live adapters from
        # there. Instead, wire them lazily on the first
        # ``pre_gateway_dispatch`` invocation (which DOES receive
        # ``gateway=self`` from Hermes's gateway/run.py).
        self._wire_gateway_once = wire_gateway_once
        self._gateway_wired = False

    # ------------------------------------------------------------------
    # Wiring helpers used by plugin.py at adapter-wiring time
    # ------------------------------------------------------------------

    def set_bot_mxid(self, mxid: str) -> None:
        self.bot_mxid = mxid

    def set_download_media(self, callback: DownloadMedia | None) -> None:
        self._download_media = callback

    # ------------------------------------------------------------------
    # Lazy media-processor accessors
    # ------------------------------------------------------------------

    def _get_transcriber(self) -> Transcriber | None:
        if self._transcriber is None:
            try:
                self._transcriber = Transcriber()
            except Exception as exc:
                logger.warning(
                    "hermes_chat_recorder: failed to construct transcriber: %s", exc
                )
                return None
        return self._transcriber

    def _get_describer(self) -> ImageDescriber | None:
        if self._describer is None:
            try:
                self._describer = ImageDescriber()
            except Exception as exc:
                logger.warning(
                    "hermes_chat_recorder: failed to construct describer: %s", exc
                )
                return None
        return self._describer

    # ------------------------------------------------------------------
    # The hook callback
    # ------------------------------------------------------------------

    def on_pre_gateway_dispatch(
        self,
        *,
        event: Any,
        gateway: Any = None,
        session_store: Any = None,
        **kwargs: Any,
    ) -> dict | None:
        """Sync callback invoked by Hermes per inbound message.

        Accepts ``**kwargs`` so future Hermes versions can add hook
        arguments without breaking us.

        Returns:
            * ``None`` — let the gateway dispatch normally (the default
              for text events and for events we don't recognize).
            * ``{"action": "rewrite", "text": str}`` — replace
              ``event.text`` before dispatch. Used for voice/image
              events so the agent sees the transcript / description
              instead of empty content.

        Never returns ``{"action": "skip"}``; wake decisions are owned
        by Hermes's native settings.
        """
        self._maybe_wire_gateway(gateway)

        try:
            info = extract_event(event)
        except Exception as exc:
            logger.warning("hermes_chat_recorder: extract failed: %s", exc)
            return None

        if info is None:
            # Not a message we know how to record.
            return None

        if self.config.platforms and info.platform not in self.config.platforms:
            # Operator restricted recording to specific platforms.
            return None

        if info.is_reaction:
            # Reactions don't get recorded.
            return None

        path_slug = self._path_slug(info)

        # Sync-replay duplicate — already in the vault. Don't double-
        # record and don't fight Hermes's own dedupe.
        if self.writer.has_event(info.event_id, path_slug, info.timestamp):
            return None

        # Edits get their own section linked back to the original.
        # We intentionally don't go through the placeholder/terminal
        # two-step here — edits ARE terminal as soon as they land.
        if info.is_edit:
            self._write_edit(info, path_slug)
            return None

        # Kinds with no processing pipeline are terminal immediately.
        if info.kind in ("video", "file", "location"):
            self._write_unprocessed(info, path_slug)
            return None

        # 1) Persist a placeholder so the event is durable even if
        #    downstream processing crashes.
        if not self._write_placeholder(info, path_slug):
            logger.error(
                "hermes_chat_recorder: vault placeholder write failed for "
                "event %s in %s; passing through unmodified",
                info.event_id,
                path_slug,
            )
            return None

        # 2) Process media → write terminal stage section.
        if info.kind == "voice":
            transcript, ok = self._process_voice(info, path_slug)
            if ok and transcript:
                return {"action": "rewrite", "text": transcript}
            # Failed: rewrite to a placeholder so the agent sees SOMETHING
            # if Hermes decides to wake it.
            return {
                "action": "rewrite",
                "text": "[voice note — transcription failed; please retry or send as text]",
            }

        if info.kind == "image":
            description, ocr_text, ok = self._process_image(info, path_slug)
            if ok and (description or ocr_text):
                return {
                    "action": "rewrite",
                    "text": self._format_image_rewrite(info.body, description, ocr_text),
                }
            # Failed: keep the caption (if any) so the agent has context.
            fallback = info.body or "[image — description failed]"
            return {"action": "rewrite", "text": fallback}

        # Text events: passthrough.
        return None

    # ------------------------------------------------------------------
    # Outbound recording (called from the wrapped adapter.send)
    # ------------------------------------------------------------------

    def record_outbound(
        self,
        *,
        platform: str,
        chat_id: str,
        text: str,
        event_id: str,
        timestamp: Any,
        sender_display: str = "",
        reply_to_event_id: str | None = None,
    ) -> None:
        """Record the bot's own reply to the vault."""
        if not self.config.record_outbound:
            return
        if self.config.platforms and platform not in self.config.platforms:
            return
        path_slug = self._compose_slug(
            platform, self.resolver.chat_slug(platform, chat_id)
        )
        sender = self._bot_display(platform, sender_display)
        fields: dict[str, str] = {}
        if reply_to_event_id:
            fields["reply_to"] = reply_to_event_id
        section = Section(
            event_id=event_id,
            timestamp=timestamp,
            sender=sender,
            kind="reply",
            stage="sent",
            fields=fields,
            body=text or "",
        )
        try:
            self.writer.write_section(section, path_slug=path_slug)
        except Exception as exc:
            logger.warning("hermes_chat_recorder: outbound write failed: %s", exc)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _path_slug(self, info: EventInfo) -> str:
        """Compose the vault path segment for an event's chat.

        Group layout: ``<platform>/<chat-slug>`` — the platform folder
        keeps chat IDs from different platforms from ever colliding.
        Flat (1on1) layout ignores the slug entirely, so the value is
        only used for lock scoping there.
        """
        # Only unnamed DMs may borrow the sender's display name for the
        # chat folder; a group chat must never be named after whoever
        # happened to speak first.
        peer_hint = info.sender_display if info.chat_type == "dm" else ""
        slug = self.resolver.chat_slug(
            info.platform,
            info.chat_id,
            name_hint=info.chat_name,
            peer_hint=peer_hint,
        )
        return self._compose_slug(info.platform, slug)

    @staticmethod
    def _compose_slug(platform: str, chat_slug: str) -> str:
        platform_part = platform or "unknown-platform"
        return f"{platform_part}/{chat_slug}"

    def _bot_display(self, platform: str, explicit: str = "") -> str:
        """Display name for the bot's outbound sections.

        Order: explicit caller string → ``bot_name`` config → resolver
        chain on the bot's own ID (Matrix MXID when known) → "bot".
        """
        if explicit and not looks_like_id(explicit):
            return explicit
        if self.config.bot_name:
            return self.config.bot_name
        if self.bot_mxid:
            # bot_mxid is a Matrix identity regardless of which
            # platform this outbound message is for — resolve it under
            # the matrix scope so the profile lookup applies and the
            # cache entry lands in the right namespace.
            resolved = self.resolver.user_display("matrix", self.bot_mxid)
            if resolved:
                return resolved
        return "bot"

    def _write_placeholder(self, info: EventInfo, path_slug: str) -> bool:
        """Write the initial placeholder section. Returns success."""
        fields = self._fields_for(info)
        body = info.body if info.kind == "text" else "(processing media…)"
        section = Section(
            event_id=info.event_id,
            timestamp=info.timestamp,
            sender=self._sender_display(info),
            kind=info.kind,
            stage="received",
            fields=fields,
            body=body,
        )
        try:
            self.writer.write_section(section, path_slug=path_slug)
        except Exception as exc:
            logger.error(
                "hermes_chat_recorder: placeholder write failed for %s in %s: %s",
                info.event_id,
                path_slug,
                exc,
            )
            return False
        return True

    def _write_unprocessed(self, info: EventInfo, path_slug: str) -> None:
        """Record a kind with no processing pipeline (video/file/location).

        The section is terminal (stage "recorded") immediately: body is
        the caption / text the platform delivered, fields carry the
        media provenance.
        """
        fields = self._fields_for(info)
        body = info.body or f"({info.kind} message)"
        section = Section(
            event_id=info.event_id,
            timestamp=info.timestamp,
            sender=self._sender_display(info),
            kind=info.kind,
            stage="recorded",
            fields=fields,
            body=body,
        )
        try:
            self.writer.write_section(section, path_slug=path_slug)
        except Exception as exc:
            logger.warning(
                "hermes_chat_recorder: %s write failed: %s", info.kind, exc
            )

    def _maybe_wire_gateway(self, gateway: Any) -> None:
        """Fire the gateway-wiring callback exactly once, on first message.

        Hermes's ``on_session_start`` hook doesn't receive the gateway
        object, so adapter wiring can't happen there.
        ``pre_gateway_dispatch`` is the earliest hook that does get it.
        We dedupe via ``self._gateway_wired`` so a late-arriving second
        gateway (e.g. test ctx that swaps gateways between calls)
        doesn't double-wrap ``adapter.send``.
        """
        if self._gateway_wired or gateway is None or self._wire_gateway_once is None:
            return
        self._gateway_wired = True
        try:
            self._wire_gateway_once(gateway)
        except Exception as exc:
            logger.warning("hermes_chat_recorder: gateway wiring failed: %s", exc)

    def _sender_display(self, info: EventInfo) -> str:
        """Resolve the friendliest display string for an event's sender.

        The event's ``sender_display`` hint (the adapter's ``user_name``
        or Matrix's enriched display name) is passed to the resolver,
        which prefers overrides, falls back to lookups, and caches the
        result.
        """
        return self.resolver.user_display(
            info.platform, info.sender_id, hint=info.sender_display
        )

    def _fields_for(self, info: EventInfo) -> dict[str, str]:
        fields: dict[str, str] = {}
        if info.mxc_url:
            fields["mxc"] = info.mxc_url
        if info.media_path and info.kind in ("video", "file", "location"):
            fields["media_path"] = info.media_path
        if info.mime:
            fields["mime"] = info.mime
        if info.duration_sec is not None:
            fields["duration_sec"] = str(info.duration_sec)
        if info.reply_to_id:
            fields["reply_to"] = info.reply_to_id
        return fields

    def _process_voice(self, info: EventInfo, path_slug: str) -> tuple[str, bool]:
        """Transcribe a voice note. Always writes a terminal section.

        Prefers ``info.media_path`` (set by the platform adapter after
        it downloads + decrypts the audio) so no download callable is
        needed. Falls back to the Matrix ``mxc_url + download_media``
        path for legacy events that somehow lack the cached file.
        """
        transcriber = self._get_transcriber()
        if transcriber is None:
            self._write_terminal(
                info,
                path_slug,
                stage="transcribe_failed",
                body="(transcriber unavailable)",
            )
            return "", False

        try:
            if info.media_path:
                # The adapter already downloaded + decrypted the bytes —
                # just hand the path to the STT layer.
                transcript = transcriber.transcribe(info.media_path)
            elif self._download_media and info.mxc_url:
                audio_bytes = self._download_media(info.mxc_url)
                transcript = self._transcribe_bytes(
                    transcriber, audio_bytes, info.mime or "audio/ogg"
                )
            else:
                self._write_terminal(
                    info,
                    path_slug,
                    stage="transcribe_failed",
                    body="(no local media path and no download fallback)",
                )
                return "", False
        except TranscriberError as exc:
            self._write_terminal(
                info,
                path_slug,
                stage="transcribe_failed",
                body=f"(transcription failed: {exc})",
            )
            return "", False
        except Exception as exc:
            self._write_terminal(
                info,
                path_slug,
                stage="transcribe_failed",
                body=f"(download or io failure: {exc})",
            )
            return "", False

        body = f"> {transcript}" if transcript else "(empty transcript)"
        self._write_terminal(info, path_slug, stage="transcribed", body=body)
        return transcript, True

    def _process_image(
        self, info: EventInfo, path_slug: str
    ) -> tuple[str, str, bool]:
        """Describe an image. Returns (description, ocr_text, success).

        Prefers ``info.media_path`` (already downloaded + decrypted by
        the platform adapter); falls back to fetching the Matrix
        ``mxc_url`` via the legacy download callable when the cached
        path is missing.
        """
        describer = self._get_describer()
        if describer is None:
            self._write_terminal(
                info,
                path_slug,
                stage="describe_failed",
                body="(describer unavailable)",
            )
            return "", "", False

        try:
            if info.media_path:
                with open(info.media_path, "rb") as f:
                    image_bytes = f.read()
            elif self._download_media and info.mxc_url:
                image_bytes = self._download_media(info.mxc_url)
            else:
                self._write_terminal(
                    info,
                    path_slug,
                    stage="describe_failed",
                    body="(no local media path and no download fallback)",
                )
                return "", "", False
            result = describer.describe(image_bytes, mime=info.mime or "image/png")
        except ImageDescriberError as exc:
            self._write_terminal(
                info,
                path_slug,
                stage="describe_failed",
                body=f"(description failed: {exc})",
            )
            return "", "", False
        except Exception as exc:
            self._write_terminal(
                info,
                path_slug,
                stage="describe_failed",
                body=f"(download or io failure: {exc})",
            )
            return "", "", False

        body_parts: list[str] = []
        if result.description:
            body_parts.append(result.description)
        if result.text:
            body_parts.append(f"\n**text:**\n{result.text}")
        body = "\n".join(body_parts) if body_parts else "(empty description)"
        self._write_terminal(info, path_slug, stage="described", body=body)
        return result.description, result.text, True

    def _write_edit(self, info: EventInfo, path_slug: str) -> None:
        """Append a section for a Matrix ``m.replace`` edit event.

        The edit has its own (new) event_id, so it gets its own
        section anchor. The ``edits:`` field points back at the
        original message so a reader can scan upward to find what was
        being changed. We don't try to mutate the original section in
        place — the vault is a historical record, and Matrix itself
        keeps every edit as a distinct event on the wire.

        NOTE: Hermes's current Matrix adapter filters edits before
        dispatch, so this path is dormant defense — it only fires if an
        older or future adapter passes ``m.replace`` events through.
        """
        fields: dict[str, str] = {}
        if info.replaced_event_id:
            fields["edits"] = info.replaced_event_id
        section = Section(
            event_id=info.event_id,
            timestamp=info.timestamp,
            sender=self._sender_display(info),
            kind="text",
            stage="edited",
            fields=fields,
            body=info.body or "(edited message body empty)",
        )
        try:
            self.writer.write_section(section, path_slug=path_slug)
        except Exception as exc:
            logger.warning("hermes_chat_recorder: edit write failed: %s", exc)

    def _write_terminal(
        self,
        info: EventInfo,
        path_slug: str,
        *,
        stage: str,
        body: str,
    ) -> None:
        fields = self._fields_for(info)
        section = Section(
            event_id=info.event_id,
            timestamp=info.timestamp,
            sender=self._sender_display(info),
            kind=info.kind,
            stage=stage,  # type: ignore[arg-type]
            fields=fields,
            body=body,
        )
        try:
            self.writer.write_section(section, path_slug=path_slug)
        except Exception as exc:
            logger.warning("hermes_chat_recorder: terminal write failed: %s", exc)

    def _transcribe_bytes(
        self, transcriber: Transcriber, audio_bytes: bytes, mime: str
    ) -> str:
        """Spill bytes to a tempfile so the STT layer can read them."""
        import contextlib
        import os
        import tempfile

        ext = self._ext_for_mime(mime)
        fd, path = tempfile.mkstemp(suffix=ext)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(audio_bytes)
            return transcriber.transcribe(path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    @staticmethod
    def _ext_for_mime(mime: str) -> str:
        m = (mime or "").lower()
        if "ogg" in m:
            return ".ogg"
        if "opus" in m:
            return ".opus"
        if "wav" in m:
            return ".wav"
        if "mp3" in m or "mpeg" in m:
            return ".mp3"
        if "m4a" in m or "aac" in m:
            return ".m4a"
        return ".audio"

    @staticmethod
    def _format_image_rewrite(caption: str, description: str, ocr_text: str) -> str:
        """Compose the text the agent sees in place of an image event."""
        parts: list[str] = []
        if caption:
            parts.append(caption)
        if description:
            parts.append(f"[image description: {description}]")
        if ocr_text:
            parts.append(f"[text in image:\n{ocr_text}]")
        return "\n\n".join(parts) if parts else "[image]"
