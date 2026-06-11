"""Tests for hermes_chat_recorder.name_resolver."""

from __future__ import annotations

import pytest

from hermes_chat_recorder.name_resolver import (
    NameResolver,
    looks_like_id,
    mxid_localpart,
    sanitize_slug,
)

# ---------------------------------------------------------------------------
# sanitize_slug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Matt & Annika", "Matt-and-Annika"),
        ("Family Chat", "Family-Chat"),
        ("  /weird:name  ", "weird-name"),
        ("hello", "hello"),
        ("Already-Hyphenated", "Already-Hyphenated"),
        ("multiple   spaces", "multiple-spaces"),
        ("a---b", "a-b"),
        ("---trim---", "trim"),
        ("Underscores_ok", "Underscores_ok"),
        ("emoji 🚀 stripped", "emoji-stripped"),
        ("Guild / #general", "Guild-general"),  # Discord chat_name shape
    ],
)
def test_sanitize_slug_common_cases(raw: str, expected: str) -> None:
    assert sanitize_slug(raw) == expected


def test_sanitize_slug_empty_input_returns_empty() -> None:
    assert sanitize_slug("") == ""
    assert sanitize_slug("///") == ""
    assert sanitize_slug("   ") == ""


def test_sanitize_slug_truncates_long_input() -> None:
    long = "x" * 200
    assert len(sanitize_slug(long, max_length=64)) == 64


def test_sanitize_slug_truncate_does_not_leave_trailing_hyphen() -> None:
    # If the truncation point lands ON a hyphen, strip it.
    raw = "abcdef-" + "x" * 100
    out = sanitize_slug(raw, max_length=7)  # would truncate to "abcdef-"
    assert out == "abcdef"


# ---------------------------------------------------------------------------
# mxid_localpart
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("@user:home.example.com", "user"),
        ("@annika:srv", "annika"),
        ("user", "user"),  # already a localpart — non-Matrix IDs pass through
        ("@bare", "bare"),  # @ without colon
        ("", ""),
    ],
)
def test_mxid_localpart(raw: str, expected: str) -> None:
    assert mxid_localpart(raw) == expected


# ---------------------------------------------------------------------------
# looks_like_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("@user:srv", True),          # MXID
        ("!room:srv", True),          # Matrix room ID
        ("#chan:srv", True),          # Matrix alias
        ("5551212", True),            # Telegram numeric user id
        ("-1001234", True),           # Telegram group id
        ("", True),                   # empty → not a usable name
        ("Annika", False),
        ("Family Chat", False),
        ("nick42", False),            # IRC nick with digits is a name
    ],
)
def test_looks_like_id(raw: str, expected: bool) -> None:
    assert looks_like_id(raw) is expected


# ---------------------------------------------------------------------------
# NameResolver — chat_slug
# ---------------------------------------------------------------------------


def test_chat_slug_uses_name_hint() -> None:
    r = NameResolver()
    assert r.chat_slug("telegram", "-1001234", name_hint="Family Chat") == "Family-Chat"


def test_chat_slug_ignores_id_shaped_hint() -> None:
    """Adapters sometimes echo the raw ID into chat_name; that must not
    become the folder name when a better fallback exists."""
    r = NameResolver()
    assert r.chat_slug("matrix", "!abc:srv", name_hint="!abc:srv") == "abc"


def test_chat_slug_uses_matrix_room_lookup_when_no_hint() -> None:
    r = NameResolver(room_name_lookup=lambda _: "Matt & Annika")
    assert r.chat_slug("matrix", "!abc:srv") == "Matt-and-Annika"


def test_chat_slug_matrix_lookup_not_consulted_for_other_platforms() -> None:
    calls: list[str] = []

    def _lookup(chat_id: str) -> str:
        calls.append(chat_id)
        return "Should Not Appear"

    r = NameResolver(room_name_lookup=_lookup)
    assert r.chat_slug("telegram", "-100999") == "100999"
    assert calls == []


