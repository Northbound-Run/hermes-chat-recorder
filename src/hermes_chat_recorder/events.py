"""Extractor that turns a Hermes gateway ``MessageEvent`` into a typed
:class:`EventInfo` the recorder can persist.

Hermes dispatches a unified ``MessageEvent`` for every platform
(Telegram, Discord, Matrix, Slack, Signal, WhatsApp, IRC, …) with a
``SessionSource`` describing where it came from. The generic path here
reads only those documented unified fields:

* ``event.message_id``, ``event.text``, ``event.message_type``
* ``event.timestamp`` (datetime), ``event.media_urls`` (local cached
  file paths — every adapter downloads media before dispatch)
* ``event.reply_to_message_id``
* ``source.platform`` / ``chat_id`` / ``chat_type`` / ``chat_name`` /
  ``user_id`` / ``user_name``

On top of that, **Matrix events get an enrichment pass** that reads the
raw mautrix event for things the unified shape doesn't carry: the
``origin_server_ts`` server timestamp, ``mxc://`` URLs, audio duration,
reaction flagging, and ``m.replace`` edit relations. (Hermes's current
Matrix adapter filters edits and reactions before dispatch, so those
guards are dormant defense — they keep the recorder correct if an
older or future adapter passes them through.)

This module is the ONLY place in the package that touches Hermes
event shapes directly. Everything downstream operates on
:class:`EventInfo`, so the recorder + writer stay testable without a
running gateway, and there is one file to update when upstream fields
move.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from hermes_chat_recorder.types import MessageKind

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventInfo:
    """Normalized chat event the recorder operates on."""

    platform: str            # "matrix", "telegram", "discord", …
    event_id: str            # platform message id, unique per platform
    chat_id: str
    sender_id: str
    sender_display: str      # display-name hint; may equal sender_id
    timestamp: datetime      # tz-aware
    kind: MessageKind
    body: str
    # Chat metadata hints from the unified SessionSource. Either may be
    # empty — adapters populate them best-effort.
    chat_name: str = ""
    chat_type: str = ""      # "dm", "group", "channel", "thread"
    # Local filesystem path of the already-downloaded (and decrypted,
    # for E2EE rooms) media bytes. Populated from
    # ``MessageEvent.media_urls[0]`` — every Hermes adapter caches
    # media locally before dispatch.
    media_path: str | None = None
    mime: str | None = None
    duration_sec: int | None = None
    reply_to_id: str | None = None
    # --- Matrix-only enrichment below ---
    mxc_url: str | None = None
    is_reaction: bool = False
    # Matrix edits arrive as their own event with an ``m.relates_to``
    # block whose ``rel_type`` is ``m.replace``. When this event IS an
    # edit, ``is_edit`` is True and ``replaced_event_id`` points back
    # at the original message; ``body`` contains the new content
    # (stripped of the ``* `` fallback prefix Matrix clients add).
    is_edit: bool = False
    replaced_event_id: str | None = None


# Hermes's ``MessageType`` enum members, mapped to our storage kinds.
# VIDEO / DOCUMENT / LOCATION are recorded without processing;
# COMMAND is conversational text (e.g. "/new"). Types not listed here
# (and any future additions) are skipped with a debug log.
_KIND_BY_TYPE_NAME: dict[str, MessageKind] = {
    "TEXT": "text",
    "COMMAND": "text",
    "AUDIO": "voice",
    "VOICE": "voice",
    "IMAGE": "image",
    "PHOTO": "image",
    "STICKER": "image",
    "VIDEO": "video",
    "DOCUMENT": "file",
    "LOCATION": "location",
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


def _platform_value(source: Any) -> str:
    """Normalize ``source.platform`` (enum or string) to its lowercase value."""
    if source is None:
        return ""
    platform = _get(source, "platform")
    if platform is None:
        return ""
    value = getattr(platform, "value", None) or str(platform)
    return str(value).lower()


def _extract_timestamp(event: Any, raw_message: Any, platform: str) -> datetime:
    """Return a tz-aware timestamp for the event.

    Matrix: prefer the raw event's ``origin_server_ts`` (milliseconds
    since epoch, UTC) — it's the homeserver's authoritative time.

    All platforms: fall back to the unified ``event.timestamp``
    datetime. Hermes populates it with naive local wall-clock time
    (``datetime.now()``), so a naive value is interpreted as system
    local time via ``astimezone()`` — the writer then converts to its
    configured zone. Last resort is current UTC time.
    """
    if platform == "matrix":
        ts_ms = _get(raw_message, "origin_server_ts", "originServerTs", "timestamp_ms")
        if isinstance(ts_ms, (int, float)):
            return datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)

    ts = _get(event, "timestamp")
    if isinstance(ts, datetime):
        # Naive values are assumed to be SYSTEM-LOCAL wall-clock time
        # (argless astimezone attaches the host zone). Correct as long
        # as the recorder runs in the same process — and therefore the
        # same system zone — as the adapter that stamped the event.
        return ts.astimezone() if ts.tzinfo is None else ts
    return datetime.now(UTC)


def _extract_local_media_path(event: Any, kind: MessageKind) -> str | None:
    """Pull a locally-cached media path off the Hermes ``MessageEvent``.

    Every adapter populates ``MessageEvent.media_urls = [cached_path]``
    after downloading (and decrypting, in E2EE rooms) the media bytes.
    Remote URLs are rejected — the recorder only reads local files.
    """
    # Location messages are text coordinates; any map thumbnail in
    # media_urls is platform-generated chrome, not user content.
    if kind not in ("voice", "image", "video", "file"):
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


def _extract_reply_to(event: Any) -> str | None:
    val = _get(event, "reply_to_message_id")
    if isinstance(val, str) and val:
        return val
    return None


def _synthesize_event_id(sender_id: str, raw_message: Any, timestamp: Any) -> str:
    """Build a stable event ID for adapters that don't set message_id.

    Prefers the platform's own millisecond timestamp off the raw
    payload (Signal ships ``timestamp_ms``; the value IS the protocol's
    message identity together with the sender), falling back to the
    extracted event timestamp. Both are redelivery-stable, so the vault
    anchor dedupes replays exactly like a real message ID would. The
    ``syn:`` prefix keeps synthesized anchors greppable.
    """
    ts_ms = _get(raw_message, "timestamp_ms", "origin_server_ts")
    millis = (
        int(ts_ms)
        if isinstance(ts_ms, (int, float))
        else int(timestamp.timestamp() * 1000)
    )
    return f"syn:{sender_id}:{millis}"


def extract(event: Any) -> EventInfo | None:
    """Pull a normalized :class:`EventInfo` off a Hermes ``MessageEvent``.

    Returns ``None`` when the event can't be recorded (no platform, no
    chat/sender, unsupported message type, …). A missing ``message_id``
    is NOT fatal — some adapters (Signal) never set one, so a stable ID
    is synthesized from sender + timestamp instead. The recorder uses
    ``None`` as the signal to bail out early without touching the
    vault.
    """
    source = _get(event, "source")
    platform = _platform_value(source)
    if not platform:
        return None

    raw = _get(event, "raw_message")

    # Matrix reactions are flagged so the recorder can skip them.
    if platform == "matrix" and _is_matrix_reaction(raw):
        return EventInfo(
            platform=platform,
            event_id=str(_get(event, "message_id", default="")),
            chat_id=str(_get(source, "chat_id", default="")),
            sender_id=str(_get(source, "user_id", default="")),
            sender_display="",
            timestamp=_extract_timestamp(event, raw, platform),
            kind="text",
            body="",
            is_reaction=True,
        )

    kind = _kind_from_message_type(_get(event, "message_type"))
    if kind is None:
        logger.debug(
            "hermes_chat_recorder: skipping unsupported message_type %r on %s",
            _get(event, "message_type"),
            platform,
        )
        return None

    chat_id = _get(source, "chat_id")
    user_id = _get(source, "user_id")
    if not chat_id or not user_id:
        return None

    sender_id = str(user_id)
    timestamp = _extract_timestamp(event, raw, platform)
    event_id = _get(event, "message_id")
    if not event_id:
        # Some adapters never set message_id — Signal, for one,
        # identifies a message by (sender, timestamp_ms) and ships
        # exactly those in raw_message. Synthesize a stable ID from the
        # same attributes so redelivery still dedupes against the
        # existing vault anchor.
        event_id = _synthesize_event_id(sender_id, raw, timestamp)

    sender_display = _get(source, "user_name", default="") or ""
    chat_name = _get(source, "chat_name", default="") or ""
    chat_type = str(_get(source, "chat_type", default="") or "")

    media_path = _extract_local_media_path(event, kind)
    body = str(_get(event, "text", default="") or "")

    info = EventInfo(
        platform=platform,
        event_id=str(event_id),
        chat_id=str(chat_id),
        sender_id=sender_id,
        sender_display=str(sender_display) or sender_id,
        timestamp=timestamp,
        kind=kind,
        body=body,
        chat_name=str(chat_name),
        chat_type=chat_type,
        media_path=media_path,
        reply_to_id=_extract_reply_to(event),
    )

    if platform == "matrix":
        info = _enrich_matrix(info, event, raw)
    return info


# ---------------------------------------------------------------------------
# Matrix enrichment — reads the raw mautrix event
# ---------------------------------------------------------------------------


def _enrich_matrix(info: EventInfo, event: Any, raw: Any) -> EventInfo:
    """Layer Matrix-only fields onto a generic :class:`EventInfo`.

    Reads the raw mautrix event for the mxc URL, mime type, audio
    duration, edit relations, and a richer sender display name. Returns
    a new (frozen) EventInfo.
    """
    from dataclasses import replace

    mxc_url, mime, duration_sec = _extract_mxc_and_media(raw, info.kind)
    is_edit, replaced_event_id, edited_body = _extract_edit_relation(raw)

    sender_display = info.sender_display
    raw_display = _get(raw, "sender_display_name", "displayname")
    if isinstance(raw_display, str) and raw_display.strip():
        sender_display = raw_display.strip()

    return replace(
        info,
        sender_display=sender_display or info.sender_id,
        body=edited_body or info.body,
        mxc_url=mxc_url,
        mime=mime if mime is not None else info.mime,
        duration_sec=duration_sec,
        is_edit=is_edit,
        replaced_event_id=replaced_event_id,
    )


def _is_matrix_reaction(raw_message: Any) -> bool:
    """True if the raw event is an m.reaction (we don't record these)."""
    event_type = _get(raw_message, "type")
    if event_type is None:
        return False
    value = getattr(event_type, "value", None) or str(event_type)
    return str(value).lower() == "m.reaction"


def _extract_mxc_and_media(
    raw_message: Any, kind: MessageKind
) -> tuple[str | None, str | None, int | None]:
    """Return (mxc_url, mime, duration_sec) from a Matrix event's content.

    Handles two shapes:

    * **Plaintext rooms** — ``content.url`` is the mxc.
    * **End-to-end encrypted rooms** — ``content.file.url`` is the mxc;
      ``content.file`` also carries ``key``, ``iv``, ``hashes``, ``v``.
      We record the URL either way; the recorder reads media bytes from
      the adapter's locally-cached (already decrypted) file, so the
      mxc is provenance metadata, not a download source.
    """
    # Location messages are text coordinates; any map thumbnail in
    # media_urls is platform-generated chrome, not user content.
    if kind not in ("voice", "image", "video", "file"):
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


# ---------------------------------------------------------------------------
# ID → slug fallback
# ---------------------------------------------------------------------------


def slug_from_chat_id(chat_id: str) -> str:
    """Derive a filesystem-safe slug from a raw chat ID.

    The last-resort fallback when no chat name is available. Matrix
    room IDs (``!abcdef:server`` / ``#name:server``) lose their sigil
    and ``:server`` suffix; other platforms' IDs (Telegram numeric,
    Discord snowflakes, Signal ``group:<base64>``, …) keep their full
    value — the ``:server`` strip applies ONLY to sigil-prefixed
    Matrix IDs, because a bare colon is meaningful elsewhere (a Signal
    group ID split at ``:`` would collapse every group to ``group``).
    Anything unsafe for a filename is sanitized to alnum + hyphen.
    """
    if not chat_id:
        return "unknown-chat"
    s = chat_id
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
    cleaned = "".join(safe).strip("-") or "unknown-chat"
    return cleaned
