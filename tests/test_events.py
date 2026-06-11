"""Tests for the platform-generic event extractor.

These verify the duck-typed extraction works against fake objects that
mimic Hermes's unified ``MessageEvent`` / ``SessionSource`` shapes —
both attribute and dict-key access patterns are exercised, so the
extractor doesn't break if upstream shifts shapes between releases.
The Matrix enrichment pass (raw mautrix event parsing) gets its own
section at the bottom.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_chat_recorder.events import (
    EventInfo,
    extract,
    slug_from_chat_id,
)


def _src(
    platform: str = "matrix",
    chat_id: str = "!room:srv",
    user_id: str = "@annika:srv",
    chat_name: str | None = None,
    chat_type: str | None = None,
    user_name: str | None = None,
) -> Any:
    return SimpleNamespace(
        platform=SimpleNamespace(value=platform),
        chat_id=chat_id,
        user_id=user_id,
        chat_name=chat_name,
        chat_type=chat_type,
        user_name=user_name,
    )


def _msg_type(name: str) -> Any:
    return SimpleNamespace(name=name)


def _raw(
    *,
    origin_server_ts: int | None = 1716729240000,
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


def _event(
    *,
    platform: str = "telegram",
    kind: str = "TEXT",
    text: str = "hello",
    message_id: str = "42",
    chat_id: str = "-1001234",
    user_id: str = "5551212",
    chat_name: str | None = "Family Chat",
    chat_type: str | None = "group",
    user_name: str | None = "Annika",
    media_urls: list | None = None,
    raw: Any = None,
    timestamp: Any = None,
    reply_to: str | None = None,
) -> Any:
    return SimpleNamespace(
        text=text,
        message_id=message_id,
        message_type=_msg_type(kind),
        source=_src(
            platform=platform,
            chat_id=chat_id,
            user_id=user_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_name=user_name,
        ),
        media_urls=media_urls or [],
        raw_message=raw,
        timestamp=timestamp,
        reply_to_message_id=reply_to,
    )


# ---------------------------------------------------------------------------
# Generic extraction — any platform
# ---------------------------------------------------------------------------


def test_telegram_text_event_extracted() -> None:
    info = extract(_event())
    assert info is not None
    assert info.platform == "telegram"
    assert info.kind == "text"
    assert info.body == "hello"
    assert info.chat_id == "-1001234"
    assert info.chat_name == "Family Chat"
    assert info.chat_type == "group"
    assert info.sender_id == "5551212"
    assert info.sender_display == "Annika"


def test_any_platform_value_is_accepted() -> None:
    for platform in ("discord", "slack", "signal", "whatsapp", "irc", "email"):
        info = extract(_event(platform=platform))
        assert info is not None, platform
        assert info.platform == platform


def test_missing_source_returns_none() -> None:
    event = SimpleNamespace(source=None, message_id="x")
    assert extract(event) is None


def test_string_platform_accepted() -> None:
    """Some test fixtures / future adapters may use plain strings for
    ``source.platform`` instead of the Platform enum."""
    event = _event()
    event.source.platform = "Discord"
    info = extract(event)
    assert info is not None
    assert info.platform == "discord"


def test_sender_display_falls_back_to_user_id_when_no_user_name() -> None:
    info = extract(_event(user_name=None))
    assert info is not None
    assert info.sender_display == "5551212"


def test_chat_metadata_tolerates_missing_fields() -> None:
    """Sources without chat_name / chat_type / user_name (sparse test
    fixtures, minimal adapters) must not crash extraction."""
    event = SimpleNamespace(
        text="hi",
        message_id="9",
        message_type=_msg_type("TEXT"),
        source=SimpleNamespace(
            platform=SimpleNamespace(value="irc"), chat_id="#chan", user_id="nick"
        ),
        raw_message=None,
    )
    info = extract(event)
    assert info is not None
    assert info.chat_name == ""
    assert info.chat_type == ""


def test_reply_to_message_id_carried_through() -> None:
    info = extract(_event(reply_to="41"))
    assert info is not None
    assert info.reply_to_id == "41"


# ---------------------------------------------------------------------------
# Message type mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("TEXT", "text"),
        ("COMMAND", "text"),
        ("AUDIO", "voice"),
        ("VOICE", "voice"),
        ("IMAGE", "image"),
        ("PHOTO", "image"),
        ("STICKER", "image"),
        ("VIDEO", "video"),
        ("DOCUMENT", "file"),
        ("LOCATION", "location"),
    ],
)
def test_message_type_mapping(name: str, expected: str) -> None:
    info = extract(_event(kind=name))
    assert info is not None
    assert info.kind == expected


def test_unknown_message_type_returns_none() -> None:
    assert extract(_event(kind="HOLOGRAM")) is None


# ---------------------------------------------------------------------------
# Required fields
# ---------------------------------------------------------------------------


def test_missing_message_id_returns_none() -> None:
    assert extract(_event(message_id="")) is None


def test_missing_chat_or_user_returns_none() -> None:
    assert extract(_event(chat_id="")) is None
    assert extract(_event(user_id="")) is None


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def test_naive_event_timestamp_becomes_tz_aware() -> None:
    """Hermes populates ``MessageEvent.timestamp`` with naive local
    wall-clock time; the extractor must attach the system zone so the
    writer can convert it correctly."""
    info = extract(_event(timestamp=datetime(2026, 6, 11, 9, 30)))
    assert info is not None
    assert info.timestamp.tzinfo is not None
    # Same wall-clock instant, now zone-tagged.
    assert info.timestamp.replace(tzinfo=None) == datetime(2026, 6, 11, 9, 30)


def test_aware_event_timestamp_preserved() -> None:
    ts = datetime(2026, 6, 11, 9, 30, tzinfo=timezone.utc)
    info = extract(_event(timestamp=ts))
    assert info is not None
    assert info.timestamp == ts


def test_missing_timestamp_falls_back_to_now() -> None:
    info = extract(_event(timestamp=None))
    assert info is not None
    assert info.timestamp.tzinfo is not None


def test_matrix_prefers_origin_server_ts() -> None:
    event = _event(
        platform="matrix",
        chat_id="!room:srv",
        user_id="@annika:srv",
        raw=_raw(origin_server_ts=1716729240000),
        timestamp=datetime(2030, 1, 1, 0, 0),  # decoy — must be ignored
    )
    info = extract(event)
    assert info is not None
    assert info.timestamp == datetime.fromtimestamp(1716729240, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Cached media paths
# ---------------------------------------------------------------------------


def test_local_media_path_taken_from_media_urls() -> None:
    info = extract(
        _event(kind="VOICE", text="", media_urls=["/home/user/.hermes/cache/audio/v.ogg"])
    )
    assert info is not None
    assert info.media_path == "/home/user/.hermes/cache/audio/v.ogg"


@pytest.mark.parametrize(
    "url", ["https://cdn.example.com/x.ogg", "http://x/y.ogg", "mxc://srv/abc"]
)
def test_remote_media_urls_rejected(url: str) -> None:
    """media_urls must contain LOCAL cached paths; remote URLs are not
    a readable file and must not be treated as one."""
    info = extract(_event(kind="VOICE", text="", media_urls=[url]))
    assert info is not None
    assert info.media_path is None


def test_text_events_ignore_media_urls() -> None:
    info = extract(_event(kind="TEXT", media_urls=["/tmp/x.bin"]))
    assert info is not None
    assert info.media_path is None


def test_video_and_document_get_media_path() -> None:
    for kind, expected_kind in (("VIDEO", "video"), ("DOCUMENT", "file")):
        info = extract(_event(kind=kind, text="", media_urls=["/tmp/m.bin"]))
        assert info is not None
        assert info.kind == expected_kind
        assert info.media_path == "/tmp/m.bin"


# ---------------------------------------------------------------------------
# Matrix enrichment — raw mautrix event parsing
# ---------------------------------------------------------------------------


def _matrix_event(
    *,
    kind: str = "TEXT",
    text: str = "hi",
    message_id: str = "$evt:srv",
    raw: Any = None,
    media_urls: list | None = None,
) -> Any:
    return _event(
        platform="matrix",
        kind=kind,
        text=text,
        message_id=message_id,
        chat_id="!room:srv",
        user_id="@annika:srv",
        chat_name=None,
        chat_type="group",
        user_name=None,
        media_urls=media_urls,
        raw=raw if raw is not None else _raw(),
    )


def test_matrix_reaction_event_flagged_for_filtering() -> None:
    info = extract(_matrix_event(raw=_raw(type="m.reaction")))
    assert info is not None
    assert info.is_reaction is True


def test_non_matrix_platforms_never_flag_reactions() -> None:
    """The m.reaction check reads the raw mautrix event; other
    platforms' raw payloads must not trigger it."""
    event = _event(raw=SimpleNamespace(type=SimpleNamespace(value="m.reaction")))
    info = extract(event)
    assert info is not None
    assert info.is_reaction is False


