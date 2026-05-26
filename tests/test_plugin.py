"""Tests for plugin.register() + on_session_start wiring.

Covers the duck-typed ctx surface, hook binding, disabled-config no-op,
config validation surface, and the on_session_start path that finds the
live Matrix adapter, sets bot_mxid, captures the download handle, and
wraps adapter.send for outbound recording.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_chat_recorder.config import ConfigError
from hermes_chat_recorder.plugin import register


def _build_ctx(plugin_block: dict, hooks: list) -> Any:
    return SimpleNamespace(
        config={"plugins": {"chat_recorder": plugin_block}},
        register_hook=lambda name, cb: hooks.append((name, cb)),
    )


# ---------------------------------------------------------------------------
# Basic register() behaviour
# ---------------------------------------------------------------------------


def test_register_returns_recorder_and_binds_two_hooks(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"enabled": True, "vault_root": str(tmp_path), "nicknames": ["ralph"]}, hooks)

    recorder = register(ctx)
    assert recorder is not None
    bound_names = {name for name, _ in hooks}
    assert bound_names == {"pre_gateway_dispatch", "on_session_start"}


def test_register_returns_none_when_disabled(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"enabled": False, "vault_root": str(tmp_path)}, hooks)
    assert register(ctx) is None
    assert hooks == []


def test_register_raises_on_bad_config() -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": 12345}, hooks)
    with pytest.raises(ConfigError, match="vault_root"):
        register(ctx)
    assert hooks == []


def test_register_returns_recorder_even_without_register_hook(tmp_path: Path) -> None:
    """Hermes plugin loader contract is loose — defensive fallback."""
    ctx = SimpleNamespace(
        config={"plugins": {"chat_recorder": {"vault_root": str(tmp_path)}}},
    )
    recorder = register(ctx)
    assert recorder is not None  # loaded but inert


def test_register_with_no_config_attr_uses_defaults(tmp_path: Path) -> None:
    hooks: list = []
    # ctx has register_hook but no config attr at all.
    ctx = SimpleNamespace(register_hook=lambda name, cb: hooks.append((name, cb)))
    recorder = register(ctx)
    assert recorder is not None
    assert {name for name, _ in hooks} == {"pre_gateway_dispatch", "on_session_start"}


# ---------------------------------------------------------------------------
# Hook binding mechanics
# ---------------------------------------------------------------------------


def test_bound_pre_gateway_dispatch_callable_with_event_kwarg(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)

    pre_dispatch = next(cb for name, cb in hooks if name == "pre_gateway_dispatch")
    # Non-matrix event → callback returns None.
    event = SimpleNamespace(
        text="x",
        message_id="$x",
        message_type=SimpleNamespace(name="TEXT"),
        source=SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id="c", user_id="u"),
        raw_message=SimpleNamespace(),
    )
    assert pre_dispatch(event=event, gateway=None, session_store=None) is None


# ---------------------------------------------------------------------------
# on_session_start wiring — sync adapter (simpler)
# ---------------------------------------------------------------------------


@dataclass
class _FakeSendResult:
    event_id: str


@dataclass
class _FakeSyncAdapter:
    user_id: str = "@ralph:srv"
    sent: list = field(default_factory=list)

    def send(self, chat_id: str, text: str, **kwargs):
        self.sent.append((chat_id, text))
        return _FakeSendResult(event_id=f"$outbound{len(self.sent)}:srv")

    def download_media(self, mxc: str) -> bytes:
        return b"AUDIO_BYTES"


def _build_gateway(adapter: Any) -> Any:
    return SimpleNamespace(adapters={"matrix": adapter})


def test_on_session_start_binds_bot_mxid_and_wraps_send(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path), "nicknames": ["ralph"]}, hooks)
    recorder = register(ctx)
    assert recorder is not None

    on_start = next(cb for name, cb in hooks if name == "on_session_start")
    adapter = _FakeSyncAdapter(user_id="@ralph:srv")
    gateway = _build_gateway(adapter)

    on_start(gateway=gateway)

    # bot_mxid carried over from adapter.user_id
    assert recorder.bot_mxid == "@ralph:srv"

    # send is wrapped — calling it persists an outbound section.
    adapter.send("!room:srv", "ack")
    day_file = next(tmp_path.rglob("*.md"))
    content = day_file.read_text()
    assert "<!-- event:$outbound1:srv -->" in content
    assert "stage:sent" in content
    assert "ack" in content


def test_on_session_start_wrap_is_idempotent(tmp_path: Path) -> None:
    """Calling on_session_start twice (gateway restart) must NOT
    double-wrap and double-record."""
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    assert recorder is not None
    on_start = next(cb for name, cb in hooks if name == "on_session_start")

    adapter = _FakeSyncAdapter()
    gateway = _build_gateway(adapter)
    on_start(gateway=gateway)
    on_start(gateway=gateway)

    adapter.send("!room:srv", "hi")
    content = next(tmp_path.rglob("*.md")).read_text()
    # Exactly ONE outbound section, not two.
    assert content.count("<!-- event:") == 1


def test_on_session_start_with_missing_gateway_kwarg_is_safe(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    on_start = next(cb for name, cb in hooks if name == "on_session_start")
    # No exception, no crash — just logs and continues.
    on_start()
    on_start(gateway=None)


def test_on_session_start_with_no_matrix_adapter_is_safe(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    on_start = next(cb for name, cb in hooks if name == "on_session_start")

    gateway = SimpleNamespace(adapters={"telegram": object()})
    on_start(gateway=gateway)  # noop, no exception


def test_on_session_start_resolves_download_callable(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    assert recorder is not None
    on_start = next(cb for name, cb in hooks if name == "on_session_start")

    adapter = _FakeSyncAdapter()
    gateway = _build_gateway(adapter)
    on_start(gateway=gateway)

    # The download callable now points at the adapter's download_media.
    assert recorder._download_media is not None  # noqa: SLF001 - test internal
    assert recorder._download_media("mxc://x") == b"AUDIO_BYTES"  # noqa: SLF001


# ---------------------------------------------------------------------------
# on_session_start — async adapter shape
# ---------------------------------------------------------------------------


class _FakeAsyncAdapter:
    def __init__(self):
        self.user_id = "@ralph:srv"
        self.sent: list = []

    async def send(self, chat_id: str, text: str, **kwargs):
        self.sent.append((chat_id, text))
        return _FakeSendResult(event_id=f"$async{len(self.sent)}:srv")

    async def download_media(self, mxc: str) -> bytes:  # noqa: ARG002
        return b"AUDIO_BYTES_ASYNC"


def test_on_session_start_handles_async_send_adapter(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    on_start = next(cb for name, cb in hooks if name == "on_session_start")

    adapter = _FakeAsyncAdapter()
    gateway = _build_gateway(adapter)
    on_start(gateway=gateway)

    # Drive the async wrapper from an event loop.
    asyncio.run(adapter.send("!room:srv", "hi from async"))
    content = next(tmp_path.rglob("*.md")).read_text()
    assert "<!-- event:$async1:srv -->" in content
    assert "hi from async" in content


# ---------------------------------------------------------------------------
# Adapter mxid resolution variants
# ---------------------------------------------------------------------------


def test_bot_mxid_resolved_via_config_attr(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    assert recorder is not None
    on_start = next(cb for name, cb in hooks if name == "on_session_start")

    adapter = SimpleNamespace(
        config=SimpleNamespace(user_id="@from_config:srv"),
        send=lambda *a, **k: _FakeSendResult(event_id="$x:srv"),
    )
    on_start(gateway=_build_gateway(adapter))
    assert recorder.bot_mxid == "@from_config:srv"
