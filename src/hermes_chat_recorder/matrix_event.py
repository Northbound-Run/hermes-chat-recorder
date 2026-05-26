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
    mime: str | None = None
    duration_sec: int | None = None
    mentioned_mxids: frozenset[str] = frozenset()
    is_reaction: bool = False


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
    """Return (mxc_url, mime, duration_sec) from the event content."""
    if kind not in ("voice", "image"):
        return None, None, None

    content = _get(raw_message, "content")
    if content is None:
        return None, None, None

    url = _get(content, "url")
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
    return MatrixEventInfo(
        event_id=str(event_id),
        room_id=str(chat_id),
        sender_mxid=str(user_id),
        sender_display=_extract_sender_display(raw, fallback=str(user_id)),
        timestamp=_extract_timestamp(raw),
        kind=kind,
        body=str(_get(event, "text", default="") or ""),
        mxc_url=mxc_url,
        mime=mime,
        duration_sec=duration_sec,
        mentioned_mxids=_extract_mentions(raw),
        is_reaction=False,
    )


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