def test_matrix_voice_extracts_mxc_mime_and_duration() -> None:
    info = extract(
        _matrix_event(
            kind="AUDIO",
            text="",
            raw=_raw(
                content={
                    "url": "mxc://srv/audiohash",
                    "info": {"mimetype": "audio/ogg", "duration": 12345},
                }
            ),
        )
    )
    assert info is not None
    assert info.kind == "voice"
    assert info.mxc_url == "mxc://srv/audiohash"
    assert info.mime == "audio/ogg"
    assert info.duration_sec == 12  # ms / 1000, floored


def test_matrix_voice_extracts_url_from_encrypted_file_block() -> None:
    """E2EE Matrix rooms put the mxc URL inside content.file.url and
    surround it with crypto metadata (key/iv/hashes/v)."""
    info = extract(
        _matrix_event(
            kind="AUDIO",
            text="",
            raw=_raw(
                content={
                    "msgtype": "m.audio",
                    "file": {
                        "url": "mxc://srv/encrypted-blob",
                        "key": {"alg": "A256CTR", "k": "abc"},
                        "iv": "AAAAAAA",
                        "hashes": {"sha256": "deadbeef"},
                        "v": "v2",
                    },
                    "info": {"mimetype": "audio/ogg", "duration": 8000},
                }
            ),
        )
    )
    assert info is not None
    assert info.mxc_url == "mxc://srv/encrypted-blob"
    assert info.duration_sec == 8


