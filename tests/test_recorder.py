"""Tests for the Recorder orchestrator.

Uses real VaultWriter (tmp_path) and Gate; injects fakes for the
transcriber, describer, and media downloader. Exercises every branch
of on_pre_gateway_dispatch + record_outbound.
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
from hermes_chat_recorder.gate import Gate
from hermes_chat_recorder.recorder import Recorder
from hermes_chat_recorder.transcriber import TranscriberError
from hermes_chat_recorder.types import DescribeResult
from hermes_chat_recorder.writer import VaultWriter


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

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
    """Build a fake Hermes MessageEvent."""
    return SimpleNamespace(
        text=text,
        message_id=message_id,
        message_type=_matrix_msg_type(kind),
        source=SimpleNamespace(
            platform=SimpleNamespace(value=platform),
            chat_id=ROOM,
            user_id=sender,
        ),
        raw_message=raw
        or SimpleNamespace(origin_server_ts=1716729240000, content={}),
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
    nicknames: list[str] = ["ralph"],
    bot_mxid: str = BOT,
    transcriber: Any = None,
    describer: Any = None,
    download_media: Any = None,
    openrouter_api_key: str = "",
    record_outbound: bool = True,
) -> Recorder:
    cfg = RecorderConfig(
        vault_root=tmp_path,
        nicknames=tuple(nicknames),
        openrouter_api_key=openrouter_api_key,
        record_outbound=record_outbound,
    )
    writer = VaultWriter(vault_root=tmp_path, timezone="UTC")
    gate = Gate(nicknames)
    return Recorder(
        config=cfg,
        writer=writer,
        gate=gate,
        transcriber=transcriber,
        describer=describer,
        bot_mxid=bot_mxid,
        download_media=download_media,
    )


# ---------------------------------------------------------------------------
# Non-Matrix events
# ---------------------------------------------------------------------------


def test_non_matrix_event_returns_none(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    result = r.on_pre_gateway_dispatch(event=_event(platform="telegram"))
    assert result is None


def test_unknown_message_type_returns_none(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    result = r.on_pre_gateway_dispatch(event=_event(kind="VIDEO"))
    assert result is None


# ---------------------------------------------------------------------------
# Text path
# ---------------------------------------------------------------------------


def test_text_nickname_match_records_and_allows(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    result = r.on_pre_gateway_dispatch(event=_event(text="hey ralph, status?"))
    assert result == {"action": "allow"}

    day_files = list(tmp_path.rglob("*.md"))
    assert len(day_files) == 1
    content = day_files[0].read_text()
    assert "<!-- event:$evt1:srv -->" in content
    assert "hey ralph, status?" in content
    assert "stage:received" in content


def test_text_without_nickname_skips(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    result = r.on_pre_gateway_dispatch(event=_event(text="just thinking out loud"))
    assert result == {"action": "skip", "reason": "no-mention-or-nickname"}

    # Still recorded, even though we declined to wake.
    day_files = list(tmp_path.rglob("*.md"))
    assert len(day_files) == 1
    assert "just thinking out loud" in day_files[0].read_text()


def test_text_at_mention_wakes_without_nickname(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path, nicknames=[])
    event = _event(
        text="quick q",
        raw=SimpleNamespace(
            origin_server_ts=1716729240000,
            content={"m.mentions": {"user_ids": [BOT]}},
        ),
    )
    result = r.on_pre_gateway_dispatch(event=event)
    assert result == {"action": "allow"}


def test_text_self_echo_does_not_wake(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    # Same as the inbound test but sender == bot.
    result = r.on_pre_gateway_dispatch(event=_event(text="ralph here", sender=BOT))
    assert result == {"action": "skip", "reason": "no-mention-or-nickname"}


# ---------------------------------------------------------------------------
# Reactions are skipped
# ---------------------------------------------------------------------------


def test_reaction_event_is_skipped_and_not_recorded(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    reaction = _event(raw=SimpleNamespace(type=SimpleNamespace(value="m.reaction")))
    result = r.on_pre_gateway_dispatch(event=reaction)
    assert result == {"action": "skip", "reason": "reaction-not-recorded"}
    # No day file written.
    assert list(tmp_path.rglob("*.md")) == []


# ---------------------------------------------------------------------------
# Sync-replay short-circuit
# ---------------------------------------------------------------------------


def test_duplicate_event_id_is_skipped_on_second_delivery(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path)
    event = _event(text="hi ralph")
    first = r.on_pre_gateway_dispatch(event=event)
    assert first == {"action": "allow"}
    second = r.on_pre_gateway_dispatch(event=event)
    assert second == {"action": "skip", "reason": "sync-replay-duplicate"}

    # File still has exactly one section.
    day_file = next(tmp_path.rglob("*.md"))
    assert day_file.read_text().count("<!-- event:$evt1:srv -->") == 1


# ---------------------------------------------------------------------------
# Voice path
# ---------------------------------------------------------------------------


def test_voice_transcribe_and_wake_on_nickname(tmp_path: Path) -> None:
    tx = _FakeTranscriber(return_value="hey ralph what about the deck")
    r = _build_recorder(
        tmp_path,
        transcriber=tx,
        download_media=lambda mxc: b"audiobytes",
    )
    event = _event(kind="AUDIO", text="", raw=_audio_raw())
    result = r.on_pre_gateway_dispatch(event=event)
    assert result == {
        "action": "rewrite",
        "text": "hey ralph what about the deck",
    }

    # Transcript reached faster-whisper exactly once.
    assert len(tx.calls) == 1
    day_file = next(tmp_path.rglob("*.md"))
    content = day_file.read_text()
    assert "stage:transcribed" in content
    assert "hey ralph what about the deck" in content
    # Placeholder section was replaced — only one anchor.
    assert content.count("<!-- event:$evt1:srv -->") == 1


def test_voice_transcribe_no_nickname_does_not_wake(tmp_path: Path) -> None:
    tx = _FakeTranscriber(return_value="just a random thought")
    r = _build_recorder(
        tmp_path,
        transcriber=tx,
        download_media=lambda mxc: b"audiobytes",
    )
    event = _event(kind="AUDIO", text="", raw=_audio_raw())
    result = r.on_pre_gateway_dispatch(event=event)
    assert result == {"action": "skip", "reason": "no-mention-or-nickname"}

    day_file = next(tmp_path.rglob("*.md"))
    content = day_file.read_text()
    assert "stage:transcribed" in content
    assert "just a random thought" in content


def test_voice_with_no_transcriber_writes_failed_section_and_skips(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path, transcriber=None, download_media=lambda m: b"x")
    # Don't lazy-init either; ensure transcriber stays None by setting model size invalid.
    r._transcriber = None  # noqa: SLF001
    # Force lazy init to fail by patching the import target — easier:
    # tell the recorder there's no transcriber AND no download path.
    r.set_download_media(None)

    event = _event(kind="AUDIO", text="", raw=_audio_raw())
    result = r.on_pre_gateway_dispatch(event=event)
    assert result == {"action": "skip", "reason": "no-mention-or-nickname"}

    day_file = next(tmp_path.rglob("*.md"))
    content = day_file.read_text()
    assert "stage:transcribe_failed" in content


def test_voice_with_download_failure_writes_failed_section(tmp_path: Path) -> None:
    tx = _FakeTranscriber()

    def _bad_download(mxc):
        raise IOError("network down")

    r = _build_recorder(tmp_path, transcriber=tx, download_media=_bad_download)
    event = _event(kind="AUDIO", text="", raw=_audio_raw())
    result = r.on_pre_gateway_dispatch(event=event)
    assert result == {"action": "skip", "reason": "no-mention-or-nickname"}

    content = next(tmp_path.rglob("*.md")).read_text()
    assert "stage:transcribe_failed" in content
    assert "network down" in content


def test_voice_with_transcriber_exception_marks_failed(tmp_path: Path) -> None:
    tx = _FakeTranscriber(raise_exc=TranscriberError("bad audio"))
    r = _build_recorder(tmp_path, transcriber=tx, download_media=lambda m: b"x")
    event = _event(kind="AUDIO", text="", raw=_audio_raw())
    result = r.on_pre_gateway_dispatch(event=event)
    assert result == {"action": "skip", "reason": "no-mention-or-nickname"}
    content = next(tmp_path.rglob("*.md")).read_text()
    assert "stage:transcribe_failed" in content
    assert "bad audio" in content


def test_voice_at_mention_wakes_even_with_no_transcript_text(tmp_path: Path) -> None:
    """If the bot is @-mentioned via Matrix mention metadata, the gate
    fires even when the transcript itself doesn't contain a nickname."""
    tx = _FakeTranscriber(return_value="hi everyone")
    r = _build_recorder(
        tmp_path, nicknames=[], transcriber=tx, download_media=lambda m: b"x"
    )
    event = _event(
        kind="AUDIO",
        text="",
        raw=SimpleNamespace(
            origin_server_ts=1716729240000,
            content={
                "url": "mxc://srv/audio",
                "info": {"mimetype": "audio/ogg", "duration": 5000},
                "m.mentions": {"user_ids": [BOT]},
            },
        ),
    )
    result = r.on_pre_gateway_dispatch(event=event)
    assert result == {"action": "rewrite", "text": "hi everyone"}


