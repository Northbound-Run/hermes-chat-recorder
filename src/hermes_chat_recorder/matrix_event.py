"""Thin extractor that turns a Hermes ``MessageEvent`` (with Matrix-
specific ``raw_message``) into a typed :class:`MatrixEventInfo` we can
hand to the recorder.

This module is the ONLY place in the package that touches Hermes /
mautrix shapes directly. By isolating it here, the recorder + writer +
gate stay testable without those runtime dependencies, and we have one
file to update when upstream MessageEvent fields move.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from hermes_chat_recorder.types import MessageKind

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MatrixEventInfo:
    """Normalized Matrix event the recorder operates on."""

    event_id: str
    room_id: str
    sender_mxid: str
    sender_display: str
    timestamp: datetime  # tz-aware UTC
    kind: MessageKind
    body: str
    mxc_url: str | None = None
    # Local filesystem path of the already-downloaded (and decrypted, in
    # E2EE rooms) media bytes. Populated by the Matrix adapter via
    # ``MessageEvent.media_urls[0]``. Using this instead of re-downloading
    # via the mautrix client avoids private-attr access, sidesteps E2EE
    # decryption duplication, and works whether or not we have a live
    # client handle.
    local_media_path: str | None = None
    mime: str | None = None
    duration_sec: int | None = None
    mentioned_mxids: frozenset[str] = frozenset()
    is_reaction: bool = False
    # Matrix edits arrive as their own event with an ``m.relates_to``
    # block whose ``rel_type`` is ``m.replace``. When this event IS an
    # edit, ``is_edit`` is True and ``replaced_event_id`` points back
    # at the original message; ``body`` contains the new content
    # (stripped of the ``* `` fallback prefix Matrix clients add).
    is_edit: bool = False
    replaced_event_id: str | None = None


# Hermes's ``MessageType`` enum ships various flavours; map to ours.
_KIND_BY_TYPE_NAME: dict[str, MessageKind] = {
    "TEXT": "text",
    "AUDIO": "voice",
    "VOICE": "voice",
    "IMAGE": "image",
    "PHOTO": "image",
    "STICKER": "image",
}


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """Read the first attribute/key that exists. Tolerates the
    differences between dataclasses, mautrix objects, and plain dicts."""
    for name in names:
        if obj is None:
            return default
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
            continue
        if hasattr(obj, name):
            val = getattr(obj, name)
            if val is not None:
                return val
    return default


def _kind_from_message_type(message_type: Any) -> MessageKind | None:
    """Map a Hermes MessageType enum (or its name) to our MessageKind."""
    if message_type is None:
        return None
    name = getattr(message_type, "name", None) or str(message_type)
    name = name.upper().rsplit(".", 1)[-1]
    return _KIND_BY_TYPE_NAME.get(name)


def _is_matrix_source(source: Any) -> bool:
    """Check the source's platform == 'matrix' tolerating enum or string."""
    if source is None:
        return False
    platform = _get(source, "platform")
    if platform is None:
        return False
    value = getattr(platform, "value", None) or str(platform)
    return str(value).lower() == "matrix"


