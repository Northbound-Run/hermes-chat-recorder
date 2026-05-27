"""Recorder — orchestrates writer, transcriber, describer.

The Recorder owns the per-process state (writer + lazy-init'd
transcriber and describer) and provides the two callbacks Hermes binds
into:

* :meth:`Recorder.on_pre_gateway_dispatch` — sync callback wired to the
  ``pre_gateway_dispatch`` plugin hook. **Records every inbound Matrix
  message to the vault**. For voice/image events it transcribes /
  describes the media and rewrites ``event.text`` so the agent has
  usable content. It does NOT make wake decisions — whether the agent
  replies is governed by Hermes's native settings (e.g.
  ``MATRIX_REQUIRE_MENTION``).
* :meth:`Recorder.on_session_start` — sync callback wired to
  ``on_session_start``. Locates the live Matrix adapter, captures a
  download-media handle, and wraps the adapter's ``send`` so outbound
  replies also land in the vault.

See ``docs/DESIGN.md`` for the storage format and concurrency model.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from hermes_chat_recorder.config import RecorderConfig
from hermes_chat_recorder.describer import ImageDescriber, ImageDescriberError
from hermes_chat_recorder.matrix_event import (
    MatrixEventInfo,
)
from hermes_chat_recorder.matrix_event import (
    extract as extract_matrix_event,
)
from hermes_chat_recorder.name_resolver import NameResolver
from hermes_chat_recorder.transcriber import Transcriber, TranscriberError
from hermes_chat_recorder.types import Section
from hermes_chat_recorder.writer import VaultWriter

logger = logging.getLogger(__name__)


# Type alias for the media-download callable. Plugin.py binds this to
# the live Matrix adapter's media-download method. None means "skip
# media processing this turn" — sections stay at stage:received.
DownloadMedia = Callable[[str], bytes]


def _looks_like_mxid(s: str) -> bool:
    """Heuristic for ``@localpart:server`` shaped strings."""
    return s.startswith("@") and ":" in s


class Recorder:
    """Records every Matrix message to the vault.

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
        # Resolver defaults to a bare instance with no lookups wired —
        # falls back to room_slug_from_room_id / MXID localpart until
        # the gateway-wiring callback (below) plumbs the live Matrix
        # client through on the first pre_gateway_dispatch.
        self.resolver = resolver if resolver is not None else NameResolver()
        self._transcriber = transcriber
        self._describer = describer
        self.bot_mxid = bot_mxid
        self._download_media = download_media
        # Hermes's ``on_session_start`` hook only receives ``session_id``
        # — not the gateway — so we can't reach the live Matrix adapter
        # from there. Instead, wire the adapter lazily on the first
        # ``pre_gateway_dispatch`` invocation (which DOES receive
        # ``gateway=self``, see gateway/run.py:5805 in Hermes).
        self._wire_gateway_once = wire_gateway_once
        self._gateway_wired = False

    # ------------------------------------------------------------------
    # Wiring helpers used by plugin.py at on_session_start time
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
        self, *, event: Any, gateway: Any = None, session_store: Any = None
    ) -> dict | None:
        """Sync callback invoked by Hermes per inbound message.

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
            info = extract_matrix_event(event)
        except Exception as exc:
            logger.warning("hermes_chat_recorder: extract failed: %s", exc)
            return None

        if info is None:
            # Not a Matrix message we know how to handle.
            return None

        if info.is_reaction:
            # Reactions don't get recorded.
            return None

        room_slug = self.resolver.room_slug(info.room_id)

        # Sync-replay duplicate — already in the vault. Don't double-
        # record and don't fight Hermes's own dedupe.
        if self.writer.has_event(info.event_id, room_slug, info.timestamp):
            return None

        # 1) Persist a placeholder so the event is durable even if
        #    downstream processing crashes.
        if not self._write_placeholder(info, room_slug):
            logger.error(
                "hermes_chat_recorder: vault placeholder write failed for "
                "event %s in %s; passing through unmodified",
                info.event_id,
                room_slug,
            )
            return None

        # 2) Process media → write terminal stage section.
        if info.kind == "voice":
            transcript, ok = self._process_voice(info, room_slug)
            if ok and transcript:
                return {"action": "rewrite", "text": transcript}
            # Failed: rewrite to a placeholder so the agent sees SOMETHING
            # if Hermes decides to wake it.
            return {
                "action": "rewrite",
                "text": "[voice note — transcription failed; please retry or send as text]",
            }

        if info.kind == "image":
            description, ocr_text, ok = self._process_image(info, room_slug)
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
        room_id: str,
        sender_display: str,
        text: str,
        event_id: str,
        timestamp: Any,
        reply_to_event_id: str | None = None,
    ) -> None:
        """Record the bot's own reply to the vault."""
        if not self.config.record_outbound:
            return
        room_slug = self.resolver.room_slug(room_id)
        # If the caller passed something MXID-shaped (or empty), let the
        # resolver pick a friendlier display name; otherwise honor the
        # explicit string the caller supplied.
        sender = self._best_display(sender_display, self.bot_mxid) or "bot"
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
            self.writer.write_section(section, room_slug=room_slug)
        except Exception as exc:
            logger.warning("hermes_chat_recorder: outbound write failed: %s", exc)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _write_placeholder(self, info: MatrixEventInfo, room_slug: str) -> bool:
        """Write the initial placeholder section. Returns success."""
        fields = self._fields_for(info)
        body = info.body if info.kind == "text" else "(processing media…)"
        section = Section(
            event_id=info.event_id,
            timestamp=info.timestamp,
            sender=self._best_display(info.sender_display, info.sender_mxid),
            kind=info.kind,
            stage="received",
            fields=fields,
            body=body,
        )
        try:
            self.writer.write_section(section, room_slug=room_slug)
        except Exception as exc:
            logger.error(
                "hermes_chat_recorder: placeholder write failed for %s in %s: %s",
                info.event_id,
                room_slug,
                exc,
            )
            return False
        return True

    def _maybe_wire_gateway(self, gateway: Any) -> None:
        """Fire the gateway-wiring callback exactly once, on first message.

        Hermes's ``on_session_start`` hook doesn't receive the gateway
        object (see hermes_cli/hooks.py:142), so adapter wiring can't
        happen there. ``pre_gateway_dispatch`` is the earliest hook
        that does get it. We dedupe via ``self._gateway_wired`` so a
        late-arriving second gateway (e.g. test ctx that swaps gateways
        between calls) doesn't double-wrap ``adapter.send``.
        """
        if self._gateway_wired or gateway is None or self._wire_gateway_once is None:
            return
        self._gateway_wired = True
        try:
            self._wire_gateway_once(gateway)
        except Exception as exc:  # noqa: BLE001 - wiring must never break dispatch
            logger.warning("hermes_chat_recorder: gateway wiring failed: %s", exc)

    def _best_display(self, hint: str, mxid: str) -> str:
        """Return the friendliest available display string for a sender.

        ``hint`` is whatever the caller already has on hand — mautrix's
        enriched ``sender_display_name`` for inbound events, or an
        explicit string for outbound bot replies. If it's empty, equal
        to the MXID, or looks like an MXID itself, defer to the
        resolver's lookup chain. Otherwise honor the hint verbatim so
        callers can override (e.g. tests passing ``"Ralph"``).
        """
        if hint and hint != mxid and not _looks_like_mxid(hint):
            return hint
        return self.resolver.user_display(mxid)

    def _fields_for(self, info: MatrixEventInfo) -> dict[str, str]:
        fields: dict[str, str] = {}
        if info.mxc_url:
            fields["mxc"] = info.mxc_url
        if info.mime:
            fields["mime"] = info.mime
        if info.duration_sec is not None:
            fields["duration_sec"] = str(info.duration_sec)
        return fields

    def _process_voice(self, info: MatrixEventInfo, room_slug: str) -> tuple[str, bool]:
        """Transcribe a voice note. Always writes a terminal section.

        Prefers ``info.local_media_path`` (set by the Matrix adapter
        after it downloads + decrypts the audio) so we don't need a
        ``download_media`` callable at all. Falls back to the
        ``mxc_url + self._download_media`` path for legacy events that
        somehow lack the cached path.
        """
        transcriber = self._get_transcriber()
        if transcriber is None:
            self._write_terminal(
                info,
                room_slug,
                stage="transcribe_failed",
                body="(transcriber unavailable)",
            )
            return "", False

        try:
            if info.local_media_path:
                # Hermes already downloaded + decrypted the bytes — just
                # hand the path to the STT layer.
                transcript = transcriber.transcribe(info.local_media_path)
            elif self._download_media and info.mxc_url:
                audio_bytes = self._download_media(info.mxc_url)
                transcript = self._transcribe_bytes(
                    transcriber, audio_bytes, info.mime or "audio/ogg"
                )
            else:
                self._write_terminal(
                    info,
                    room_slug,
                    stage="transcribe_failed",
                    body="(no local path and no download callable)",
                )
                return "", False
        except TranscriberError as exc:
            self._write_terminal(
                info,
                room_slug,
                stage="transcribe_failed",
                body=f"(transcription failed: {exc})",
            )
            return "", False
        except Exception as exc:
            self._write_terminal(
                info,
                room_slug,
                stage="transcribe_failed",
                body=f"(download or io failure: {exc})",
            )
            return "", False

        body = f"> {transcript}" if transcript else "(empty transcript)"
        self._write_terminal(info, room_slug, stage="transcribed", body=body)
        return transcript, True

    def _process_image(
        self, info: MatrixEventInfo, room_slug: str
    ) -> tuple[str, str, bool]:
        """Describe an image. Returns (description, ocr_text, success).

        Prefers ``info.local_media_path`` (already downloaded +
        decrypted by the Matrix adapter); falls back to fetching the
        ``mxc_url`` via the legacy download callable when the cached
        path is missing.
        """
        describer = self._get_describer()
        if describer is None:
            self._write_terminal(
                info,
                room_slug,
                stage="describe_failed",
                body="(describer unavailable)",
            )
            return "", "", False

        try:
            if info.local_media_path:
                with open(info.local_media_path, "rb") as f:
                    image_bytes = f.read()
            elif self._download_media and info.mxc_url:
                image_bytes = self._download_media(info.mxc_url)
            else:
                self._write_terminal(
                    info,
                    room_slug,
                    stage="describe_failed",
                    body="(no local path and no download callable)",
                )
                return "", "", False
            result = describer.describe(image_bytes, mime=info.mime or "image/png")
        except ImageDescriberError as exc:
            self._write_terminal(
                info,
                room_slug,
                stage="describe_failed",
                body=f"(description failed: {exc})",
            )
            return "", "", False
        except Exception as exc:
            self._write_terminal(
                info,
                room_slug,
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
        self._write_terminal(info, room_slug, stage="described", body=body)
        return result.description, result.text, True

    def _write_terminal(
        self,
        info: MatrixEventInfo,
        room_slug: str,
        *,
        stage: str,
        body: str,
    ) -> None:
        fields = self._fields_for(info)
        section = Section(
            event_id=info.event_id,
            timestamp=info.timestamp,
            sender=self._best_display(info.sender_display, info.sender_mxid),
            kind=info.kind,
            stage=stage,  # type: ignore[arg-type]
            fields=fields,
            body=body,
        )
        try:
            self.writer.write_section(section, room_slug=room_slug)
        except Exception as exc:
            logger.warning("hermes_chat_recorder: terminal write failed: %s", exc)

    def _transcribe_bytes(
        self, transcriber: Transcriber, audio_bytes: bytes, mime: str
    ) -> str:
        """Spill bytes to a tempfile so faster-whisper can read them."""
        import os
        import tempfile

        ext = self._ext_for_mime(mime)
        fd, path = tempfile.mkstemp(suffix=ext)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(audio_bytes)
            return transcriber.transcribe(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

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
