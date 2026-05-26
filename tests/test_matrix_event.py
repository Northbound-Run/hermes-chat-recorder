"""Tests for the Matrix event adapter.

These verify the duck-typed extraction works against fake objects that
mimic the relevant shapes — both `attribute` and `dict-key` access
patterns are exercised, so the adapter doesn't break if mautrix shifts
shapes between releases.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_chat_recorder.matrix_event import (
    MatrixEventInfo,
    extract,
    room_slug_from_room_id,
)


def _src(platform: str = "matrix", chat_id: str = "!room:srv", user_id: str = "@annika:srv") -> Any:
    return SimpleNamespace(platform=SimpleNamespace(value=platform), chat_id=chat_id, user_id=user_id)


def _msg_type(name: str) -> Any:
    return SimpleNamespace(name=name)


def _raw(
    *,
    origin_server_ts: int | None = 1716729240000,  # 2024-05-26 14:34:00 UTC
    type: str | None = None,
    content: dict | None = None,
    sender_display_name: str | None = None,
) -> Any:
    obj = SimpleNamespace()
    if origin_server_ts is not None:
        obj.origin_server_ts = origin_server_ts
    if type is not None:
        obj.type = SimpleNamespace(value=type)
    if content is not None:
        obj.content = content
    if sender_display_name is not None:
        obj.sender_display_name = sender_display_name
    return obj


# ---------------------------------------------------------------------------
# Platform filtering
# ---------------------------------------------------------------------------


def test_non_matrix_platform_returns_none() -> None:
    event = SimpleNamespace(
        source=_src(platform="telegram"),
        message_id="x",
        message_type=_msg_type("TEXT"),
        text="hi",
    )
    assert extract(event) is None


def test_missing_source_returns_none() -> None:
    event = SimpleNamespace(source=None, message_id="x")
    assert extract(event) is None


def test_matrix_platform_is_detected() -> None:
    event = SimpleNamespace(
        source=_src(),
        message_id="$evt:srv",
        message_type=_msg_type("TEXT"),
        text="hi ralph",
        raw_message=_raw(),
    )
    info = extract(event)
    assert info is not None
    assert info.kind == "text"
    assert info.body == "hi ralph"


# ---------------------------------------------------------------------------
# Message type mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("TEXT", "text"),
        ("AUDIO", "voice"),
        ("VOICE", "voice"),
        ("IMAGE", "image"),
        ("PHOTO", "image"),
        ("STICKER", "image"),
    ],
)
def test_message_type_mapping(name: str, expected: str) -> None:
    event = SimpleNamespace(
        source=_src(),
        message_id="$evt:srv",
        message_type=_msg_type(name),
        text="",
        raw_message=_raw(),
    )
    info = extract(event)
    assert info is not None
    assert info.kind == expected


def test_unknown_message_type_returns_none() -> None:
    event = SimpleNamespace(
        source=_src(),
        message_id="$evt:srv",
        message_type=_msg_type("VIDEO"),  # not in our supported map
        text="",
        raw_message=_raw(),
    )
    assert extract(event) is None


# ---------------------------------------------------------------------------
# Reactions are extracted but flagged
# ---------------------------------------------------------------------------


def test_reaction_event_flagged_for_filtering() -> None:
    event = SimpleNamespace(
        source=_src(),
        message_id="$react:srv",
        message_type=_msg_type("TEXT"),
        text="",
        raw_message=_raw(type="m.reaction"),
    )
    info = extract(event)
    assert info is not None
    assert info.is_reaction is True


# ---------------------------------------------------------------------------
# Required fields
# ---------------------------------------------------------------------------


def test_missing_message_id_returns_none() -> None:
    event = SimpleNamespace(
        source=_src(),
        message_id=None,
        message_type=_msg_type("TEXT"),
        text="",
        raw_message=_raw(),
    )
    assert extract(event) is None


def test_missing_chat_or_user_returns_none() -> None:
    event = SimpleNamespace(
        source=_src(chat_id="", user_id="@a:s"),
        message_id="$x",
        message_type=_msg_type("TEXT"),
        text="",
        raw_message=_raw(),
    )
    assert extract(event) is None


# ---------------------------------------------------------------------------
# Timestamp extraction
# ---------------------------------------------------------------------------


def test_timestamp_extracted_from_origin_server_ts() -> None:
    event = SimpleNamespace(
        source=_src(),
        message_id="$x",
        message_type=_msg_type("TEXT"),
        text="hi",
        raw_message=_raw(origin_server_ts=1716729240000),
    )
    info = extract(event)
    assert info is not None
    expected = datetime.fromtimestamp(1716729240, tz=timezone.utc)
    assert info.timestamp == expected


def test_timestamp_falls_back_to_now_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    event = SimpleNamespace(
        source=_src(),
        message_id="$x",
        message_type=_msg_type("TEXT"),
        text="hi",
        raw_message=_raw(origin_server_ts=None),
    )
    info = extract(event)
    assert info is not None
    # Just confirm it's a tz-aware datetime — fallback is wall-clock.
    assert info.timestamp.tzinfo is not None


# ---------------------------------------------------------------------------
# Voice media extraction
# ---------------------------------------------------------------------------


def test_voice_extracts_mxc_mime_and_duration() -> None:
    event = SimpleNamespace(
        source=_src(),
        message_id="$audio",
        message_type=_msg_type("AUDIO"),
        text="",
        raw_message=_raw(
            content={
                "url": "mxc://srv/audiohash",
                "info": {"mimetype": "audio/ogg", "duration": 12345},
            }
        ),
    )
    info = extract(event)
    assert info is not None
    assert info.kind == "voice"
    assert info.mxc_url == "mxc://srv/audiohash"
    assert info.mime == "audio/ogg"
    assert info.duration_sec == 12  # ms / 1000, floored


def test_voice_with_missing_info_is_still_extractable() -> None:
    event = SimpleNamespace(
        source=_src(),
        message_id="$audio",
        message_type=_msg_type("AUDIO"),
        text="",
        raw_message=_raw(content={"url": "mxc://srv/x"}),  # no info dict
    )
    info = extract(event)
    assert info is not None
    assert info.kind == "voice"
    assert info.mxc_url == "mxc://srv/x"
    assert info.mime is None
    assert info.duration_sec is None


# ---------------------------------------------------------------------------
# Image extraction
# ---------------------------------------------------------------------------


def test_image_extracts_mxc_and_mime_but_no_duration() -> None:
    event = SimpleNamespace(
        source=_src(),
        message_id="$img",
        message_type=_msg_type("IMAGE"),
        text="check this whiteboard",
        raw_message=_raw(
            content={"url": "mxc://srv/imghash", "info": {"mimetype": "image/jpeg"}}
        ),
    )
    info = extract(event)
    assert info is not None
    assert info.kind == "image"
    assert info.mxc_url == "mxc://srv/imghash"
    assert info.mime == "image/jpeg"
    assert info.duration_sec is None
    # Caption preserved in body for the gate to inspect.
    assert info.body == "check this whiteboard"


# ---------------------------------------------------------------------------
# Mention extraction
# ---------------------------------------------------------------------------


def test_mentions_from_m_mentions() -> None:
    event = SimpleNamespace(
        source=_src(),
        message_id="$x",
        message_type=_msg_type("TEXT"),
        text="hey",
        raw_message=_raw(
            content={"m.mentions": {"user_ids": ["@ralph:srv", "@cmo:srv"]}}
        ),
    )
    info = extract(event)
    assert info is not None
    assert info.mentioned_mxids == frozenset({"@ralph:srv", "@cmo:srv"})


def test_no_mentions_returns_empty_set() -> None:
    event = SimpleNamespace(
        source=_src(),
        message_id="$x",
        message_type=_msg_type("TEXT"),
        text="hi",
        raw_message=_raw(content={}),
    )
    info = extract(event)
    assert info is not None
    assert info.mentioned_mxids == frozenset()


def test_legacy_mentions_key_supported() -> None:
    event = SimpleNamespace(
        source=_src(),
        message_id="$x",
        message_type=_msg_type("TEXT"),
        text="hi",
        raw_message=_raw(content={"mentions": {"user_ids": ["@ralph:srv"]}}),
    )
    info = extract(event)
    assert info is not None
    assert info.mentioned_mxids == frozenset({"@ralph:srv"})


# ---------------------------------------------------------------------------
# Dict-shaped raw_message (defensive)
# ---------------------------------------------------------------------------


def test_dict_shaped_raw_message_works() -> None:
    event = SimpleNamespace(
        source=_src(),
        message_id="$x",
        message_type=_msg_type("AUDIO"),
        text="",
        raw_message={
            "origin_server_ts": 1716729240000,
            "content": {"url": "mxc://srv/x", "info": {"mimetype": "audio/ogg"}},
        },
    )
    info = extract(event)
    assert info is not None
    assert info.kind == "voice"
    assert info.mxc_url == "mxc://srv/x"


# ---------------------------------------------------------------------------
# Sender display name
# ---------------------------------------------------------------------------


def test_sender_display_falls_back_to_mxid_when_no_displayname() -> None:
    event = SimpleNamespace(
        source=_src(user_id="@annika:srv"),
        message_id="$x",
        message_type=_msg_type("TEXT"),
        text="hi",
        raw_message=_raw(),
    )
    info = extract(event)
    assert info is not None
    assert info.sender_display == "@annika:srv"


def test_sender_display_uses_raw_displayname() -> None:
    event = SimpleNamespace(
        source=_src(user_id="@annika:srv"),
        message_id="$x",
        message_type=_msg_type("TEXT"),
        text="hi",
        raw_message=_raw(sender_display_name="Annika"),
    )
    info = extract(event)
    assert info is not None
    assert info.sender_display == "Annika"


# ---------------------------------------------------------------------------
# room_slug_from_room_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rid,expected",
    [
        ("!abc:server", "abc"),
        ("#general:server", "general"),
        ("!cab1d9cd572caba677:agentchannels.dev", "cab1d9cd572caba677"),
        ("plain-room-id", "plain-room-id"),
        ("", "unknown-room"),
        ("!weird/chars:srv", "weird-chars"),
    ],
)
def test_room_slug_derivation(rid: str, expected: str) -> None:
    assert room_slug_from_room_id(rid) == expected
