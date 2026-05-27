"""Tests for hermes_chat_recorder.name_resolver."""

from __future__ import annotations

import pytest

from hermes_chat_recorder.name_resolver import (
    NameResolver,
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
        ("@matt:plex.matthewhall.com", "matt"),
        ("@annika:srv", "annika"),
        ("@user:server.tld", "user"),
        ("matt", "matt"),  # already a localpart
        ("@bare", "bare"),  # @ without colon
        ("", ""),
    ],
)
def test_mxid_localpart(raw: str, expected: str) -> None:
    assert mxid_localpart(raw) == expected


# ---------------------------------------------------------------------------
# NameResolver — room_slug
# ---------------------------------------------------------------------------


def test_room_slug_uses_room_name_lookup_when_available() -> None:
    r = NameResolver(room_name_lookup=lambda _: "Matt & Annika")
    assert r.room_slug("!abc:srv") == "Matt-and-Annika"


def test_room_slug_falls_back_to_dm_peer_when_room_name_blank() -> None:
    r = NameResolver(
        room_name_lookup=lambda _: None,
        dm_peer_lookup=lambda _: "Annika Reidemeister",
    )
    assert r.room_slug("!abc:srv") == "Annika-Reidemeister"


def test_room_slug_falls_back_to_slug_from_room_id_with_no_lookups() -> None:
    r = NameResolver()
    assert r.room_slug("!abc123:srv") == "abc123"


def test_room_slug_falls_back_when_all_lookups_return_none() -> None:
    r = NameResolver(
        room_name_lookup=lambda _: None,
        dm_peer_lookup=lambda _: None,
    )
    assert r.room_slug("!abc123:srv") == "abc123"


def test_room_slug_caches_result_across_calls() -> None:
    calls: list[str] = []

    def _lookup(room_id: str) -> str:
        calls.append(room_id)
        return "Family"

    r = NameResolver(room_name_lookup=_lookup)
    assert r.room_slug("!abc:srv") == "Family"
    assert r.room_slug("!abc:srv") == "Family"
    assert r.room_slug("!abc:srv") == "Family"
    assert calls == ["!abc:srv"], "lookup must be cached"


def test_room_slug_unknown_room_for_empty_input() -> None:
    r = NameResolver()
    assert r.room_slug("") == "unknown-room"


def test_room_slug_swallows_lookup_exceptions() -> None:
    def _boom(_: str) -> str:
        raise RuntimeError("network down")

    r = NameResolver(room_name_lookup=_boom)
    # Falls back to slug-from-room-id without crashing.
    assert r.room_slug("!abc:srv") == "abc"


def test_room_slug_set_lookups_clears_cache() -> None:
    r = NameResolver()
    # Cache the fallback path.
    assert r.room_slug("!abc:srv") == "abc"
    # Late-bind a real lookup — the previous fallback must be re-resolved.
    r.set_lookups(room_name_lookup=lambda _: "Family")
    assert r.room_slug("!abc:srv") == "Family"


# ---------------------------------------------------------------------------
# NameResolver — user_display
# ---------------------------------------------------------------------------


def test_user_display_uses_profile_lookup() -> None:
    r = NameResolver(user_name_lookup=lambda _: "Matt Hall")
    assert r.user_display("@matt:srv") == "Matt Hall"


def test_user_display_falls_back_to_localpart_when_lookup_blank() -> None:
    r = NameResolver(user_name_lookup=lambda _: None)
    assert r.user_display("@matt:plex.matthewhall.com") == "matt"


def test_user_display_falls_back_to_raw_mxid_when_localpart_empty() -> None:
    # Pathological MXID; localpart resolution would yield empty string.
    r = NameResolver(user_name_lookup=lambda _: None)
    assert r.user_display("@:srv") == "@:srv"


def test_user_display_caches() -> None:
    calls: list[str] = []

    def _lookup(mxid: str) -> str:
        calls.append(mxid)
        return "Matt"

    r = NameResolver(user_name_lookup=_lookup)
    r.user_display("@matt:srv")
    r.user_display("@matt:srv")
    assert calls == ["@matt:srv"]


def test_user_display_empty_mxid_returns_empty() -> None:
    r = NameResolver()
    assert r.user_display("") == ""


def test_user_display_swallows_lookup_exceptions() -> None:
    def _boom(_: str) -> str:
        raise ValueError("network down")

    r = NameResolver(user_name_lookup=_boom)
    assert r.user_display("@matt:srv") == "matt"


# ---------------------------------------------------------------------------
# Manual overrides
# ---------------------------------------------------------------------------


def test_room_override_beats_lookup() -> None:
    """User-supplied override takes precedence over m.room.name."""
    r = NameResolver(
        room_name_lookup=lambda _: "Auto Name",
        room_overrides={"!abc:srv": "Family Group"},
    )
    assert r.room_slug("!abc:srv") == "Family-Group"


def test_room_override_sanitized_to_slug() -> None:
    r = NameResolver(room_overrides={"!abc:srv": "Matt & Annika"})
    assert r.room_slug("!abc:srv") == "Matt-and-Annika"


def test_room_override_only_applies_to_listed_rooms() -> None:
    """Other rooms still go through the normal lookup chain."""
    r = NameResolver(
        room_overrides={"!abc:srv": "Family"},
        room_name_lookup=lambda rid: "Work" if rid == "!xyz:srv" else None,
    )
    assert r.room_slug("!abc:srv") == "Family"
    assert r.room_slug("!xyz:srv") == "Work"


def test_user_override_beats_lookup() -> None:
    r = NameResolver(
        user_name_lookup=lambda mxid: "Profile Name" if mxid == "@other:srv" else None,
        user_overrides={"@signal_abc:srv": "Matt"},
    )
    assert r.user_display("@signal_abc:srv") == "Matt"
    # Other users still go through the normal lookup chain.
    assert r.user_display("@other:srv") == "Profile Name"


def test_user_override_verbatim_no_sanitization() -> None:
    """Section headers carry the display name as-is — no hyphenation."""
    r = NameResolver(user_overrides={"@signal_abc:srv": "Matt H."})
    assert r.user_display("@signal_abc:srv") == "Matt H."


def test_empty_override_string_does_not_apply() -> None:
    """Empty / whitespace overrides fall through to the normal chain."""
    r = NameResolver(
        room_overrides={"!abc:srv": "   "},
        user_overrides={"@matt:srv": ""},
        user_name_lookup=lambda _: "Lookup Name",
    )
    assert r.room_slug("!abc:srv") == "abc"  # falls back to slug-from-id
    assert r.user_display("@matt:srv") == "Lookup Name"