def test_chat_slug_falls_back_to_dm_peer_lookup() -> None:
    r = NameResolver(
        room_name_lookup=lambda _: None,
        dm_peer_lookup=lambda _: "Annika Reidemeister",
    )
    assert r.chat_slug("matrix", "!abc:srv") == "Annika-Reidemeister"


def test_chat_slug_uses_peer_hint_for_unnamed_dms() -> None:
    r = NameResolver()
    assert r.chat_slug("telegram", "5551212", peer_hint="Annika") == "Annika"


def test_chat_slug_name_hint_beats_peer_hint() -> None:
    r = NameResolver()
    assert (
        r.chat_slug("telegram", "1", name_hint="Family Chat", peer_hint="Annika")
        == "Family-Chat"
    )


def test_chat_slug_falls_back_to_id_slug_with_no_hints_or_lookups() -> None:
    r = NameResolver()
    assert r.chat_slug("matrix", "!abc123:srv") == "abc123"


def test_chat_slug_caches_result_across_calls() -> None:
    calls: list[str] = []

    def _lookup(chat_id: str) -> str:
        calls.append(chat_id)
        return "Family"

    r = NameResolver(room_name_lookup=_lookup)
    assert r.chat_slug("matrix", "!abc:srv") == "Family"
    assert r.chat_slug("matrix", "!abc:srv") == "Family"
    assert calls == ["!abc:srv"], "lookup must be cached"


def test_chat_slug_cache_keeps_first_resolution_stable() -> None:
    """A renamed chat must not split the archive mid-process: the first
    resolved slug wins until restart."""
    r = NameResolver()
    assert r.chat_slug("telegram", "7", name_hint="Old Name") == "Old-Name"
    assert r.chat_slug("telegram", "7", name_hint="New Name") == "Old-Name"


def test_chat_slug_cache_scoped_by_platform() -> None:
    """The same raw chat ID on two platforms is two different chats."""
    r = NameResolver()
    assert r.chat_slug("telegram", "12345", name_hint="TG Chat") == "TG-Chat"
    assert r.chat_slug("discord", "12345", name_hint="DC Chat") == "DC-Chat"


def test_chat_slug_unknown_chat_for_empty_input() -> None:
    r = NameResolver()
    assert r.chat_slug("matrix", "") == "unknown-chat"


def test_chat_slug_swallows_lookup_exceptions() -> None:
    def _boom(_: str) -> str:
        raise RuntimeError("network down")

    r = NameResolver(room_name_lookup=_boom)
    # Falls back to slug-from-chat-id without crashing.
    assert r.chat_slug("matrix", "!abc:srv") == "abc"


def test_chat_slug_set_lookups_clears_cache() -> None:
    r = NameResolver()
    # Cache the fallback path.
    assert r.chat_slug("matrix", "!abc:srv") == "abc"
    # Late-bind a real lookup — the previous fallback must be re-resolved.
    r.set_lookups(room_name_lookup=lambda _: "Family")
    assert r.chat_slug("matrix", "!abc:srv") == "Family"


# ---------------------------------------------------------------------------
# NameResolver — user_display
# ---------------------------------------------------------------------------


def test_user_display_uses_hint() -> None:
    r = NameResolver()
    assert r.user_display("telegram", "5551212", hint="Annika") == "Annika"


def test_user_display_ignores_id_shaped_hint() -> None:
    r = NameResolver()
    assert r.user_display("matrix", "@annika:srv", hint="@annika:srv") == "annika"


def test_user_display_uses_matrix_profile_lookup() -> None:
    r = NameResolver(user_name_lookup=lambda _: "Annika R.")
    assert r.user_display("matrix", "@annika:srv") == "Annika R."


def test_user_display_matrix_lookup_not_consulted_for_other_platforms() -> None:
    calls: list[str] = []

    def _lookup(user_id: str) -> str:
        calls.append(user_id)
        return "Nope"

    r = NameResolver(user_name_lookup=_lookup)
    assert r.user_display("telegram", "5551212") == "5551212"
    assert calls == []


def test_user_display_falls_back_to_localpart_when_lookup_blank() -> None:
    r = NameResolver(user_name_lookup=lambda _: None)
    assert r.user_display("matrix", "@annika:home.example.com") == "annika"