def _extract_timestamp(raw_message: Any) -> datetime:
    """Pull a tz-aware UTC datetime from the raw mautrix event.

    Mautrix events expose ``origin_server_ts`` in milliseconds since
    epoch. Tests can pass a plain object with that field. Falls back to
    ``datetime.now(timezone.utc)`` if nothing usable is present.
    """
    ts_ms = _get(raw_message, "origin_server_ts", "originServerTs", "timestamp_ms")
    if isinstance(ts_ms, (int, float)):
        return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _extract_mxc_and_media(
    raw_message: Any, kind: MessageKind
) -> tuple[str | None, str | None, int | None]:
    """Return (mxc_url, mime, duration_sec) from the event content.

    Handles two shapes:

    * **Plaintext rooms** — ``content.url`` is the mxc.
    * **End-to-end encrypted rooms** — ``content.file.url`` is the mxc;
      ``content.file`` also carries ``key``, ``iv``, ``hashes``, ``v``.
      We just return the URL; whether the bytes we download are
      ciphertext (and need decryption upstream) depends on Hermes's
      adapter behaviour. If they ARE ciphertext, faster-whisper will
      fail with a clear "malformed audio" — the recorder writes a
      `transcribe_failed` section per the design.
    """
    if kind not in ("voice", "image"):
        return None, None, None

    content = _get(raw_message, "content")
    if content is None:
        return None, None, None

    # Prefer the plaintext url; fall back to the E2EE `file.url`.
    url = _get(content, "url")
    if url is None:
        encrypted_file = _get(content, "file")
        if encrypted_file is not None:
            url = _get(encrypted_file, "url")

    info = _get(content, "info", default={}) or {}
    mime = _get(info, "mimetype")

    duration_sec: int | None = None
    if kind == "voice":
        duration_ms = _get(info, "duration")
        if isinstance(duration_ms, (int, float)):
            duration_sec = int(duration_ms / 1000)

    if url is not None and not isinstance(url, str):
        url = str(url)
    if mime is not None and not isinstance(mime, str):
        mime = str(mime)
    return url, mime, duration_sec


def _extract_mentions(raw_message: Any) -> frozenset[str]:
    """Pull the m.mentions.user_ids list from event content if present.

    Falls back to checking the legacy ``mentions`` content key. Returns
    an empty frozenset when nothing is found — callers should NOT treat
    "no mentions detected" as "the bot wasn't mentioned"; the gate
    layers in nickname-detection too.
    """
    content = _get(raw_message, "content")
    if content is None:
        return frozenset()

    # m.mentions: { user_ids: [...] }
    mentions = _get(content, "m.mentions", "mentions")
    if isinstance(mentions, dict):
        user_ids = mentions.get("user_ids") or mentions.get("userIds")
        if isinstance(user_ids, (list, tuple)):
            return frozenset(str(uid) for uid in user_ids if isinstance(uid, str))
    return frozenset()


def _is_reaction(raw_message: Any) -> bool:
    """True if the raw event is an m.reaction (we don't record these)."""
    event_type = _get(raw_message, "type")
    if event_type is None:
        return False
    value = getattr(event_type, "value", None) or str(event_type)
    return str(value).lower() == "m.reaction"


def _extract_sender_display(raw_message: Any, fallback: str) -> str:
    """Best-effort display name from the event, fallback to MXID."""
    return _get(raw_message, "sender_display_name", "displayname", default=fallback)


def extract(event: Any) -> MatrixEventInfo | None:
    """Pull a normalized :class:`MatrixEventInfo` off a Hermes event.

    Returns ``None`` when the event is NOT a Matrix message (different
    platform, unsupported message type, reaction event, etc.). The
    recorder uses ``None`` as the signal to bail out early without
    touching the vault.
    """
    source = _get(event, "source")
    if not _is_matrix_source(source):
        return None

    raw = _get(event, "raw_message")

    if _is_reaction(raw):
        return MatrixEventInfo(
            event_id=str(_get(event, "message_id", default="")),
            room_id=str(_get(source, "chat_id", default="")),
            sender_mxid=str(_get(source, "user_id", default="")),
            sender_display="",
            timestamp=_extract_timestamp(raw),
            kind="text",
            body="",
            is_reaction=True,
        )

    kind = _kind_from_message_type(_get(event, "message_type"))
    if kind is None:
        # Unsupported message type (video, file, etc.) — skip.
        return None

    event_id = _get(event, "message_id")
    if not event_id:
        return None
    chat_id = _get(source, "chat_id")
    user_id = _get(source, "user_id")
    if not chat_id or not user_id:
        return None

    mxc_url, mime, duration_sec = _extract_mxc_and_media(raw, kind)
    local_media_path = _extract_local_media_path(event, kind)
    is_edit, replaced_event_id, edited_body = _extract_edit_relation(raw)
    body = edited_body or str(_get(event, "text", default="") or "")
    return MatrixEventInfo(
        event_id=str(event_id),
        room_id=str(chat_id),
        sender_mxid=str(user_id),
        sender_display=_extract_sender_display(raw, fallback=str(user_id)),
        timestamp=_extract_timestamp(raw),
        kind=kind,
        body=body,
        mxc_url=mxc_url,
        local_media_path=local_media_path,
        mime=mime,
        duration_sec=duration_sec,
        mentioned_mxids=_extract_mentions(raw),
        is_reaction=False,
        is_edit=is_edit,
        replaced_event_id=replaced_event_id,
    )


