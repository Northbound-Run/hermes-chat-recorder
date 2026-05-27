"""Tests for the Recorder orchestrator (post wake-gate removal).

Uses real VaultWriter (tmp_path); injects fakes for the transcriber,
describer, and media downloader. The recorder no longer makes wake
decisions — every message is recorded, and the hook returns either
None (passthrough for text) or a rewrite (for media events).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_chat_recorder.config import RecorderConfig
from hermes_chat_recorder.describer import ImageDescriberError
from hermes_chat_recorder.name_resolver import NameResolver
from hermes_chat_recorder.recorder import Recorder
from hermes_chat_recorder.transcriber import TranscriberError
from hermes_chat_recorder.types import DescribeResult
from hermes_chat_recorder.writer import VaultWriter


ROOM = "!testroom:srv"
BOT = "@ralph:srv"
ANNIKA = "@annika:srv"


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

    def describe(self, image_bytes, *, mime=""):  # noqa: ARG002
        self.calls.append(image_bytes)
        if self.raise_exc:
            raise self.raise_exc
        return self.return_value


def _matrix_msg_type(name: str):
    return SimpleNamespace(name=name)


def _event(
    *,
    kind: str = "TEXT",
    text: str = "hello",
    message_id: str = "$evt1:srv",
    sender: str = ANNIKA,
    raw: Any = None,
    platform: str = "matrix",
):
    return SimpleNamespace(
        text=text,
        message_id=message_id,
        message_type=_matrix_msg_type(kind),
        source=SimpleNamespace(
            platform=SimpleNamespace(value=platform),
            chat_id=ROOM,
            user_id=sender,
        ),
        raw_message=raw or SimpleNamespace(origin_server_ts=1716729240000, content={}),
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
) -> Recorder:
    cfg = RecorderConfig(
        vault_root=tmp_path,
        record_outbound=record_outbound,
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
# Non-matrix / unknown events → None passthrough
# ---------------------------------------------------------------------------


def test_non_matrix_event_returns_none(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    assert r.on_pre_gateway_dispatch(event=_event(platform="telegram")) is None


def test_unknown_message_type_returns_none(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    assert r.on_pre_gateway_dispatch(event=_event(kind="VIDEO")) is None


# ---------------------------------------------------------------------------
# Text path → passthrough + recorded
# ---------------------------------------------------------------------------


def test_text_message_is_recorded_and_passes_through(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    result = r.on_pre_gateway_dispatch(event=_event(text="hey there"))
    # Passthrough — let Hermes's native settings decide whether to wake.
    assert result is None

    day_files = list(tmp_path.rglob("*.md"))
    assert len(day_files) == 1
    content = day_files[0].read_text()
    assert "<!-- event:$evt1:srv -->" in content
    assert "hey there" in content
    assert "stage:received" in content


def test_text_without_any_nicknames_still_recorded_and_passthrough(tmp_path: Path) -> None:
    """The wake gate is gone — recording is unconditional and the hook
    NEVER skips text messages."""
    r = _build_recorder(tmp_path)
    result = r.on_pre_gateway_dispatch(event=_event(text="just thinking out loud"))
    assert result is None
    assert "just thinking out loud" in next(tmp_path.rglob("*.md")).read_text()


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
    """When the Matrix adapter has already cached + decrypted the audio
    bytes to disk, the recorder should hand the file path straight to
    the transcriber instead of trying to download via mxc URL.
    """
    audio_file = tmp_path / "voice.ogg"
    audio_file.write_bytes(b"OggS\x00...")

    tx = _FakeTranscriber(return_value="from local path")
    r = _build_recorder(tmp_path, transcriber=tx, download_media=None)
    event = SimpleNamespace(
        text="",
        message_id="$evtlocal:srv",
        message_type=_matrix_msg_type("AUDIO"),
        source=SimpleNamespace(
            platform=SimpleNamespace(value="matrix"),
            chat_id=ROOM,
            user_id=ANNIKA,
        ),
        raw_message=_audio_raw(),
        media_urls=[str(audio_file)],
    )
    result = r.on_pre_gateway_dispatch(event=event)
    assert result == {"action": "rewrite", "text": "from local path"}
    # Transcriber was handed the cached path verbatim.
    assert tx.calls == [str(audio_file)]


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


def test_voice_without_download_writes_failed_section_with_placeholder_rewrite(
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
        raise IOError("network down")

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
    event = SimpleNamespace(
        text="hey look",
        message_id="$imglocal:srv",
        message_type=_matrix_msg_type("IMAGE"),
        source=SimpleNamespace(
            platform=SimpleNamespace(value="matrix"),
            chat_id=ROOM,
            user_id=ANNIKA,
        ),
        raw_message=_image_raw(),
        media_urls=[str(image_file)],
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
    r = _build_recorder(tmp_path)
    ts = datetime(2026, 5, 26, 14, 30, tzinfo=timezone.utc)
    r.record_outbound(
        room_id=ROOM,
        sender_display="Ralph",
        text="On it.",
        event_id="$reply:srv",
        timestamp=ts,
        reply_to_event_id="$inbound:srv",
    )
    content = next(tmp_path.rglob("*.md")).read_text()
    assert "<!-- event:$reply:srv -->" in content
    assert "stage:sent" in content
    assert "**reply_to:** $inbound:srv" in content
    assert "On it." in content


def test_record_outbound_no_op_when_disabled(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path, record_outbound=False)
    ts = datetime(2026, 5, 26, 14, 30, tzinfo=timezone.utc)
    r.record_outbound(
        room_id=ROOM, sender_display="Ralph", text="hi", event_id="$x", timestamp=ts
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

    def _boom(section, *, room_slug):  # noqa: ARG001
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


# ---------------------------------------------------------------------------
# Name resolver integration — pretty folder + pretty section header
# ---------------------------------------------------------------------------


def test_pretty_room_folder_when_resolver_has_room_name(tmp_path: Path) -> None:
    resolver = NameResolver(room_name_lookup=lambda _: "Matt & Annika")
    r = _build_recorder(tmp_path, resolver=resolver)
    r.on_pre_gateway_dispatch(event=_event(text="hello"))

    folders = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(folders) == 1
    assert folders[0].name == "Matt-and-Annika"


def test_pretty_sender_in_section_header(tmp_path: Path) -> None:
    resolver = NameResolver(
        room_name_lookup=lambda _: "Matt & Annika",
        user_name_lookup=lambda mxid: {ANNIKA: "Annika R.", BOT: "Ralph"}.get(mxid),
    )
    r = _build_recorder(tmp_path, resolver=resolver)
    r.on_pre_gateway_dispatch(event=_event(text="hi", sender=ANNIKA))

    content = next(tmp_path.rglob("*.md")).read_text()
    assert "Annika R." in content
    # The MXID must NOT appear in the section header line.
    header_line = next(line for line in content.splitlines() if line.startswith("### "))
    assert ANNIKA not in header_line


def test_outbound_resolves_bot_display_name_via_resolver(tmp_path: Path) -> None:
    resolver = NameResolver(
        user_name_lookup=lambda mxid: "Ralph" if mxid == BOT else None,
    )
    r = _build_recorder(tmp_path, resolver=resolver)
    ts = datetime(2026, 5, 26, 14, 30, tzinfo=timezone.utc)
    r.record_outbound(
        room_id=ROOM,
        sender_display=BOT,  # caller passed an MXID — recorder MUST upgrade
        text="On it.",
        event_id="$reply:srv",
        timestamp=ts,
    )
    header_line = next(
        line
        for line in next(tmp_path.rglob("*.md")).read_text().splitlines()
        if line.startswith("### ")
    )
    assert "Ralph" in header_line
    assert BOT not in header_line


def test_outbound_respects_explicit_friendly_sender(tmp_path: Path) -> None:
    """When the caller passes a real display name, honor it (don't override
    with the resolver). This keeps the API stable for tests / callers
    that have already done the lookup themselves."""
    resolver = NameResolver(user_name_lookup=lambda _: "Resolver Result")
    r = _build_recorder(tmp_path, resolver=resolver)
    ts = datetime(2026, 5, 26, 14, 30, tzinfo=timezone.utc)
    r.record_outbound(
        room_id=ROOM,
        sender_display="Caller Override",
        text="hi",
        event_id="$rep:srv",
        timestamp=ts,
    )
    content = next(tmp_path.rglob("*.md")).read_text()
    assert "Caller Override" in content
    assert "Resolver Result" not in content


def test_edit_event_writes_edited_section_linked_to_original(tmp_path: Path) -> None:
    """Matrix m.replace events get their own anchor, link back to the
    original via the ``edits:`` field, and use stage:edited."""
    r = _build_recorder(tmp_path)
    edit_event = SimpleNamespace(
        text="",  # edit events deliver new body via content.m.new_content
        message_id="$edit:srv",
        message_type=_matrix_msg_type("TEXT"),
        source=SimpleNamespace(
            platform=SimpleNamespace(value="matrix"),
            chat_id=ROOM,
            user_id=ANNIKA,
        ),
        raw_message=SimpleNamespace(
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


def test_fallback_to_slug_from_room_id_when_resolver_blank(tmp_path: Path) -> None:
    """With no lookups wired, the room folder reverts to the legacy
    `room_slug_from_room_id` behavior."""
    r = _build_recorder(tmp_path)  # default resolver, no lookups
    r.on_pre_gateway_dispatch(event=_event(text="hi"))

    folders = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(folders) == 1
    # !testroom:srv → "testroom"
    assert folders[0].name == "testroom"
