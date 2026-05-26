"""Tests for the vault Markdown writer.

The writer is the trickiest piece — section lifecycle, day-boundary
behaviour, and concurrency all interact. Tests use ``tmp_path`` for
real file IO (small enough to be fast, sharp enough to catch any
serialization bug).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from zoneinfo import ZoneInfo

from hermes_chat_recorder.types import Section, WriteOutcome
from hermes_chat_recorder.writer import (
    VaultWriter,
    _extract_stage,
    _find_existing_section,
    render_section,
)


PT = ZoneInfo("America/Los_Angeles")
ROOM = "northbound-ceo"


def _ts(year=2026, month=5, day=26, hour=9, minute=14, tz: ZoneInfo = PT) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=tz)


def _section(
    event_id: str = "$abc:server",
    stage: str = "received",
    kind: str = "text",
    body: str = "hey ralph",
    ts: datetime | None = None,
    fields: dict[str, str] | None = None,
) -> Section:
    return Section(
        event_id=event_id,
        timestamp=ts or _ts(),
        sender="Annika",
        kind=kind,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        fields=fields or {},
        body=body,
    )


# ---------------------------------------------------------------------------
# render_section + parse helpers
# ---------------------------------------------------------------------------


def test_render_section_includes_anchor_header_and_terminator() -> None:
    s = _section(event_id="$evt:srv", stage="transcribed", body="okay so the deck...")
    rendered = render_section(s)
    assert rendered.startswith("<!-- event:$evt:srv -->\n")
    assert "### 09:14 Annika · text · stage:transcribed" in rendered
    assert rendered.endswith("\n---\n")


def test_render_section_emits_fields_in_insertion_order() -> None:
    s = _section(
        fields={"audio": "mxc://srv/abc", "duration_sec": "12", "mime": "audio/ogg"}
    )
    rendered = render_section(s)
    a = rendered.index("**audio:**")
    b = rendered.index("**duration_sec:**")
    c = rendered.index("**mime:**")
    assert a < b < c


def test_render_section_with_empty_body_still_has_terminator() -> None:
    s = _section(body="")
    rendered = render_section(s)
    assert rendered.endswith("\n---\n")
    # No spurious blank line at the start of the body region.
    assert "\n\n\n" not in rendered


def test_extract_stage_from_rendered_section() -> None:
    s = _section(stage="describe_failed")
    rendered = render_section(s)
    assert _extract_stage(rendered) == "describe_failed"


def test_find_existing_section_locates_anchor_and_terminator() -> None:
    s1 = render_section(_section(event_id="$one", body="first"))
    s2 = render_section(_section(event_id="$two", body="second"))
    s3 = render_section(_section(event_id="$three", body="third"))
    blob = s1 + s2 + s3
    found = _find_existing_section(blob, "$two")
    assert found is not None
    start, end, text = found
    assert blob[start:end] == s2
    assert "$two" in text


def test_find_existing_section_returns_none_when_missing() -> None:
    blob = render_section(_section(event_id="$one"))
    assert _find_existing_section(blob, "$missing") is None


def test_find_existing_section_event_id_in_body_is_not_a_false_positive() -> None:
    """A user message that LITERALLY contains the anchor string in its
    body is unusual, but the anchor format includes the HTML-comment
    delimiters, so a bare event_id reference in a body should NOT match."""
    s1 = render_section(
        _section(event_id="$one", body="I saw a comment referencing $two earlier.")
    )
    blob = s1
    # Looking up $two should NOT match — only the literal anchor comment counts.
    assert _find_existing_section(blob, "$two") is None


# ---------------------------------------------------------------------------
# VaultWriter.write_section — append / replace / no-op
# ---------------------------------------------------------------------------


def test_appends_to_fresh_file(tmp_path: Path) -> None:
    w = VaultWriter(vault_root=tmp_path)
    outcome = w.write_section(_section(), room_slug=ROOM)
    assert outcome == WriteOutcome.APPENDED

    expected = tmp_path / ROOM / "2026-05-26.md"
    assert expected.exists()
    content = expected.read_text()
    assert "<!-- event:$abc:server -->" in content
    assert "hey ralph" in content


def test_appends_multiple_distinct_sections(tmp_path: Path) -> None:
    w = VaultWriter(vault_root=tmp_path)
    assert w.write_section(_section(event_id="$one", body="first"), room_slug=ROOM) == WriteOutcome.APPENDED
    assert w.write_section(_section(event_id="$two", body="second"), room_slug=ROOM) == WriteOutcome.APPENDED
    assert w.write_section(_section(event_id="$three", body="third"), room_slug=ROOM) == WriteOutcome.APPENDED

    day_file = tmp_path / ROOM / "2026-05-26.md"
    content = day_file.read_text()
    assert content.count("<!-- event:$") == 3
    assert content.index("$one") < content.index("$two") < content.index("$three")


def test_replaces_existing_section_on_stage_transition(tmp_path: Path) -> None:
    w = VaultWriter(vault_root=tmp_path)
    # First write: received placeholder.
    placeholder = _section(stage="received", body="(transcribing...)")
    w.write_section(placeholder, room_slug=ROOM)

    # Update: terminal transcribed.
    final = _section(stage="transcribed", body="real transcript text")
    outcome = w.write_section(final, room_slug=ROOM)
    assert outcome == WriteOutcome.REPLACED

    day_file = tmp_path / ROOM / "2026-05-26.md"
    content = day_file.read_text()
    assert "real transcript text" in content
    assert "(transcribing...)" not in content
    assert content.count("<!-- event:$abc:server -->") == 1


def test_replaces_terminal_to_different_terminal(tmp_path: Path) -> None:
    """A section that landed at one terminal stage CAN be replaced by a
    different terminal (e.g. operator manually re-runs description)."""
    w = VaultWriter(vault_root=tmp_path)
    w.write_section(_section(stage="describe_failed", body="(timed out)"), room_slug=ROOM)
    outcome = w.write_section(
        _section(stage="described", body="A whiteboard photo."), room_slug=ROOM
    )
    assert outcome == WriteOutcome.REPLACED
    day_file = tmp_path / ROOM / "2026-05-26.md"
    assert "A whiteboard photo." in day_file.read_text()


def test_no_op_when_same_terminal_stage(tmp_path: Path) -> None:
    w = VaultWriter(vault_root=tmp_path)
    w.write_section(_section(stage="transcribed", body="content"), room_slug=ROOM)
    # Same event_id + same terminal stage — should NO_OP.
    outcome = w.write_section(_section(stage="transcribed", body="content"), room_slug=ROOM)
    assert outcome == WriteOutcome.NO_OP
    day_file = tmp_path / ROOM / "2026-05-26.md"
    assert day_file.read_text().count("<!-- event:$abc:server -->") == 1


def test_non_terminal_to_non_terminal_replaces(tmp_path: Path) -> None:
    """received → received with new fields should replace (not no-op)."""
    w = VaultWriter(vault_root=tmp_path)
    w.write_section(_section(stage="received", body="v1"), room_slug=ROOM)
    outcome = w.write_section(_section(stage="received", body="v2"), room_slug=ROOM)
    assert outcome == WriteOutcome.REPLACED
    day_file = tmp_path / ROOM / "2026-05-26.md"
    assert "v2" in day_file.read_text()
    assert "v1" not in day_file.read_text()


# ---------------------------------------------------------------------------
# Day boundaries + timezone handling
# ---------------------------------------------------------------------------


def test_event_local_date_picks_day_file(tmp_path: Path) -> None:
    """An event at 23:59 PT and a reply at 00:01 PT (next day) land in
    different files. Per docs/DESIGN.md §3."""
    w = VaultWriter(vault_root=tmp_path, timezone="America/Los_Angeles")
    night = _section(
        event_id="$inbound", body="night", ts=_ts(hour=23, minute=59)
    )
    morning = _section(
        event_id="$reply",
        body="morning",
        ts=_ts(day=27, hour=0, minute=1),
    )
    w.write_section(night, room_slug=ROOM)
    w.write_section(morning, room_slug=ROOM)

    assert (tmp_path / ROOM / "2026-05-26.md").exists()
    assert (tmp_path / ROOM / "2026-05-27.md").exists()
    assert "$reply" not in (tmp_path / ROOM / "2026-05-26.md").read_text()
    assert "$inbound" not in (tmp_path / ROOM / "2026-05-27.md").read_text()


def test_utc_timestamp_converted_to_configured_zone(tmp_path: Path) -> None:
    """Caller passes a UTC timestamp; writer converts to configured tz
    for day-file selection. 07:00 UTC = 00:00 PT (the prior day in PT
    during DST, but May 26 is PDT = UTC-7, so 07:00 UTC = midnight PT
    on May 26)."""
    w = VaultWriter(vault_root=tmp_path, timezone="America/Los_Angeles")
    utc_ts = datetime(2026, 5, 26, 7, 0, tzinfo=timezone.utc)  # 00:00 PT, May 26
    w.write_section(_section(ts=utc_ts), room_slug=ROOM)
    assert (tmp_path / ROOM / "2026-05-26.md").exists()


def test_naive_timestamp_assumed_to_be_in_configured_zone(tmp_path: Path) -> None:
    w = VaultWriter(vault_root=tmp_path, timezone="America/Los_Angeles")
    naive = datetime(2026, 5, 26, 9, 14)  # no tzinfo
    w.write_section(_section(ts=naive), room_slug=ROOM)  # type: ignore[arg-type]
    # Lands in PT's May 26 file.
    assert (tmp_path / ROOM / "2026-05-26.md").exists()


# ---------------------------------------------------------------------------
# has_event() — sync-replay short-circuit support
# ---------------------------------------------------------------------------


def test_has_event_false_when_file_missing(tmp_path: Path) -> None:
    w = VaultWriter(vault_root=tmp_path)
    assert w.has_event("$missing", ROOM, _ts()) is False


def test_has_event_true_after_write(tmp_path: Path) -> None:
    w = VaultWriter(vault_root=tmp_path)
    w.write_section(_section(event_id="$present"), room_slug=ROOM)
    assert w.has_event("$present", ROOM, _ts()) is True


def test_has_event_false_for_different_event(tmp_path: Path) -> None:
    w = VaultWriter(vault_root=tmp_path)
    w.write_section(_section(event_id="$one"), room_slug=ROOM)
    assert w.has_event("$two", ROOM, _ts()) is False


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_writes_to_same_day_file_serialize(tmp_path: Path) -> None:
    """Twenty threads write 20 distinct sections to the same day file
    simultaneously. Result: all 20 sections present, no corruption."""
    w = VaultWriter(vault_root=tmp_path)

    def worker(i: int) -> None:
        w.write_section(_section(event_id=f"$evt{i}:s", body=f"body{i}"), room_slug=ROOM)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    content = (tmp_path / ROOM / "2026-05-26.md").read_text()
    assert content.count("<!-- event:$evt") == 20
    # Every body line is present.
    for i in range(20):
        assert f"body{i}" in content
    # Every section terminated cleanly — no spliced anchors.
    assert content.count("\n---\n") == 20


def test_concurrent_writes_different_rooms_dont_block(tmp_path: Path) -> None:
    """Locks are per-(room, date) — writes to different rooms should
    not contend. Functional smoke: both files end up with one section."""
    w = VaultWriter(vault_root=tmp_path)

    def writer(room: str, evt: str) -> None:
        w.write_section(_section(event_id=evt, body=room), room_slug=room)

    t1 = threading.Thread(target=writer, args=("room-a", "$a"))
    t2 = threading.Thread(target=writer, args=("room-b", "$b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert (tmp_path / "room-a" / "2026-05-26.md").exists()
    assert (tmp_path / "room-b" / "2026-05-26.md").exists()


def test_lock_cache_reuses_same_lock_for_same_key(tmp_path: Path) -> None:
    """Idempotency / efficiency: asking for the lock twice yields the
    same lock object. Important so concurrent writers actually serialize."""
    w = VaultWriter(vault_root=tmp_path)
    l1 = w._get_lock(ROOM, "2026-05-26")  # noqa: SLF001 - test internal
    l2 = w._get_lock(ROOM, "2026-05-26")  # noqa: SLF001
    l3 = w._get_lock("other", "2026-05-26")  # noqa: SLF001
    assert l1 is l2
    assert l1 is not l3