def _extract_edit_relation(raw_message: Any) -> tuple[bool, str | None, str | None]:
    """Detect a Matrix edit (m.replace) on the raw event.

    Returns ``(is_edit, replaced_event_id, new_body)``. Matrix's edit
    convention puts the new content under ``content.m.new_content`` and
    flags the relationship via ``content.m.relates_to`` with
    ``rel_type == "m.replace"``. The top-level ``content.body`` also
    carries the new body but with a ``"* "`` fallback prefix that
    legacy clients show — we prefer ``m.new_content.body`` when it's
    present, falling back to a stripped ``content.body`` otherwise.
    """
    content = _get(raw_message, "content")
    if content is None:
        return False, None, None
    relates = _get(content, "m.relates_to", "relates_to")
    if not isinstance(relates, dict):
        return False, None, None
    rel_type = relates.get("rel_type") or relates.get("relType")
    if str(rel_type) != "m.replace":
        return False, None, None
    target = relates.get("event_id") or relates.get("eventId")
    if not isinstance(target, str) or not target:
        return False, None, None

    new_body: str | None = None
    new_content = _get(content, "m.new_content", "new_content")
    if isinstance(new_content, dict):
        candidate = new_content.get("body")
        if isinstance(candidate, str) and candidate:
            new_body = candidate
    if new_body is None:
        # Fallback: strip the "* " fallback prefix off content.body.
        candidate = _get(content, "body")
        if isinstance(candidate, str) and candidate:
            new_body = candidate[2:] if candidate.startswith("* ") else candidate

    return True, target, new_body


def _extract_local_media_path(event: Any, kind: MessageKind) -> str | None:
    """Pull a locally-cached media path off the Hermes ``MessageEvent``.

    The matrix adapter populates ``MessageEvent.media_urls = [cached_path]``
    after downloading (and decrypting, in E2EE rooms) the media bytes.
    When that's present we'd much rather point our STT/vision tools at
    the cached file than re-download the mxc URL ourselves.
    """
    if kind not in ("voice", "image"):
        return None
    urls = _get(event, "media_urls")
    if not urls:
        return None
    if isinstance(urls, (list, tuple)) and urls:
        candidate = urls[0]
        if isinstance(candidate, str) and candidate and not candidate.startswith(
            ("http://", "https://", "mxc://")
        ):
            return candidate
    return None


def room_slug_from_room_id(room_id: str) -> str:
    """Derive a filesystem-safe slug from a Matrix room ID.

    Matrix room IDs look like ``!abcdef:server``. We strip the leading
    ``!`` and the ``:server`` suffix. For canonical-alias rooms
    (``#name:server``), strip the ``#`` and suffix too. Anything else
    is sanitized to alnum+hyphen.
    """
    if not room_id:
        return "unknown-room"
    s = room_id
    if s.startswith(("!", "#")):
        s = s[1:]
    if ":" in s:
        s = s.split(":", 1)[0]
    # Replace anything unsafe for a filename.
    safe = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_"):
            safe.append(ch)
        else:
            safe.append("-")
    cleaned = "".join(safe).strip("-") or "unknown-room"
    return cleaned