def test_matrix_image_extracts_url_from_encrypted_file_block() -> None:
    info = extract(
        _matrix_event(
            kind="IMAGE",
            text="",
            raw=_raw(
                content={
                    "msgtype": "m.image",
                    "file": {"url": "mxc://srv/encrypted-img", "key": {"k": "x"}},
                    "info": {"mimetype": "image/jpeg"},
                }
            ),
        )
    )
    assert info is not None
    assert info.mxc_url == "mxc://srv/encrypted-img"
    assert info.mime == "image/jpeg"


def test_matrix_voice_with_missing_info_is_still_extractable() -> None:
    info = extract(
        _matrix_event(kind="AUDIO", text="", raw=_raw(content={"url": "mxc://srv/x"}))
    )
    assert info is not None
    assert info.mxc_url == "mxc://srv/x"
    assert info.mime is None
    assert info.duration_sec is None


def test_matrix_image_caption_preserved_in_body() -> None:
    info = extract(
        _matrix_event(
            kind="IMAGE",
            text="check this whiteboard",
            raw=_raw(content={"url": "mxc://srv/img", "info": {"mimetype": "image/jpeg"}}),
        )
    )
    assert info is not None
    assert info.body == "check this whiteboard"
    assert info.duration_sec is None


def test_dict_shaped_raw_message_works() -> None:
    info = extract(
        _matrix_event(
            kind="AUDIO",
            text="",
            raw={
                "origin_server_ts": 1716729240000,
                "content": {"url": "mxc://srv/x", "info": {"mimetype": "audio/ogg"}},
            },
        )
    )
    assert info is not None
    assert info.mxc_url == "mxc://srv/x"


