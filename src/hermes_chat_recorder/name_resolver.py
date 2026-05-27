"""Resolve Matrix room IDs and MXIDs to human-readable names.

The resolver is intentionally agnostic about HOW names are fetched —
callers inject sync callables that take a Matrix ID and return
``Optional[str]``. :mod:`plugin` wires these to the live Matrix client
via the existing background-loop sync↔async bridge; tests inject
plain Python stubs.

All results are cached for the process lifetime. The cache is keyed by
the canonical Matrix ID (room_id or MXID) so resolutions persist for
the gateway's whole run. There is no TTL — name changes mid-session
will not be picked up until the next process restart, which is an
intentional tradeoff for an alpha tool (avoids server pressure and the
complexity of state-event listeners). Restart the container to refresh.

Fallback chain when a lookup returns nothing:

* room → m.room.name → DM-peer display name → ``room_slug_from_room_id``
* user → profile displayname → MXID localpart → raw MXID
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable

from hermes_chat_recorder.matrix_event import room_slug_from_room_id

logger = logging.getLogger(__name__)


Lookup = Callable[[str], "str | None"]


_UNSAFE_RUN = re.compile(r"[^A-Za-z0-9_\-]+")
_HYPHEN_RUN = re.compile(r"-+")


def sanitize_slug(name: str, *, max_length: int = 64) -> str:
    """Turn a display name into a filesystem-safe folder slug.

    "Matt & Annika" → "Matt-and-Annika"
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
    """Return the localpart of an MXID: ``@matt:server`` → ``matt``.

    Empty input → empty string. No ``@`` or ``:`` → return the whole
    string. Used as a fallback when profile display-name lookup fails.
    """
    if not mxid:
        return ""
    s = mxid
    if s.startswith("@"):
        s = s[1:]
    if ":" in s:
        s = s.split(":", 1)[0]
    return s


class NameResolver:
    """Caches resolved room/user names per process.

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
        self._room_name_lookup = room_name_lookup
        self._user_name_lookup = user_name_lookup
        self._dm_peer_lookup = dm_peer_lookup
        # Explicit user-supplied overrides win over any Matrix lookup.
        # Useful for bridge users (Signal, WhatsApp) who never get a
        # display name set on the homeserver, and for power users who
        # just want to rename a room.
        self._room_overrides = dict(room_overrides or {})
        self._user_overrides = dict(user_overrides or {})
        self._room_slug_cache: dict[str, str] = {}
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

        Called from :func:`plugin._wire_matrix_adapter` once the live
        Matrix client is available. Clears caches so any
        previously-resolved fallback values get re-resolved with the
        real lookups.
        """
        if room_name_lookup is not None:
            self._room_name_lookup = room_name_lookup
        if user_name_lookup is not None:
            self._user_name_lookup = user_name_lookup
        if dm_peer_lookup is not None:
            self._dm_peer_lookup = dm_peer_lookup
        with self._lock:
            self._room_slug_cache.clear()
            self._user_display_cache.clear()

    def room_slug(self, room_id: str) -> str:
        """Filesystem-safe folder slug for a room.

        Resolution order: explicit override → m.room.name → DM peer
        display name → ``room_slug_from_room_id`` fallback. Result is
        cached for the process lifetime.
        """
        if not room_id:
            return "unknown-room"

        with self._lock:
            cached = self._room_slug_cache.get(room_id)
        if cached is not None:
            return cached

        # User-supplied overrides win — sanitized to a filesystem-safe
        # slug exactly like an auto-resolved name.
        override = self._room_overrides.get(room_id)
        if override:
            slug = sanitize_slug(override) or room_slug_from_room_id(room_id)
            with self._lock:
                self._room_slug_cache[room_id] = slug
            return slug

        resolved: str | None = None
        if self._room_name_lookup is not None:
            resolved = self._safe_lookup(self._room_name_lookup, room_id, "room_name")
        if not resolved and self._dm_peer_lookup is not None:
            resolved = self._safe_lookup(self._dm_peer_lookup, room_id, "dm_peer")

        slug = sanitize_slug(resolved) if resolved else ""
        if not slug:
            slug = room_slug_from_room_id(room_id)

        with self._lock:
            self._room_slug_cache[room_id] = slug
        return slug

    def user_display(self, mxid: str) -> str:
        """Human-readable display name for a user.

        Resolution order: explicit override → injected profile lookup →
        MXID localpart → raw MXID (last resort, never empty for a
        non-empty input). Result is cached for the process lifetime.
        """
        if not mxid:
            return ""

        with self._lock:
            cached = self._user_display_cache.get(mxid)
        if cached is not None:
            return cached

        # Overrides take the human-readable string verbatim — no
        # sanitization, because section headers carry the name as-is.
        override = self._user_overrides.get(mxid)
        if override and override.strip():
            display = override.strip()
            with self._lock:
                self._user_display_cache[mxid] = display
            return display

        resolved: str | None = None
        if self._user_name_lookup is not None:
            resolved = self._safe_lookup(self._user_name_lookup, mxid, "user_name")

        display = (resolved or "").strip()
        if not display:
            display = mxid_localpart(mxid) or mxid

        with self._lock:
            self._user_display_cache[mxid] = display
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
