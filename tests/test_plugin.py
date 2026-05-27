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


def _neutral_event() -> Any:
    """Non-Matrix event the recorder will pass through as ``None``.

    Used by wiring tests to invoke ``pre_gateway_dispatch`` purely for
    its side effect of firing the lazy gateway-wire callback. The
    recorder's matrix-event extractor returns ``None`` for non-Matrix
    platforms, so no vault writes happen and the event flows on.
    """
    return SimpleNamespace(
        text="",
        message_id="$wiring:srv",
        message_type=SimpleNamespace(name="TEXT"),
        source=SimpleNamespace(
            platform=SimpleNamespace(value="telegram"),
            chat_id="!noop",
            user_id="@noop",
        ),
        raw_message=SimpleNamespace(),
    )


def _fire_wiring(pre_dispatch, gateway: Any) -> None:
    """Invoke pre_gateway_dispatch with a neutral event so the recorder
    fires its lazy gateway-wire callback. Mirrors the old
    ``on_start(gateway=gateway)`` semantics for the test suite."""
    pre_dispatch(event=_neutral_event(), gateway=gateway, session_store=None)


def _take(hooks: list, name: str):
    return next(cb for hname, cb in hooks if hname == name)


# ---------------------------------------------------------------------------
# Basic register() behaviour
# ---------------------------------------------------------------------------


def test_register_returns_recorder_and_binds_two_hooks(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"enabled": True, "vault_root": str(tmp_path)}, hooks)

    recorder = register(ctx)
    assert recorder is not None
    bound_names = {name for name, _ in hooks}
    assert bound_names == {"pre_gateway_dispatch"}


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
    assert {name for name, _ in hooks} == {"pre_gateway_dispatch"}


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

    def send(self, chat_id: str, content: str | None = None, **kwargs):
        # Mirror Hermes's canonical signature (see
        # gateway/platforms/matrix.py:929) — keyword `content` is the
        # message body. Older tests still pass positional `(chat_id,
        # text)`, which lands here as `(chat_id, content)`.
        text = content or kwargs.get("text", "") or ""
        self.sent.append((chat_id, text))
        return _FakeSendResult(event_id=f"$outbound{len(self.sent)}:srv")

    def download_media(self, mxc: str) -> bytes:
        return b"AUDIO_BYTES"


def _build_gateway(adapter: Any) -> Any:
    return SimpleNamespace(adapters={"matrix": adapter})


