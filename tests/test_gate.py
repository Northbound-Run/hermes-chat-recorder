"""Tests for the wake gate.

Covers the four gating paths: @-mention, nickname match, self-echo
guard, and the default-no-wake fall-through. Also the strictness of
the nickname allowlist (no `ralphing`, `ralphson`, etc.).
"""

from __future__ import annotations

import pytest

from hermes_chat_recorder.gate import Gate
from hermes_chat_recorder.types import GateInput


BOT = "@ralph:server"
ANNIKA = "@annika:server"


def _g(*, text: str = "", mentions: frozenset[str] = frozenset(), sender: str = ANNIKA) -> GateInput:
    return GateInput(
        gate_text=text, bot_mxid=BOT, sender_mxid=sender, mentioned_mxids=mentions
    )


# ---------------------------------------------------------------------------
# Construction / allowlist normalization
# ---------------------------------------------------------------------------


def test_nickname_allowlist_is_normalized_and_deduped() -> None:
    gate = Gate(["Ralph", "ralph", "  ralphy  ", "", "ralphie", "RALPHIE"])
    # Order-preserving dedupe on the lowercase form.
    assert gate.nicknames == ("Ralph", "ralphy", "ralphie")


def test_empty_nickname_list_disables_nickname_path() -> None:
    gate = Gate([])
    # Only the @-mention path can wake him now.
    assert gate.should_wake(_g(text="hey ralph", mentions=frozenset())) is False
    assert gate.should_wake(_g(text="hey", mentions=frozenset({BOT}))) is True


# ---------------------------------------------------------------------------
# Mention path
# ---------------------------------------------------------------------------


def test_at_mention_wakes_regardless_of_text() -> None:
    gate = Gate(["ralph"])
    assert gate.should_wake(_g(text="anything", mentions=frozenset({BOT}))) is True


def test_at_mention_of_a_different_mxid_does_not_wake() -> None:
    gate = Gate(["ralph"])
    other = "@elsewhere:server"
    assert gate.should_wake(_g(text="hi", mentions=frozenset({other}))) is False


# ---------------------------------------------------------------------------
# Nickname path — allowlist must be strict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "ralph",
        "Ralph",
        "RALPH",
        "hey ralph, can you...",
        "...ralph?",
        "tell ralph about it",
        "Ralph's project",
        "(ralph)",
        "ralphy",
        "Ralphy!",
        "ralphie",
        "Ralphie's done",
        "hey RALPHIE",
    ],
)
def test_nickname_matches_wake(body: str) -> None:
    gate = Gate(["ralph", "ralphy", "ralphie"])
    assert gate.should_wake(_g(text=body)) is True, f"expected wake on {body!r}"


@pytest.mark.parametrize(
    "body",
    [
        "ralphson",
        "ralphing",
        "ralphed",
        "ralpha",
        "ralpher",
        "ralphabc",
        "ralphlauren",      # one word, no boundary
        "underralph",       # prefix without word boundary
        "absolutely nothing here",
        "",
    ],
)
def test_strict_allowlist_does_not_wake_on_close_misses(body: str) -> None:
    gate = Gate(["ralph", "ralphy", "ralphie"])
    assert gate.should_wake(_g(text=body)) is False, f"unexpected wake on {body!r}"


def test_multi_line_body_matches() -> None:
    gate = Gate(["ralph"])
    body = "thinking about this\n\ncan you take a look ralph?"
    assert gate.should_wake(_g(text=body)) is True


# ---------------------------------------------------------------------------
# Self-echo guard
# ---------------------------------------------------------------------------


def test_self_echo_never_wakes_even_with_nickname() -> None:
    gate = Gate(["ralph"])
    assert gate.should_wake(_g(text="ralph here", sender=BOT)) is False


def test_self_echo_wins_over_at_mention() -> None:
    """If the bot somehow mentions itself, still don't wake."""
    gate = Gate([])
    assert (
        gate.should_wake(_g(text="oops", sender=BOT, mentions=frozenset({BOT}))) is False
    )


# ---------------------------------------------------------------------------
# Default no-wake fall-through
# ---------------------------------------------------------------------------


def test_no_nickname_no_mention_does_not_wake() -> None:
    gate = Gate(["ralph"])
    assert gate.should_wake(_g(text="just a stray thought")) is False


def test_empty_bot_mxid_disables_mention_path() -> None:
    """If we somehow don't know the bot's own MXID, mention path is
    inert — but nickname path still works."""
    gate = Gate(["ralph"])
    inp = GateInput(
        gate_text="hi ralph",
        bot_mxid="",
        sender_mxid=ANNIKA,
        mentioned_mxids=frozenset({"@anyone:server"}),
    )
    assert gate.should_wake(inp) is True


def test_empty_bot_mxid_and_no_nickname_does_not_wake() -> None:
    gate = Gate(["ralph"])
    inp = GateInput(
        gate_text="hi there",
        bot_mxid="",
        sender_mxid=ANNIKA,
        mentioned_mxids=frozenset({"@anyone:server"}),
    )
    assert gate.should_wake(inp) is False