# ---------------------------------------------------------------------------
# Image path
# ---------------------------------------------------------------------------


def test_image_caption_contains_nickname_wakes(tmp_path: Path) -> None:
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
        openrouter_api_key="sk-or-x",
    )
    event = _event(
        kind="IMAGE", text="ralph take a look at this", raw=_image_raw()
    )
    result = r.on_pre_gateway_dispatch(event=event)
    assert result is not None
    assert result["action"] == "rewrite"
    assert "ralph take a look at this" in result["text"]
    assert "A whiteboard photo" in result["text"]
    assert "Pilot scope" in result["text"]

    content = next(tmp_path.rglob("*.md")).read_text()
    assert "stage:described" in content


def test_image_description_with_ralph_does_NOT_false_wake(tmp_path: Path) -> None:
    """An AI description that mentions Ralph must not wake the agent —
    only caption + OCR'd text count toward the gate decision."""
    desc = _FakeDescriber(
        return_value=DescribeResult(
            description="I see Ralph from accounting in the photo.",
            text="random label",
            raw="",
        )
    )
    r = _build_recorder(
        tmp_path,
        describer=desc,
        download_media=lambda m: b"\x89PNG",
        openrouter_api_key="sk-or-x",
    )
    event = _event(kind="IMAGE", text="here's the whiteboard", raw=_image_raw())
    result = r.on_pre_gateway_dispatch(event=event)
    assert result == {"action": "skip", "reason": "no-mention-or-nickname"}


