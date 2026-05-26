"""Recorder — orchestrates writer, gate, transcriber, describer.

The Recorder owns the per-process state (writer + gate + lazy-init'd
transcriber and describer) and provides the two callbacks Hermes binds
into:

* :meth:`Recorder.on_pre_gateway_dispatch` — sync callback wired to the
  ``pre_gateway_dispatch`` plugin hook. Records every inbound Matrix
  event to the vault and decides skip/rewrite/allow per the wake gate.
* :meth:`Recorder.on_session_start` — sync callback wired to
  ``on_session_start``. Locates the live Matrix adapter, captures a
  download-media handle, and wraps the adapter's ``send`` so outbound
  replies also land in the vault.

See ``docs/DESIGN.md §2`` for the dispatch contract and ``§5`` for
the Pattern A (sync blocking) concurrency model.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from hermes_chat_recorder.config import RecorderConfig
from hermes_chat_recorder.describer import ImageDescriber, ImageDescriberError
from hermes_chat_recorder.gate import Gate
from hermes_chat_recorder.matrix_event import (
    MatrixEventInfo,
    extract as extract_matrix_event,
    room_slug_from_room_id,
)
from hermes_chat_recorder.transcriber import Transcriber, TranscriberError
from hermes_chat_recorder.types import GateInput, Section
from hermes_chat_recorder.writer import VaultWriter

logger = logging.getLogger(__name__)


# Type alias for the media-download callable. Plugin.py binds this to
# the live Matrix adapter's media-download method. None means "skip
# media processing this turn" — sections stay at stage:received.
DownloadMedia = Callable[[str], bytes]


class Recorder:
    """Wires every Matrix message through the vault writer and the wake gate."""

    def __init__(
        self,
        *,
        config: RecorderConfig,
        writer: VaultWriter,
        gate: Gate,
        transcriber: Transcriber | None = None,
        describer: ImageDescriber | None = None,
        bot_mxid: str = "",
        download_media: DownloadMedia | None = None,
    ) -> None:
        self.config = config
        self.writer = writer
        self.gate = gate
        self._transcriber = transcriber
        self._describer = describer
        self.bot_mxid = bot_mxid
        self._download_media = download_media

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
                self._transcriber = Transcriber(model_size=self.config.whisper_model_size)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "transcript_recorder: failed to load faster-whisper '%s': %s",
                    self.config.whisper_model_size,
                    exc,
                )
                return None
        return self._transcriber

    def _get_describer(self) -> ImageDescriber | None:
        if self._describer is None:
            if not self.config.openrouter_api_key:
                return None
            try:
                self._describer = ImageDescriber(
                    api_key=self.config.openrouter_api_key,
                    model=self.config.image_describer_model,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("transcript_recorder: failed to construct describer: %s", exc)
                return None
        return self._describer

    # ------------------------------------------------------------------
    # The hook callback
    # ------------------------------------------------------------------

    def on_pre_gateway_dispatch(
        self, *, event: Any, gateway: Any = None, session_store: Any = None
    ) -> dict | None:
        """Hermes invokes this synchronously for every inbound message.

        Returns:
            * ``None`` — let the gateway dispatch the event normally.
            * ``{"action": "skip", "reason": str}`` — drop the event;
              the agent should NOT generate a reply.
            * ``{"action": "rewrite", "text": str}`` — replace
              ``event.text`` before dispatch (used to substitute
              transcripts / image descriptions for media events).
        """
        try:
            info = extract_matrix_event(event)
        except Exception as exc:  # noqa: BLE001 - defensive; never crash the dispatch loop
            logger.warning("transcript_recorder: extract failed: %s", exc)
            return None

        if info is None:
            # Not a Matrix message we know how to handle. Let the
            # gateway dispatch unmodified.
            return None

        if info.is_reaction:
            # Reactions aren't recorded as sections and never wake the
            # agent. Skip so Hermes drops it on the floor.
            return {"action": "skip", "reason": "reaction-not-recorded"}

        room_slug = room_slug_from_room_id(info.room_id)

        # Sync-replay short-circuit. Matrix re-delivers events with the
        # same event_id on reconnect — we MUST NOT double-wake the agent.
        # Writer.has_event scans the day file we'd write into, so this
        # is exact-match by anchor.
        if self.writer.has_event(info.event_id, room_slug, info.timestamp):
            logger.info(
                "transcript_recorder: skipping duplicate event %s in %s",
                info.event_id,
                room_slug,
            )
            return {"action": "skip", "reason": "sync-replay-duplicate"}

        # 1) Persist a placeholder section so the event is durable even
        #    if downstream processing crashes.
        self._write_placeholder(info, room_slug)

        # 2) Process media (voice → transcript, image → description),
        #    upgrade the section to its terminal stage.
        gate_text = info.body
        rewrite_text: str | None = None

        if info.kind == "voice":
            transcript, ok = self._process_voice(info, room_slug)
            gate_text = transcript
            if ok and transcript:
                rewrite_text = transcript
        elif info.kind == "image":
            description, ocr_text, ok = self._process_image(info, room_slug)
            # Gate on caption + OCR text only — model-generated description
            # MUST NOT be allowed to false-wake the agent.
            gate_text = (info.body + "\n" + ocr_text).strip()
            if ok and (description or ocr_text):
                rewrite_text = self._format_image_rewrite(info.body, description, ocr_text)

        # 3) Apply the gate.
        wake = self.gate.should_wake(
            GateInput(
                gate_text=gate_text or "",
                bot_mxid=self.bot_mxid,
                sender_mxid=info.sender_mxid,
                mentioned_mxids=info.mentioned_mxids,
            )
        )

        if not wake:
            return {"action": "skip", "reason": "no-mention-or-nickname"}

        if rewrite_text is not None:
            return {"action": "rewrite", "text": rewrite_text}
        return {"action": "allow"}

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
        """Record the bot's own reply to the vault. Called from the
        wrapper around the Matrix adapter's send method."""
        if not self.config.record_outbound:
            return
        room_slug = room_slug_from_room_id(room_id)
        fields: dict[str, str] = {}
        if reply_to_event_id:
            fields["reply_to"] = reply_to_event_id
        section = Section(
            event_id=event_id,
            timestamp=timestamp,
            sender=sender_display or "bot",
            kind="reply",
            stage="sent",
            fields=fields,
            body=text or "",
        )
        try:
            self.writer.write_section(section, room_slug=room_slug)
        except Exception as exc:  # noqa: BLE001 - we never want vault failure to break send
            logger.warning("transcript_recorder: outbound write failed: %s", exc)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _write_placeholder(self, info: MatrixEventInfo, room_slug: str) -> None:
        fields = self._fields_for(info)
        body = info.body if info.kind == "text" else "(processing media…)"
        section = Section(
            event_id=info.event_id,
            timestamp=info.timestamp,
            sender=info.sender_display,
            kind=info.kind,
            stage="received",
            fields=fields,
            body=body,
        )
        try:
            self.writer.write_section(section, room_slug=room_slug)
        except Exception as exc:  # noqa: BLE001 - logged, but we keep going
            logger.warning("transcript_recorder: placeholder write failed: %s", exc)

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
        """Download + transcribe a voice note. Always writes a terminal
        section to the vault. Returns (transcript, success).
        """
        download = self._download_media

        # Check the cheap preconditions BEFORE lazy-loading faster-whisper,
        # so a misconfigured deploy doesn't pay the ~10s model-load cost
        # just to write a failure section.
        if download is None or not info.mxc_url:
            self._write_terminal(
                info,
                room_slug,
                stage="transcribe_failed",
                body="(download unavailable)",
            )
            return "", False

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
            audio_bytes = download(info.mxc_url)
            transcript = self._transcribe_bytes(transcriber, audio_bytes, info.mime or "audio/ogg")
        except TranscriberError as exc:
            self._write_terminal(
                info,
                room_slug,
                stage="transcribe_failed",
                body=f"(transcription failed: {exc})",
            )
            return "", False
        except Exception as exc:  # noqa: BLE001
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
        """Download + describe an image. Returns (description, ocr_text, success)."""
        download = self._download_media

        if download is None or not info.mxc_url:
            self._write_terminal(
                info,
                room_slug,
                stage="describe_failed",
                body="(download unavailable)",
            )
            return "", "", False

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
            image_bytes = download(info.mxc_url)
            result = describer.describe(image_bytes, mime=info.mime or "image/png")
        except ImageDescriberError as exc:
            self._write_terminal(
                info,
                room_slug,
                stage="describe_failed",
                body=f"(description failed: {exc})",
            )
            return "", "", False
        except Exception as exc:  # noqa: BLE001
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
            sender=info.sender_display,
            kind=info.kind,
            stage=stage,  # type: ignore[arg-type]
            fields=fields,
            body=body,
        )
        try:
            self.writer.write_section(section, room_slug=room_slug)
        except Exception as exc:  # noqa: BLE001
            logger.warning("transcript_recorder: terminal write failed: %s", exc)

    def _transcribe_bytes(
        self, transcriber: Transcriber, audio_bytes: bytes, mime: str
    ) -> str:
        """Spill bytes to a tempfile so faster-whisper can read them.

        faster-whisper accepts file paths or numpy arrays; the tempfile
        hop is the lowest-friction path that works for both ogg and
        opus voice notes from Matrix.
        """
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
