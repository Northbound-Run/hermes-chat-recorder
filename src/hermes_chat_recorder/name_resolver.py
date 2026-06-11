"""Resolve chat IDs and user IDs to human-readable names.

Most of the time the gateway hands us names for free: every Hermes
adapter populates ``SessionSource.chat_name`` and ``user_name``
best-effort, and the recorder passes those through as *hints*. The
resolver's job is to pick the best available name per ID and keep it
stable for the process lifetime:

* explicit config override → wins always
* cached prior resolution → keeps one chat in one folder even if the
  chat is renamed mid-run
* per-event hint (``chat_name`` / ``user_name`` from the adapter)
* injected lookup callables — used for Matrix, where the live client
  can resolve ``m.room.name`` / profile display names / DM peers that
  the unified event may lack
* ID-derived fallback (``slug_from_chat_id`` / localpart)

Caches are scoped by platform so a Telegram chat ID can never collide
with a Discord snowflake. There is no TTL — name changes mid-session
are not picked up until the next process restart, which is an
intentional tradeoff (stable folders beat fresh names for an archive).
Restart the gateway to refresh.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable

from hermes_chat_recorder.events import slug_from_chat_id

logger = logging.getLogger(__name__)


Lookup = Callable[[str], "str | None"]


_UNSAFE_RUN = re.compile(r"[^A-Za-z0-9_\-]+")
_HYPHEN_RUN = re.compile(r"-+")


def sanitize_slug(name: str, *, max_length: int = 64) -> str:
    """Turn a display name into a filesystem-safe folder slug.

    "Family & Friends" → "Family-and-Friends"
    "Family Chat" → "Family-Chat"
    "  /weird:name  " → "weird-name"
    """
    if not name:
        return ""
    # Replace common punctuation with readable equivalents BEFORE the
    # generic alnum-only sweep, so "&" doesn't collapse to nothing.
    s = name.replace("&", " and ")
    s = _UNSAFE_RUN.sub("-", s)
    s = _HYPHEN_RUN.sub("-", s).strip("-")
    if not s:
        return ""
    if len(s) > max_length:
        s = s[:max_length].rstrip("-")
    return s


def mxid_localpart(mxid: str) -> str:
    """Return the localpart of a Matrix ID: ``@user:server`` → ``user``.

    Empty input → empty string. No ``@`` or ``:`` → return the whole
    string (which makes this a safe no-op for non-Matrix user IDs).
    """
    if not mxid:
        return ""
    s = mxid
    if s.startswith("@"):
        s = s[1:]
    if ":" in s:
        s = s.split(":", 1)[0]
    return s


def looks_like_id(s: str) -> bool:
    """Heuristic: does this string look like a raw platform ID rather
    than a human display name? Covers MXIDs (``@user:server``), Matrix
    room IDs (``!room:server``), and purely numeric IDs (Telegram chat
    IDs, Discord snowflakes)."""
    if not s:
        return True
    if s.startswith(("@", "!", "#")) and ":" in s:
        return True
    return s.lstrip("-").isdigit()


class NameResolver:
    """Caches resolved chat/user names per process.

    Construct once and share. Thread-safe; the injected lookup callables
    may block (they typically dispatch to the background asyncio loop
    in production).
    """

    def __init__(
        self,
        *,
        room_name_lookup: Lookup | None = None,
        user_name_lookup: Lookup | None = None,
        dm_peer_lookup: Lookup | None = None,
        room_overrides: dict[str, str] | None = None,
        user_overrides: dict[str, str] | None = None,
    ) -> None:
        # Lookup callables are Matrix-only today: plugin.py wires them
        # to the live mautrix client. Other platforms rely on hints.
        self._room_name_lookup = room_name_lookup
        self._user_name_lookup = user_name_lookup
        self._dm_peer_lookup = dm_peer_lookup
        # Explicit user-supplied overrides win over hints and lookups.
        # Useful for bridge users (Signal, WhatsApp) who never get a
        # display name set, and for renaming a chat's folder. Keyed by
        # the raw platform ID.
        self._room_overrides = dict(room_overrides or {})
        self._user_overrides = dict(user_overrides or {})
        # Caches keyed by "platform:raw_id".
        self._chat_slug_cache: dict[str, str] = {}
        self._user_display_cache: dict[str, str] = {}
        self._lock = threading.Lock()

    def set_lookups(
        self,
        *,
        room_name_lookup: Lookup | None = None,
        user_name_lookup: Lookup | None = None,
        dm_peer_lookup: Lookup | None = None,
    ) -> None:
        """Late-bind the lookup functions.

        Called from :func:`plugin._wire_adapters` once the live Matrix
        client is available. Clears caches so any previously-resolved
        fallback values get re-resolved with the real lookups.
        """
        # The attribute swaps happen under the same lock as the cache
        # clear so a concurrent resolver call can't cache a result
        # computed with a half-replaced lookup generation. (Resolution
        # itself still runs outside the lock — see chat_slug — which is
        # a deliberate tradeoff to avoid holding a lock across network
        # I/O; the worst case there is a redundant duplicate lookup.)
        with self._lock:
            if room_name_lookup is not None:
                self._room_name_lookup = room_name_lookup
            if user_name_lookup is not None:
                self._user_name_lookup = user_name_lookup
            if dm_peer_lookup is not None:
                self._dm_peer_lookup = dm_peer_lookup
            self._chat_slug_cache.clear()
            self._user_display_cache.clear()

    def chat_slug(
        self,
        platform: str,
        chat_id: str,
        *,
        name_hint: str = "",
        peer_hint: str = "",
    ) -> str:
        """Filesystem-safe folder slug for a chat.

        Resolution order: explicit override → cache → ``name_hint``
        (the adapter's ``chat_name``) → Matrix room-name lookup →
        ``peer_hint`` (sender display, for unnamed DMs) → Matrix
        DM-peer lookup → ``slug_from_chat_id`` fallback. The first
        resolution is cached for the process lifetime.
        """
        if not chat_id:
            return "unknown-chat"

        cache_key = f"{platform}:{chat_id}"
        with self._lock:
            cached = self._chat_slug_cache.get(cache_key)
        if cached is not None:
            return cached

        slug = ""
        override = self._room_overrides.get(chat_id)
        if override:
            slug = sanitize_slug(override)
        if not slug and name_hint and not looks_like_id(name_hint):
            slug = sanitize_slug(name_hint)
        if not slug and platform == "matrix" and self._room_name_lookup is not None:
            resolved = self._safe_lookup(self._room_name_lookup, chat_id, "room_name")
            slug = sanitize_slug(resolved) if resolved else ""
        if not slug and peer_hint and not looks_like_id(peer_hint):
            slug = sanitize_slug(peer_hint)
        if not slug and platform == "matrix" and self._dm_peer_lookup is not None:
            resolved = self._safe_lookup(self._dm_peer_lookup, chat_id, "dm_peer")
            slug = sanitize_slug(resolved) if resolved else ""
        if not slug:
            slug = slug_from_chat_id(chat_id)

        with self._lock:
            self._chat_slug_cache[cache_key] = slug
        return slug

    def user_display(self, platform: str, user_id: str, *, hint: str = "") -> str:
        """Human-readable display name for a user.

        Resolution order: explicit override → cache → ``hint`` (the
        adapter's ``user_name``) → Matrix profile lookup → ID localpart
        → raw ID (last resort, never empty for a non-empty input). The
        first resolution is cached for the process lifetime.
        """
        if not user_id:
            return ""

        cache_key = f"{platform}:{user_id}"
        with self._lock:
            cached = self._user_display_cache.get(cache_key)
        if cached is not None:
            return cached

        display = ""
        # Overrides take the human-readable string verbatim — no
        # sanitization, because section headers carry the name as-is.
        override = self._user_overrides.get(user_id)
        if override and override.strip():
            display = override.strip()
        if not display and hint and hint != user_id and not looks_like_id(hint):
            display = hint.strip()
        if not display and platform == "matrix" and self._user_name_lookup is not None:
            display = self._safe_lookup(self._user_name_lookup, user_id, "user_name") or ""
        if not display:
            display = mxid_localpart(user_id) or user_id

        with self._lock:
            self._user_display_cache[cache_key] = display
        return display

    @staticmethod
    def _safe_lookup(fn: Lookup, key: str, kind: str) -> str | None:
        try:
            result = fn(key)
        except Exception as exc:
            logger.warning(
                "hermes_chat_recorder: %s lookup failed for %s: %s",
                kind,
                key,
                exc,
            )
            return None
        if isinstance(result, str) and result.strip():
            return result.strip()
        return None