def test_user_display_falls_back_to_raw_id_when_localpart_empty() -> None:
    # Pathological MXID; localpart resolution would yield empty string.
    r = NameResolver(user_name_lookup=lambda _: None)
    assert r.user_display("matrix", "@:srv") == "@:srv"


def test_user_display_caches() -> None:
    calls: list[str] = []

    def _lookup(user_id: str) -> str:
        calls.append(user_id)
        return "Annika"

    r = NameResolver(user_name_lookup=_lookup)
    r.user_display("matrix", "@annika:srv")
    r.user_display("matrix", "@annika:srv")
    assert calls == ["@annika:srv"]


def test_user_display_cache_scoped_by_platform() -> None:
    r = NameResolver()
    assert r.user_display("telegram", "777", hint="TG Person") == "TG Person"
    assert r.user_display("discord", "777", hint="DC Person") == "DC Person"


def test_user_display_empty_id_returns_empty() -> None:
    r = NameResolver()
    assert r.user_display("matrix", "") == ""


def test_user_display_swallows_lookup_exceptions() -> None:
    def _boom(_: str) -> str:
        raise ValueError("network down")

    r = NameResolver(user_name_lookup=_boom)
    assert r.user_display("matrix", "@annika:srv") == "annika"


# ---------------------------------------------------------------------------
# Manual overrides
# ---------------------------------------------------------------------------


def test_chat_override_beats_hint_and_lookup() -> None:
    r = NameResolver(
        room_name_lookup=lambda _: "Auto Name",
        room_overrides={"!abc:srv": "Family Group"},
    )
    assert r.chat_slug("matrix", "!abc:srv", name_hint="Hint Name") == "Family-Group"


def test_chat_override_sanitized_to_slug() -> None:
    r = NameResolver(room_overrides={"!abc:srv": "Matt & Annika"})
    assert r.chat_slug("matrix", "!abc:srv") == "Matt-and-Annika"


def test_chat_override_only_applies_to_listed_chats() -> None:
    """Other chats still go through the normal resolution chain."""
    r = NameResolver(
        room_overrides={"!abc:srv": "Family"},
        room_name_lookup=lambda cid: "Work" if cid == "!xyz:srv" else None,
    )
    assert r.chat_slug("matrix", "!abc:srv") == "Family"
    assert r.chat_slug("matrix", "!xyz:srv") == "Work"


def test_chat_override_works_for_any_platform() -> None:
    """Overrides are keyed by raw chat ID, platform-agnostic — a
    Telegram group ID is just as overridable as a Matrix room."""
    r = NameResolver(room_overrides={"-1001234": "Signal Bridge"})
    assert r.chat_slug("telegram", "-1001234") == "Signal-Bridge"


def test_user_override_beats_hint_and_lookup() -> None:
    r = NameResolver(
        user_name_lookup=lambda uid: "Profile Name" if uid == "@other:srv" else None,
        user_overrides={"@signal_abc:srv": "Annika"},
    )
    assert r.user_display("matrix", "@signal_abc:srv", hint="Wrong") == "Annika"
    # Other users still go through the normal lookup chain.
    assert r.user_display("matrix", "@other:srv") == "Profile Name"


def test_user_override_verbatim_no_sanitization() -> None:
    """Section headers carry the display name as-is — no hyphenation."""
    r = NameResolver(user_overrides={"@signal_abc:srv": "Annika R."})
    assert r.user_display("matrix", "@signal_abc:srv") == "Annika R."


def test_empty_override_string_does_not_apply() -> None:
    """Empty / whitespace overrides fall through to the normal chain."""
    r = NameResolver(
        room_overrides={"!abc:srv": "   "},
        user_overrides={"@annika:srv": ""},
        user_name_lookup=lambda _: "Lookup Name",
    )
    assert r.chat_slug("matrix", "!abc:srv") == "abc"  # falls back to id slug
    assert r.user_display("matrix", "@annika:srv") == "Lookup Name"