def test_image_ocr_text_with_nickname_wakes(tmp_path: Path) -> None:
    """OCR'd text from the image (the TEXT: block) IS allowed to wake."""
    desc = _FakeDescriber(
        return_value=DescribeResult(
            description="A handwritten note.",
            text="Please tell Ralph about this",
            raw="",
        )
    )
    r = _build_recorder(
        tmp_path,
        describer=desc,
        download_media=lambda m: b"\x89PNG",
        openrouter_api_key="sk-or-x",
    )
    event = _event(kind="IMAGE", text="", raw=_image_raw())
    result = r.on_pre_gateway_dispatch(event=event)
    assert result is not None
    assert result["action"] == "rewrite"


def test_image_with_no_describer_writes_failed_section(tmp_path: Path) -> None:
    r = _build_recorder(
        tmp_path,
        describer=None,
        download_media=lambda m: b"\x89PNG",
        openrouter_api_key="",  # describer remains None
    )
    event = _event(kind="IMAGE", text="check this", raw=_image_raw())
    result = r.on_pre_gateway_dispatch(event=event)
    assert result == {"action": "skip", "reason": "no-mention-or-nickname"}

    content = next(tmp_path.rglob("*.md")).read_text()
    assert "stage:describe_failed" in content


def test_image_describer_failure_writes_failed_section(tmp_path: Path) -> None:
    desc = _FakeDescriber(raise_exc=ImageDescriberError("openrouter 503"))
    r = _build_recorder(
        tmp_path,
        describer=desc,
        download_media=lambda m: b"\x89PNG",
        openrouter_api_key="sk-or-x",
    )
    event = _event(kind="IMAGE", text="check this ralph", raw=_image_raw())
    result = r.on_pre_gateway_dispatch(event=event)
    # Caption nickname still wakes, even when describer failed.
    assert result is not None
    assert result["action"] in {"rewrite", "allow"}

    content = next(tmp_path.rglob("*.md")).read_text()
    assert "stage:describe_failed" in content
    assert "openrouter 503" in content


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
    day_file = next(tmp_path.rglob("*.md"))
    content = day_file.read_text()
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
# Wiring helpers
# ---------------------------------------------------------------------------


def test_set_bot_mxid_changes_self_echo_target(tmp_path: Path) -> None:
    r = _build_recorder(tmp_path, bot_mxid="")
    # Without bot_mxid, sender == ANNIKA never collides — gate runs nicknames only.
    out = r.on_pre_gateway_dispatch(event=_event(text="hey ralph", sender=ANNIKA))
    assert out == {"action": "allow"}

    # Now set bot_mxid to Annika and re-send the same event. Self-echo guard kicks in.
    r2 = _build_recorder(tmp_path / "v2", bot_mxid=ANNIKA)
    out2 = r2.on_pre_gateway_dispatch(event=_event(text="hey ralph", sender=ANNIKA, message_id="$x2"))
    assert out2 == {"action": "skip", "reason": "no-mention-or-nickname"}