def test_on_session_start_binds_bot_mxid_and_wraps_send(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    assert recorder is not None

    pre_dispatch = _take(hooks, "pre_gateway_dispatch")
    adapter = _FakeSyncAdapter(user_id="@ralph:srv")
    gateway = _build_gateway(adapter)

    _fire_wiring(pre_dispatch, gateway)

    # bot_mxid carried over from adapter.user_id
    assert recorder.bot_mxid == "@ralph:srv"

    # send is wrapped — calling it persists an outbound section.
    adapter.send("!room:srv", "ack")
    day_file = next(tmp_path.rglob("*.md"))
    content = day_file.read_text()
    assert "<!-- event:$outbound1:srv -->" in content
    assert "stage:sent" in content
    assert "ack" in content


def test_wrap_send_handles_hermes_kwarg_call_shape(tmp_path: Path) -> None:
    """Regression: Hermes invokes ``send(chat_id=..., content=...,
    reply_to=..., metadata=...)`` (see gateway/platforms/base.py:2485).
    Earlier versions of our wrapper hardcoded ``text`` as a required
    positional, so this call shape crashed with ``missing 1 required
    positional argument: 'text'`` and the bot never delivered replies.
    """
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")
    adapter = _FakeSyncAdapter(user_id="@ralph:srv")
    _fire_wiring(pre_dispatch, _build_gateway(adapter))

    # Exact call shape Hermes uses — keyword args, with `content`, not `text`.
    result = adapter.send(
        chat_id="!room:srv",
        content="hello from hermes",
        reply_to=None,
        metadata={"thread_id": "$t:srv"},
    )
    assert result.event_id  # didn't crash

    md = next(tmp_path.rglob("*.md")).read_text()
    assert "hello from hermes" in md
    assert "stage:sent" in md


def test_on_session_start_wrap_is_idempotent(tmp_path: Path) -> None:
    """Calling on_session_start twice (gateway restart) must NOT
    double-wrap and double-record."""
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    assert recorder is not None
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    adapter = _FakeSyncAdapter()
    gateway = _build_gateway(adapter)
    _fire_wiring(pre_dispatch, gateway)
    _fire_wiring(pre_dispatch, gateway)

    adapter.send("!room:srv", "hi")
    content = next(tmp_path.rglob("*.md")).read_text()
    # Exactly ONE outbound section, not two.
    assert content.count("<!-- event:") == 1


def test_on_session_start_with_missing_gateway_kwarg_is_safe(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")
    # No exception, no crash — wiring callback simply doesn't fire
    # when no gateway is supplied (matches Hermes's reality: only
    # pre_gateway_dispatch ever delivers the gateway object).
    _fire_wiring(pre_dispatch, None)
    pre_dispatch(event=_neutral_event(), session_store=None)


def test_on_session_start_with_no_matrix_adapter_is_safe(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    gateway = SimpleNamespace(adapters={"telegram": object()})
    _fire_wiring(pre_dispatch, gateway)  # noop, no exception


def test_on_session_start_resolves_download_callable(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    assert recorder is not None
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    adapter = _FakeSyncAdapter()
    gateway = _build_gateway(adapter)
    _fire_wiring(pre_dispatch, gateway)

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

    async def send(self, chat_id: str, content: str | None = None, **kwargs):
        text = content or kwargs.get("text", "") or ""
        self.sent.append((chat_id, text))
        return _FakeSendResult(event_id=f"$async{len(self.sent)}:srv")

    async def download_media(self, mxc: str) -> bytes:  # noqa: ARG002
        return b"AUDIO_BYTES_ASYNC"


def test_on_session_start_handles_async_send_adapter(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    adapter = _FakeAsyncAdapter()
    gateway = _build_gateway(adapter)
    _fire_wiring(pre_dispatch, gateway)

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
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    adapter = SimpleNamespace(
        config=SimpleNamespace(user_id="@from_config:srv"),
        send=lambda *a, **k: _FakeSendResult(event_id="$x:srv"),
    )
    _fire_wiring(pre_dispatch, _build_gateway(adapter))
    assert recorder.bot_mxid == "@from_config:srv"


# ---------------------------------------------------------------------------
# Sync↔async bridges (Codex review fixes)
# ---------------------------------------------------------------------------


class _SyncReturningCoroAdapter:
    """The trap shape: `send` looks sync (not declared async def) but
    returns an awaitable. ``inspect.iscoroutinefunction`` returns False
    so the old sync wrapper recorded the coroutine object, not the
    awaited result. Codex caught this — the wrapper now detects via
    ``isawaitable`` and bridges via the background loop."""

    def __init__(self) -> None:
        self.user_id = "@ralph:srv"

    def send(self, chat_id: str, content: str | None = None, **_):  # type: ignore[no-untyped-def]
        async def _real_send():
            await asyncio.sleep(0.01)
            return _FakeSendResult(event_id="$bridged:srv")

        return _real_send()


def test_wrap_send_handles_sync_function_returning_coroutine(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    adapter = _SyncReturningCoroAdapter()
    _fire_wiring(pre_dispatch, _build_gateway(adapter))

    # Caller is sync; result must be awaited via the background loop
    # before we record. Without the fix, the recorded event_id would
    # have been a synthetic timestamp fallback.
    result = adapter.send("!room:srv", "hi via sync-returning-coro")
    assert isinstance(result, _FakeSendResult)
    assert result.event_id == "$bridged:srv"

    content = next(tmp_path.rglob("*.md")).read_text()
    assert "<!-- event:$bridged:srv -->" in content
    assert "hi via sync-returning-coro" in content


class _AsyncDownloadAdapter:
    def __init__(self) -> None:
        self.user_id = "@ralph:srv"
        self.send_calls: list = []

    def send(self, chat_id, content=None, **kwargs):
        text = content or kwargs.get("text", "") or ""
        self.send_calls.append((chat_id, text))
        return _FakeSendResult(event_id="$x")

    async def download_media(self, mxc: str) -> bytes:  # noqa: ARG002
        await asyncio.sleep(0.01)
        return b"AUDIO_FROM_ASYNC"


# ---------------------------------------------------------------------------
# Name resolver wiring at on_session_start
# ---------------------------------------------------------------------------


class _FakeNameClient:
    """Mautrix-shaped client surface used by _wire_name_resolver.

    Mixes sync and async methods to exercise both code paths in the
    background-loop bridge.
    """

    def __init__(self) -> None:
        self.room_names = {"!room1:srv": "Matt & Annika"}
        self.displaynames = {"@matt:srv": "Matt Hall", "@ralph:srv": "Ralph"}
        # Members per room: room_id -> {mxid: {"displayname": str}}
        self.members = {
            "!dmroom:srv": {
                "@ralph:srv": {"displayname": "Ralph"},
                "@matt:srv": {"displayname": "Matt Hall"},
            }
        }

    async def get_state_event(self, room_id: str, event_type: str, state_key: str = ""):
        if event_type == "m.room.name":
            name = self.room_names.get(room_id)
            return {"name": name} if name else None
        return None

    async def get_displayname(self, mxid: str):
        return self.displaynames.get(mxid)

    async def get_joined_members(self, room_id: str):
        return self.members.get(room_id, {})


def test_name_resolver_wires_pretty_room_and_user_names(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    assert recorder is not None
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    client = _FakeNameClient()
    adapter = SimpleNamespace(
        user_id="@ralph:srv",
        client=client,
        send=lambda *a, **k: _FakeSendResult(event_id="$x:srv"),
    )
    _fire_wiring(pre_dispatch, _build_gateway(adapter))

    assert recorder.resolver.room_slug("!room1:srv") == "Matt-and-Annika"
    assert recorder.resolver.user_display("@matt:srv") == "Matt Hall"


def test_name_resolver_falls_back_to_dm_peer_when_room_name_missing(
    tmp_path: Path,
) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    client = _FakeNameClient()
    adapter = SimpleNamespace(
        user_id="@ralph:srv",
        client=client,
        send=lambda *a, **k: _FakeSendResult(event_id="$x:srv"),
    )
    _fire_wiring(pre_dispatch, _build_gateway(adapter))

    # !dmroom:srv has no m.room.name; resolver should pick the peer's
    # display name (skipping the bot itself).
    assert recorder.resolver.room_slug("!dmroom:srv") == "Matt-Hall"


def test_finds_matrix_adapter_when_adapters_dict_keyed_by_enum(tmp_path: Path) -> None:
    """Hermes's GatewayRunner.adapters is Dict[Platform, BasePlatformAdapter]
    — keyed by enum, not string. Our finder must locate the adapter via
    ``.platform.value == 'matrix'`` rather than dict-string lookup."""
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    # Simulate a Platform enum member.
    class _PlatformEnum:
        def __init__(self, value: str) -> None:
            self.value = value

    matrix_platform = _PlatformEnum("matrix")
    adapter = _FakeSyncAdapter()
    gateway = SimpleNamespace(adapters={matrix_platform: adapter})

    _fire_wiring(pre_dispatch, gateway)

    # If the lookup found the adapter, send was wrapped — verify by
    # firing send and checking the vault gets the outbound section.
    adapter.send("!room:srv", "ack")
    content = next(tmp_path.rglob("*.md")).read_text()
    assert "ack" in content
    assert "stage:sent" in content


def test_name_resolver_safe_when_adapter_has_no_client(tmp_path: Path) -> None:
    """No client → resolver keeps its default fallbacks (slug-from-id,
    MXID localpart). No exception."""
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    adapter = SimpleNamespace(
        user_id="@ralph:srv",
        send=lambda *a, **k: _FakeSendResult(event_id="$x:srv"),
    )
    _fire_wiring(pre_dispatch, _build_gateway(adapter))

    # Falls back to slug-from-room-id.
    assert recorder.resolver.room_slug("!abc:srv") == "abc"
    # Falls back to MXID localpart.
    assert recorder.resolver.user_display("@matt:srv") == "matt"


def test_async_download_callable_works_from_inside_running_loop(tmp_path: Path) -> None:
    """The killer scenario from Codex's #1 concern.

    ``_resolve_download_callable`` wraps the adapter's async
    download_media for sync callers. Old impl used ``asyncio.run`` which
    raises ``RuntimeError: asyncio.run() cannot be called from a
    running event loop`` when called from a thread that already has
    a loop running — exactly Hermes's hot path. The fix routes through
    a dedicated background-loop singleton.
    """
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    assert recorder is not None
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    adapter = _AsyncDownloadAdapter()
    _fire_wiring(pre_dispatch, _build_gateway(adapter))
    assert recorder._download_media is not None  # noqa: SLF001

    async def _drive() -> bytes:
        # Inside a running event loop on this thread. The bridge MUST
        # still return bytes synchronously.
        return recorder._download_media("mxc://x/y")  # noqa: SLF001

    result = asyncio.run(_drive())
    assert result == b"AUDIO_FROM_ASYNC"
