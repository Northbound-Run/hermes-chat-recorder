"""Tests for the Recorder orchestrator.

Uses real VaultWriter (tmp_path); injects fakes for the transcriber,
describer, and media downloader. The recorder makes no wake decisions —
every message is recorded, and the hook returns either None
(passthrough) or a rewrite (for voice/image events).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hermes_chat_recorder.config import RecorderConfig
from hermes_chat_recorder.describer import ImageDescriberError
from hermes_chat_recorder.name_resolver import NameResolver
from hermes_chat_recorder.recorder import Recorder
from hermes_chat_recorder.transcriber import TranscriberError
from hermes_chat_recorder.types import DescribeResult
from hermes_chat_recorder.writer import VaultWriter

ROOM = "!testroom:srv"
BOT = "@recorder_bot:srv"
SENDER = "@annika:srv"


@dataclass
class _FakeTranscriber:
    return_value: str = "transcribed text"
    raise_exc: Exception | None = None
    calls: list[str] = field(default_factory=list)

    def transcribe(self, path, **_):
        self.calls.append(path)
        if self.raise_exc:
            raise self.raise_exc
        return self.return_value


@dataclass
class _FakeDescriber:
    return_value: DescribeResult = field(
        default_factory=lambda: DescribeResult(
            description="a whiteboard photo", text="Pilot scope", raw=""
        )
    )
    raise_exc: Exception | None = None
    calls: list[bytes] = field(default_factory=list)

    def describe(self, image_bytes, *, mime=""):
        self.calls.append(image_bytes)
        if self.raise_exc:
            raise self.raise_exc
        return self.return_value


def _msg_type(name: str):
    return SimpleNamespace(name=name)


def _event(
    *,
    kind: str = "TEXT",
    text: str = "hello",
    message_id: str = "$evt1:srv",
    sender: str = SENDER,
    chat_id: str = ROOM,
    platform: str = "matrix",
    chat_name: str | None = None,
    chat_type: str | None = "group",
    user_name: str | None = None,
    media_urls: list | None = None,
    raw: Any = None,
):
    return SimpleNamespace(
        text=text,
        message_id=message_id,
        message_type=_msg_type(kind),
        source=SimpleNamespace(
            platform=SimpleNamespace(value=platform),
            chat_id=chat_id,
            user_id=sender,
            chat_name=chat_name,
            chat_type=chat_type,
            user_name=user_name,
        ),
        media_urls=media_urls or [],
        raw_message=raw
        if raw is not None
        else SimpleNamespace(origin_server_ts=1716729240000, content={}),
    )


def _audio_raw(mxc: str = "mxc://srv/audio", mime: str = "audio/ogg", duration_ms: int = 12000):
    return SimpleNamespace(
        origin_server_ts=1716729240000,
        content={"url": mxc, "info": {"mimetype": mime, "duration": duration_ms}},
    )


def _image_raw(mxc: str = "mxc://srv/img", mime: str = "image/jpeg"):
    return SimpleNamespace(
        origin_server_ts=1716729240000,
        content={"url": mxc, "info": {"mimetype": mime}},
    )


def _build_recorder(
    tmp_path: Path,
    *,
    bot_mxid: str = BOT,
    transcriber: Any = None,
    describer: Any = None,
    download_media: Any = None,
    record_outbound: bool = True,
    resolver: NameResolver | None = None,
    platforms: frozenset[str] = frozenset(),
    bot_name: str = "",
) -> Recorder:
    cfg = RecorderConfig(
        vault_root=tmp_path,
        record_outbound=record_outbound,
        platforms=platforms,
        bot_name=bot_name,
    )
    writer = VaultWriter(vault_root=tmp_path, timezone="UTC")
    return Recorder(
        config=cfg,
        writer=writer,
        resolver=resolver,
        transcriber=transcriber,
        describer=describer,
        bot_mxid=bot_mxid,
        download_media=download_media,
    )


# ---------------------------------------------------------------------------
# Multi-platform recording
# ---------------------------------------------------------------------------


def test_telegram_text_event_is_recorded(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    result = r.on_pre_gateway_dispatch(
        event=_event(
            platform="telegram",
            chat_id="-1001234",
            sender="5551212",
            chat_name="Family Chat",
            user_name="Annika",
            text="hi from telegram",
            message_id="88",
            raw=None,
        )
    )
    assert result is None
    day_file = next(tmp_path.rglob("*.md"))
    # Platform folder + chat-name folder.
    assert day_file.parent == tmp_path / "telegram" / "Family-Chat"
    content = day_file.read_text()
    assert "<!-- event:88 -->" in content
    assert "hi from telegram" in content
    assert "Annika" in content


def test_platform_folder_separates_same_chat_id(tmp_path: Path) -> None:
    """The same chat_id on two platforms must land in two folders."""
    r = _build_recorder(tmp_path)
    r.on_pre_gateway_dispatch(
        event=_event(platform="telegram", chat_id="12345", sender="1", message_id="a", raw=None)
    )
    r.on_pre_gateway_dispatch(
        event=_event(platform="discord", chat_id="12345", sender="2", message_id="b", raw=None)
    )
    assert (tmp_path / "telegram" / "12345").is_dir()
    assert (tmp_path / "discord" / "12345").is_dir()


def test_platform_allowlist_filters_recording(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path, platforms=frozenset({"matrix"}))
    result = r.on_pre_gateway_dispatch(
        event=_event(platform="telegram", chat_id="1", sender="2", raw=None)
    )
    assert result is None
    assert list(tmp_path.rglob("*.md")) == []
    # Matrix still records.
    r.on_pre_gateway_dispatch(event=_event(text="recorded"))
    assert len(list(tmp_path.rglob("*.md"))) == 1


def test_unnamed_dm_uses_sender_name_for_chat_folder(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    r.on_pre_gateway_dispatch(
        event=_event(
            platform="telegram",
            chat_id="5551212",
            sender="5551212",
            chat_name=None,
            chat_type="dm",
            user_name="Annika",
            message_id="9",
            raw=None,
        )
    )
    assert (tmp_path / "telegram" / "Annika").is_dir()


def test_group_chat_never_borrows_sender_name(tmp_path: Path) -> None:
    """An unnamed GROUP chat must fall back to its ID slug, not the
    first speaker's name."""
    r = _build_recorder(tmp_path)
    r.on_pre_gateway_dispatch(
        event=_event(
            platform="telegram",
            chat_id="-100777",
            sender="5551212",
            chat_name=None,
            chat_type="group",
            user_name="Annika",
            message_id="9",
            raw=None,
        )
    )
    assert (tmp_path / "telegram" / "100777").is_dir()