def test_matrix_sender_display_falls_back_to_mxid() -> None:
    info = extract(_matrix_event())
    assert info is not None
    assert info.sender_display == "@annika:srv"


def test_matrix_sender_display_uses_raw_displayname() -> None:
    info = extract(_matrix_event(raw=_raw(sender_display_name="Annika")))
    assert info is not None
    assert info.sender_display == "Annika"


# ---------------------------------------------------------------------------
# Matrix edit detection — m.replace relations
#
# NOTE: Hermes's current Matrix adapter filters m.replace events before
# dispatch, so in production these never reach the hook. The handling
# is dormant defense for older/future adapters that pass them through.
# ---------------------------------------------------------------------------


def _edit_event(
    *,
    new_body: str,
    target_event_id: str = "$orig:srv",
    fallback_body: str = "* edited fallback",
    use_new_content: bool = True,
):
    content: dict[str, Any] = {
        "body": fallback_body,
        "m.relates_to": {
            "rel_type": "m.replace",
            "event_id": target_event_id,
        },
    }
    if use_new_content:
        content["m.new_content"] = {"body": new_body}
    return _matrix_event(message_id="$edit:srv", text="", raw=_raw(content=content))


def test_edit_event_flagged_with_new_body() -> None:
    info = extract(_edit_event(new_body="here is the corrected text"))
    assert info is not None
    assert info.is_edit is True
    assert info.replaced_event_id == "$orig:srv"
    assert info.body == "here is the corrected text"


def test_edit_event_falls_back_to_stripped_body_prefix() -> None:
    """Older Matrix clients omit m.new_content. We then read content.body
    and strip the leading "* " fallback prefix."""
    info = extract(
        _edit_event(
            new_body="ignored",
            fallback_body="* second-try wording",
            use_new_content=False,
        )
    )
    assert info is not None
    assert info.is_edit is True
    assert info.body == "second-try wording"


def test_non_edit_event_is_not_flagged() -> None:
    info = extract(
        _matrix_event(text="plain", raw=_raw(content={"body": "plain"}))
    )
    assert info is not None
    assert info.is_edit is False
    assert info.replaced_event_id is None


def test_threaded_reply_is_not_treated_as_edit() -> None:
    info = extract(
        _matrix_event(
            text="threaded reply",
            raw=_raw(
                content={
                    "body": "threaded reply",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$root:srv"},
                }
            ),
        )
    )
    assert info is not None
    assert info.is_edit is False


# ---------------------------------------------------------------------------
# slug_from_chat_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cid,expected",
    [
        ("!abc:server", "abc"),
        ("#general:server", "general"),
        ("!cab1d9cd572caba677:agentchannels.dev", "cab1d9cd572caba677"),
        ("plain-chat-id", "plain-chat-id"),
        ("", "unknown-chat"),
        ("!weird/chars:srv", "weird-chars"),
        ("-1001234567", "1001234567"),     # Telegram group
        ("987654321098765432", "987654321098765432"),  # Discord snowflake
        ("user@example.com", "user-example-com"),       # email
    ],
)
def test_slug_derivation(cid: str, expected: str) -> None:
    assert slug_from_chat_id(cid) == expected


def test_event_info_is_frozen() -> None:
    import dataclasses

    info = extract(_event())
    assert isinstance(info, EventInfo)
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.body = "mutate"  # type: ignore[misc]