def test_set_download_media_updates_handle(tmp_path: Path) -> None:
    """Default no download → voice section is transcribe_failed.
    After set_download_media, a real handle lets transcription proceed."""
    tx = _FakeTranscriber(return_value="ok ralph")
    r = _build_recorder(tmp_path, transcriber=tx, download_media=None)

    event_a = _event(kind="AUDIO", text="", raw=_audio_raw(), message_id="$a")
    out_a = r.on_pre_gateway_dispatch(event=event_a)
    assert out_a == {"action": "skip", "reason": "no-mention-or-nickname"}

    r.set_download_media(lambda mxc: b"bytes")
    event_b = _event(kind="AUDIO", text="", raw=_audio_raw(), message_id="$b")
    out_b = r.on_pre_gateway_dispatch(event=event_b)
    assert out_b == {"action": "rewrite", "text": "ok ralph"}


# ---------------------------------------------------------------------------
# Vault placeholder failure short-circuits the gate (Codex review fix)
# ---------------------------------------------------------------------------


def test_vault_placeholder_write_failure_returns_none(tmp_path: Path) -> None:
    """If the vault write fails for the placeholder, we MUST NOT then
    apply the wake gate against a missing archive — return None so the
    gateway dispatches normally and the failure is loud in logs."""

    r = _build_recorder(tmp_path)

    # Replace writer.write_section with a function that raises.
    def _boom(section, *, room_slug):  # noqa: ARG001
        raise OSError("disk full")

    r.writer.write_section = _boom  # type: ignore[method-assign]
    result = r.on_pre_gateway_dispatch(event=_event(text="hi ralph"))
    assert result is None


# ---------------------------------------------------------------------------
# transcribe_failure_visible — give the agent something to respond to
# when an @-mentioned voice note can't be transcribed
# ---------------------------------------------------------------------------


def test_transcribe_failure_visible_rewrites_when_at_mentioned(tmp_path: Path) -> None:
    """Voice note fails to transcribe but the bot IS @-mentioned via
    Matrix mention metadata. Gate wakes; the recorder substitutes a
    placeholder message so the agent has something coherent."""
    tx = _FakeTranscriber(raise_exc=TranscriberError("malformed audio"))
    r = _build_recorder(
        tmp_path,
        nicknames=[],
        transcriber=tx,
        download_media=lambda m: b"x",
    )
    event = _event(
        kind="AUDIO",
        text="",
        raw=SimpleNamespace(
            origin_server_ts=1716729240000,
            content={
                "url": "mxc://srv/audio",
                "info": {"mimetype": "audio/ogg", "duration": 5000},
                "m.mentions": {"user_ids": [BOT]},
            },
        ),
    )
    result = r.on_pre_gateway_dispatch(event=event)
    assert result is not None
    assert result["action"] == "rewrite"
    assert "transcription failed" in result["text"]


def test_transcribe_failure_visible_off_returns_allow(tmp_path: Path) -> None:
    """When the operator disables the visible-failure feature, we fall
    back to allow + empty event.text (caller's problem)."""
    cfg_overrides = RecorderConfig(
        vault_root=tmp_path,
        nicknames=(),
        record_outbound=True,
        transcribe_failure_visible=False,
    )
    writer = VaultWriter(vault_root=tmp_path, timezone="UTC")
    gate = Gate([])
    tx = _FakeTranscriber(raise_exc=TranscriberError("malformed audio"))
    r = Recorder(
        config=cfg_overrides,
        writer=writer,
        gate=gate,
        transcriber=tx,
        bot_mxid=BOT,
        download_media=lambda m: b"x",
    )
    event = _event(
        kind="AUDIO",
        text="",
        raw=SimpleNamespace(
            origin_server_ts=1716729240000,
            content={
                "url": "mxc://srv/audio",
                "info": {"mimetype": "audio/ogg", "duration": 5000},
                "m.mentions": {"user_ids": [BOT]},
            },
        ),
    )
    result = r.on_pre_gateway_dispatch(event=event)
    assert result == {"action": "allow"}


def test_image_describe_failure_visible_falls_back_to_caption(tmp_path: Path) -> None:
    """Image describe fails but the user @-mentioned the bot in the
    CAPTION. We should rewrite to the caption + a placeholder note."""

    @dataclass
    class _DescBoom:
        calls: list = field(default_factory=list)

        def describe(self, image_bytes, *, mime=""):  # noqa: ARG002
            from hermes_chat_recorder.describer import ImageDescriberError

            raise ImageDescriberError("openrouter down")

    r = _build_recorder(
        tmp_path,
        describer=_DescBoom(),
        download_media=lambda m: b"IMG",
        openrouter_api_key="sk-or-x",
    )
    event = _event(
        kind="IMAGE",
        text="ralph what is this",
        raw=_image_raw(),
    )
    result = r.on_pre_gateway_dispatch(event=event)
    assert result is not None
    assert result["action"] == "rewrite"
    # Caption preserved because describe_failed + visible-failure flag.
    assert "ralph what is this" in result["text"]