# ---------------------------------------------------------------------------
# Unprocessed kinds — video / file / location
# ---------------------------------------------------------------------------


def test_video_event_recorded_without_processing(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    result = r.on_pre_gateway_dispatch(
        event=_event(
            platform="telegram",
            kind="VIDEO",
            text="check this clip",
            message_id="v1",
            media_urls=["/tmp/cache/video.mp4"],
            raw=None,
        )
    )
    assert result is None  # no rewrite for unprocessed kinds
    content = next(tmp_path.rglob("*.md")).read_text()
    assert "stage:recorded" in content
    assert "· video ·" in content
    assert "check this clip" in content
    assert "**media_path:** /tmp/cache/video.mp4" in content


def test_document_event_recorded_as_file_kind(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    r.on_pre_gateway_dispatch(
        event=_event(platform="slack", kind="DOCUMENT", text="", message_id="d1", raw=None)
    )
    content = next(tmp_path.rglob("*.md")).read_text()
    assert "· file ·" in content
    assert "(file message)" in content  # placeholder body when no caption


def test_location_event_recorded(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    r.on_pre_gateway_dispatch(
        event=_event(
            platform="telegram",
            kind="LOCATION",
            text="40.7128, -74.0060",
            message_id="l1",
            raw=None,
        )
    )
    content = next(tmp_path.rglob("*.md")).read_text()
    assert "· location ·" in content
    assert "40.7128" in content


# ---------------------------------------------------------------------------
# Text path → passthrough + recorded
# ---------------------------------------------------------------------------


def test_text_message_is_recorded_and_passes_through(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    result = r.on_pre_gateway_dispatch(event=_event(text="hey there"))
    # Passthrough — Hermes's native settings decide whether to wake.
    assert result is None

    day_files = list(tmp_path.rglob("*.md"))
    assert len(day_files) == 1
    # Matrix events land under the matrix platform folder.
    assert day_files[0].parent == tmp_path / "matrix" / "testroom"
    content = day_files[0].read_text()
    assert "<!-- event:$evt1:srv -->" in content
    assert "hey there" in content
    assert "stage:received" in content


def test_unsupported_message_type_returns_none(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    assert r.on_pre_gateway_dispatch(event=_event(kind="HOLOGRAM")) is None
    assert list(tmp_path.rglob("*.md")) == []


# ---------------------------------------------------------------------------
# Reaction events → not recorded, not skipped
# ---------------------------------------------------------------------------


def test_reaction_event_is_not_recorded(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    reaction = _event(raw=SimpleNamespace(type=SimpleNamespace(value="m.reaction")))
    assert r.on_pre_gateway_dispatch(event=reaction) is None
    # No day file written.
    assert list(tmp_path.rglob("*.md")) == []


# ---------------------------------------------------------------------------
# Sync-replay short-circuit → don't double-record
# ---------------------------------------------------------------------------


def test_duplicate_event_id_is_not_double_recorded(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    event = _event(text="hi")
    assert r.on_pre_gateway_dispatch(event=event) is None
    # Second delivery — vault should still have exactly one section.
    assert r.on_pre_gateway_dispatch(event=event) is None
    day_file = next(tmp_path.rglob("*.md"))
    assert day_file.read_text().count("<!-- event:$evt1:srv -->") == 1


# ---------------------------------------------------------------------------
# Voice path → transcribe + rewrite event.text
# ---------------------------------------------------------------------------


def test_voice_uses_local_media_path_when_available(tmp_path: Path) -> None:
    """When the adapter has already cached + decrypted the audio bytes
    to disk, the recorder hands the file path straight to the
    transcriber instead of trying to download via mxc URL."""
    audio_file = tmp_path / "voice.ogg"
    audio_file.write_bytes(b"OggS\x00...")

    tx = _FakeTranscriber(return_value="from local path")
    r = _build_recorder(tmp_path, transcriber=tx, download_media=None)
    event = _event(
        kind="AUDIO",
        text="",
        message_id="$evtlocal:srv",
        media_urls=[str(audio_file)],
        raw=_audio_raw(),
    )
    result = r.on_pre_gateway_dispatch(event=event)
    assert result == {"action": "rewrite", "text": "from local path"}
    # Transcriber was handed the cached path verbatim.
    assert tx.calls == [str(audio_file)]


def test_telegram_voice_transcribed_via_cached_file(tmp_path: Path) -> None:
    """Non-Matrix platforms have no mxc fallback at all — the cached
    local file IS the only media source, and it must be sufficient."""
    audio_file = tmp_path / "tg-voice.oga"
    audio_file.write_bytes(b"OggS\x00telegram")

    tx = _FakeTranscriber(return_value="telegram voice note text")
    r = _build_recorder(tmp_path, transcriber=tx, download_media=None)
    result = r.on_pre_gateway_dispatch(
        event=_event(
            platform="telegram",
            kind="VOICE",
            text="",
            chat_id="-1001",
            sender="5551212",
            message_id="v9",
            media_urls=[str(audio_file)],
            raw=None,
        )
    )
    assert result == {"action": "rewrite", "text": "telegram voice note text"}
    content = next(tmp_path.rglob("*.md")).read_text()
    assert "stage:transcribed" in content


def test_voice_transcribed_rewrites_event_text(tmp_path: Path) -> None:
    tx = _FakeTranscriber(return_value="hey transcribed message")
    r = _build_recorder(tmp_path, transcriber=tx, download_media=lambda mxc: b"audiobytes")
    event = _event(kind="AUDIO", text="", raw=_audio_raw())
    result = r.on_pre_gateway_dispatch(event=event)
    assert result == {"action": "rewrite", "text": "hey transcribed message"}

    day_file = next(tmp_path.rglob("*.md"))
    content = day_file.read_text()
    assert "stage:transcribed" in content
    assert "hey transcribed message" in content
    assert content.count("<!-- event:$evt1:srv -->") == 1


def test_voice_without_media_writes_failed_section_with_placeholder_rewrite(
    tmp_path: Path,
) -> None:
    r = _build_recorder(tmp_path, transcriber=_FakeTranscriber(), download_media=None)
    result = r.on_pre_gateway_dispatch(event=_event(kind="AUDIO", text="", raw=_audio_raw()))
    assert result is not None
    assert result["action"] == "rewrite"
    assert "transcription failed" in result["text"]
    content = next(tmp_path.rglob("*.md")).read_text()
    assert "stage:transcribe_failed" in content


def test_voice_with_download_failure_writes_failed_section(tmp_path: Path) -> None:
    def _bad_download(mxc):
        raise OSError("network down")

    r = _build_recorder(
        tmp_path, transcriber=_FakeTranscriber(), download_media=_bad_download
    )
    result = r.on_pre_gateway_dispatch(event=_event(kind="AUDIO", text="", raw=_audio_raw()))
    assert result is not None
    assert "transcription failed" in result["text"]
    content = next(tmp_path.rglob("*.md")).read_text()
    assert "stage:transcribe_failed" in content
    assert "network down" in content


def test_voice_with_transcriber_exception_marks_failed(tmp_path: Path) -> None:
    tx = _FakeTranscriber(raise_exc=TranscriberError("bad audio"))
    r = _build_recorder(tmp_path, transcriber=tx, download_media=lambda m: b"x")
    result = r.on_pre_gateway_dispatch(event=_event(kind="AUDIO", text="", raw=_audio_raw()))
    assert result is not None
    assert "transcription failed" in result["text"]
    content = next(tmp_path.rglob("*.md")).read_text()
    assert "stage:transcribe_failed" in content
    assert "bad audio" in content


# ---------------------------------------------------------------------------
# Image path → describe + rewrite event.text
# ---------------------------------------------------------------------------


def test_image_uses_local_media_path_when_available(tmp_path: Path) -> None:
    """Same shortcut for images: read the locally-cached file rather
    than re-downloading from the mxc URL."""
    image_file = tmp_path / "pic.png"
    image_file.write_bytes(b"\x89PNG\r\n\x1a\nlocalbytes")

    desc = _FakeDescriber(
        return_value=DescribeResult(description="Local image", text="", raw="")
    )
    r = _build_recorder(tmp_path, describer=desc, download_media=None)
    event = _event(
        kind="IMAGE",
        text="hey look",
        message_id="$imglocal:srv",
        media_urls=[str(image_file)],
        raw=_image_raw(),
    )
    result = r.on_pre_gateway_dispatch(event=event)
    assert result is not None
    assert "Local image" in result["text"]
    # Describer saw the actual cached bytes.
    assert desc.calls == [b"\x89PNG\r\n\x1a\nlocalbytes"]


def test_image_described_rewrites_with_caption_and_description(tmp_path: Path) -> None:
    desc = _FakeDescriber(
        return_value=DescribeResult(
            description="A whiteboard photo from a low angle.",
            text="Pilot scope / Risk",
            raw="",
        )
    )
    r = _build_recorder(
        tmp_path,
        describer=desc,
        download_media=lambda m: b"\x89PNG",
    )
    event = _event(kind="IMAGE", text="check this out", raw=_image_raw())
    result = r.on_pre_gateway_dispatch(event=event)
    assert result is not None
    assert result["action"] == "rewrite"
    assert "check this out" in result["text"]
    assert "A whiteboard photo" in result["text"]
    assert "Pilot scope" in result["text"]

    content = next(tmp_path.rglob("*.md")).read_text()
    assert "stage:described" in content


def test_image_describer_failure_falls_back_to_caption(tmp_path: Path) -> None:
    desc = _FakeDescriber(raise_exc=ImageDescriberError("vision provider down"))
    r = _build_recorder(
        tmp_path,
        describer=desc,
        download_media=lambda m: b"\x89PNG",
    )
    event = _event(kind="IMAGE", text="check this", raw=_image_raw())
    result = r.on_pre_gateway_dispatch(event=event)
    assert result is not None
    assert result["text"].startswith("check this")
    content = next(tmp_path.rglob("*.md")).read_text()
    assert "stage:describe_failed" in content
    assert "vision provider down" in content


# ---------------------------------------------------------------------------
# Outbound recording
# ---------------------------------------------------------------------------


def test_record_outbound_writes_reply_section(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path, bot_name="Recorder Bot")
    ts = datetime(2026, 5, 26, 14, 30, tzinfo=UTC)
    r.record_outbound(
        platform="matrix",
        chat_id=ROOM,
        text="On it.",
        event_id="$reply:srv",
        timestamp=ts,
        reply_to_event_id="$inbound:srv",
    )
    day_file = next(tmp_path.rglob("*.md"))
    assert day_file.parent == tmp_path / "matrix" / "testroom"
    content = day_file.read_text()
    assert "<!-- event:$reply:srv -->" in content
    assert "stage:sent" in content
    assert "**reply_to:** $inbound:srv" in content
    assert "On it." in content
    assert "Recorder Bot" in content


def test_record_outbound_lands_in_platform_folder(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    ts = datetime(2026, 5, 26, 14, 30, tzinfo=UTC)
    r.record_outbound(
        platform="telegram", chat_id="-1001", text="ack", event_id="55", timestamp=ts
    )
    assert (tmp_path / "telegram" / "1001").is_dir()


def test_record_outbound_no_op_when_disabled(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path, record_outbound=False)
    ts = datetime(2026, 5, 26, 14, 30, tzinfo=UTC)
    r.record_outbound(
        platform="matrix", chat_id=ROOM, text="hi", event_id="$x", timestamp=ts
    )
    assert list(tmp_path.rglob("*.md")) == []


def test_record_outbound_respects_platform_allowlist(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path, platforms=frozenset({"matrix"}))
    ts = datetime(2026, 5, 26, 14, 30, tzinfo=UTC)
    r.record_outbound(
        platform="telegram", chat_id="-1001", text="ack", event_id="55", timestamp=ts
    )
    assert list(tmp_path.rglob("*.md")) == []


# ---------------------------------------------------------------------------
# Vault placeholder failure short-circuits
# ---------------------------------------------------------------------------


def test_vault_placeholder_write_failure_returns_none(tmp_path: Path) -> None:
    """If the vault write fails for the placeholder, we MUST NOT then
    try to process media or rewrite — return None so the gateway
    dispatches normally and the failure is loud in logs."""

    r = _build_recorder(tmp_path)

    def _boom(section, *, path_slug):
        raise OSError("disk full")

    r.writer.write_section = _boom  # type: ignore[method-assign]
    result = r.on_pre_gateway_dispatch(event=_event(text="hi"))
    assert result is None


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def test_set_download_media_updates_handle(tmp_path: Path) -> None:
    """Default no download → voice section is transcribe_failed.
    After set_download_media, a real handle lets transcription proceed."""
    tx = _FakeTranscriber(return_value="ok then")
    r = _build_recorder(tmp_path, transcriber=tx, download_media=None)

    event_a = _event(kind="AUDIO", text="", raw=_audio_raw(), message_id="$a")
    result_a = r.on_pre_gateway_dispatch(event=event_a)
    assert "transcription failed" in result_a["text"]

    r.set_download_media(lambda mxc: b"bytes")
    event_b = _event(kind="AUDIO", text="", raw=_audio_raw(), message_id="$b")
    result_b = r.on_pre_gateway_dispatch(event=event_b)
    assert result_b == {"action": "rewrite", "text": "ok then"}


def test_set_bot_mxid_updates_outbound_display(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path, bot_mxid="")
    r.set_bot_mxid("@new_bot:srv")
    assert r.bot_mxid == "@new_bot:srv"


def test_hook_accepts_unknown_future_kwargs(tmp_path: Path) -> None:
    """Forward compatibility: Hermes may add hook kwargs at any time."""
    r = _build_recorder(tmp_path)
    result = r.on_pre_gateway_dispatch(
        event=_event(text="hi"), gateway=None, session_store=None, shiny_new_arg=42
    )
    assert result is None
    assert len(list(tmp_path.rglob("*.md"))) == 1


# ---------------------------------------------------------------------------
# Name resolution — pretty folder + pretty section header
# ---------------------------------------------------------------------------


def test_pretty_chat_folder_when_resolver_has_room_name(tmp_path: Path) -> None:
    resolver = NameResolver(room_name_lookup=lambda _: "Matt & Annika")
    r = _build_recorder(tmp_path, resolver=resolver)
    r.on_pre_gateway_dispatch(event=_event(text="hello"))

    assert (tmp_path / "matrix" / "Matt-and-Annika").is_dir()


def test_chat_name_hint_used_without_any_lookup(tmp_path: Path) -> None:
    """source.chat_name alone is enough for a pretty folder — no Matrix
    client wiring required."""
    r = _build_recorder(tmp_path)
    r.on_pre_gateway_dispatch(event=_event(text="hello", chat_name="Ops War Room"))
    assert (tmp_path / "matrix" / "Ops-War-Room").is_dir()


def test_pretty_sender_in_section_header(tmp_path: Path) -> None:
    resolver = NameResolver(
        user_name_lookup=lambda uid: {SENDER: "Annika R."}.get(uid),
    )
    r = _build_recorder(tmp_path, resolver=resolver)
    r.on_pre_gateway_dispatch(event=_event(text="hi", sender=SENDER))

    content = next(tmp_path.rglob("*.md")).read_text()
    assert "Annika R." in content
    # The raw ID must NOT appear in the section header line.
    header_line = next(line for line in content.splitlines() if line.startswith("### "))
    assert SENDER not in header_line


def test_outbound_resolves_bot_display_name_via_resolver(tmp_path: Path) -> None:
    resolver = NameResolver(
        user_name_lookup=lambda uid: "Recorder Bot" if uid == BOT else None,
    )
    r = _build_recorder(tmp_path, resolver=resolver)
    ts = datetime(2026, 5, 26, 14, 30, tzinfo=UTC)
    r.record_outbound(
        platform="matrix",
        chat_id=ROOM,
        sender_display=BOT,  # caller passed a raw MXID — recorder MUST upgrade
        text="On it.",
        event_id="$reply:srv",
        timestamp=ts,
    )
    header_line = next(
        line
        for line in next(tmp_path.rglob("*.md")).read_text().splitlines()
        if line.startswith("### ")
    )
    assert "Recorder Bot" in header_line
    assert BOT not in header_line


def test_outbound_respects_explicit_friendly_sender(tmp_path: Path) -> None:
    """When the caller passes a real display name, honor it (don't
    override with config or resolver)."""
    r = _build_recorder(tmp_path, bot_name="Config Name")
    ts = datetime(2026, 5, 26, 14, 30, tzinfo=UTC)
    r.record_outbound(
        platform="matrix",
        chat_id=ROOM,
        sender_display="Caller Override",
        text="hi",
        event_id="$rep:srv",
        timestamp=ts,
    )
    content = next(tmp_path.rglob("*.md")).read_text()
    assert "Caller Override" in content
    assert "Config Name" not in content


def test_outbound_falls_back_to_literal_bot(tmp_path: Path) -> None:
    """No bot_name, no bot_mxid, no resolver hit → sender is "bot"."""
    r = _build_recorder(tmp_path, bot_mxid="")
    ts = datetime(2026, 5, 26, 14, 30, tzinfo=UTC)
    r.record_outbound(
        platform="telegram", chat_id="-1", text="hi", event_id="x", timestamp=ts
    )
    header_line = next(
        line
        for line in next(tmp_path.rglob("*.md")).read_text().splitlines()
        if line.startswith("### ")
    )
    assert " bot " in header_line


def test_edit_event_writes_edited_section_linked_to_original(tmp_path: Path) -> None:
    """Matrix m.replace events get their own anchor, link back to the
    original via the ``edits:`` field, and use stage:edited. (Dormant
    in production — Hermes's current Matrix adapter filters edits.)"""
    r = _build_recorder(tmp_path)
    edit_event = _event(
        text="",  # edit events deliver new body via content.m.new_content
        message_id="$edit:srv",
        raw=SimpleNamespace(
            origin_server_ts=1716729240000,
            content={
                "body": "* corrected wording",
                "m.new_content": {"body": "corrected wording"},
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$original:srv",
                },
            },
        ),
    )
    result = r.on_pre_gateway_dispatch(event=edit_event)
    # Edits don't rewrite event.text — let Hermes deliver the new body
    # as-is and the agent can re-evaluate if it cares.
    assert result is None

    content = next(tmp_path.rglob("*.md")).read_text()
    assert "<!-- event:$edit:srv -->" in content
    assert "stage:edited" in content
    assert "**edits:** $original:srv" in content
    assert "corrected wording" in content


def test_lazy_gateway_wire_fires_on_first_dispatch_only(tmp_path: Path) -> None:
    """The wiring callback fires exactly once — on the first
    pre_gateway_dispatch with a gateway. Subsequent dispatches don't
    re-fire it (avoiding double-wrapping ``adapter.send``)."""
    cfg = RecorderConfig(vault_root=tmp_path)
    writer = VaultWriter(vault_root=tmp_path, timezone="UTC")
    calls: list = []
    r = Recorder(
        config=cfg,
        writer=writer,
        wire_gateway_once=lambda gw: calls.append(gw),
    )

    fake_gateway_1 = object()
    fake_gateway_2 = object()

    r.on_pre_gateway_dispatch(event=_event(text="hi"), gateway=fake_gateway_1)
    r.on_pre_gateway_dispatch(event=_event(text="again", message_id="$2"), gateway=fake_gateway_2)
    r.on_pre_gateway_dispatch(event=_event(text="third", message_id="$3"), gateway=None)

    assert calls == [fake_gateway_1]


def test_lazy_gateway_wire_swallows_exceptions(tmp_path: Path) -> None:
    """If wire-once raises, dispatch must still proceed normally."""
    cfg = RecorderConfig(vault_root=tmp_path)
    writer = VaultWriter(vault_root=tmp_path, timezone="UTC")

    def _boom(_):
        raise RuntimeError("wiring exploded")

    r = Recorder(config=cfg, writer=writer, wire_gateway_once=_boom)
    # Should NOT raise. The text message still gets recorded.
    result = r.on_pre_gateway_dispatch(event=_event(text="hi"), gateway=object())
    assert result is None
    assert next(tmp_path.rglob("*.md")).read_text().count("hi") >= 1


def test_fallback_to_id_slug_when_resolver_blank(tmp_path: Path) -> None:
    """With no hints or lookups, the chat folder derives from the raw
    chat ID."""
    r = _build_recorder(tmp_path)  # default resolver, no lookups
    r.on_pre_gateway_dispatch(event=_event(text="hi"))

    # !testroom:srv → "testroom", under the matrix platform folder.
    assert (tmp_path / "matrix" / "testroom").is_dir()
