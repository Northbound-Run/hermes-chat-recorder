"""Tests for plugin.register() + lazy gateway wiring.

Covers the duck-typed ctx surface, hook binding, disabled-config no-op,
config validation surface, and the adapter-wiring path that wraps every
platform adapter's ``send`` for outbound recording — with Matrix
additionally contributing bot identity, a media-download fallback, and
name-resolver lookups.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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
    """Source-less event the recorder ignores entirely.

    Used by wiring tests to invoke ``pre_gateway_dispatch`` purely for
    its side effect of firing the lazy gateway-wire callback. With no
    ``source`` the extractor returns ``None``, so no vault writes
    happen and the event flows on.
    """
    return SimpleNamespace(
        text="",
        message_id="$wiring:srv",
        message_type=SimpleNamespace(name="TEXT"),
        source=None,
        raw_message=SimpleNamespace(),
    )


def _fire_wiring(pre_dispatch, gateway: Any) -> None:
    """Invoke pre_gateway_dispatch with a neutral event so the recorder
    fires its lazy gateway-wire callback."""
    pre_dispatch(event=_neutral_event(), gateway=gateway, session_store=None)


def _take(hooks: list, name: str):
    return next(cb for hname, cb in hooks if hname == name)


# ---------------------------------------------------------------------------
# Basic register() behaviour
# ---------------------------------------------------------------------------


def test_register_returns_recorder_and_binds_hook(tmp_path: Path) -> None:
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
    assert pre_dispatch(event=_neutral_event(), gateway=None, session_store=None) is None


def test_bound_hook_tolerates_future_kwargs(tmp_path: Path) -> None:
    """Hermes may add hook kwargs in any release; the callback must not
    TypeError on names it doesn't know."""
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")
    result = pre_dispatch(
        event=_neutral_event(), gateway=None, session_store=None, brand_new_kwarg=object()
    )
    assert result is None


# ---------------------------------------------------------------------------
# Adapter wiring — sync adapter (simpler)
# ---------------------------------------------------------------------------


@dataclass
class _FakeSendResult:
    """Mirrors Hermes's SendResult shape (success + message_id)."""

    message_id: str
    success: bool = True


@dataclass
class _FakeSyncAdapter:
    user_id: str = "@recorder_bot:srv"
    sent: list = field(default_factory=list)
    send_success: bool = True

    def send(self, chat_id: str, content: str | None = None, **kwargs):
        # Mirror Hermes's canonical signature — keyword `content` is the
        # message body. Older call sites pass positional `(chat_id,
        # text)`, which lands here as `(chat_id, content)`.
        text = content or kwargs.get("text", "") or ""
        self.sent.append((chat_id, text))
        return _FakeSendResult(
            message_id=f"$outbound{len(self.sent)}:srv", success=self.send_success
        )

    def download_media(self, mxc: str) -> bytes:
        return b"AUDIO_BYTES"


def _matrix_gateway(adapter: Any) -> Any:
    return SimpleNamespace(adapters={"matrix": adapter})


def test_wiring_binds_bot_mxid_and_wraps_send(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    assert recorder is not None

    pre_dispatch = _take(hooks, "pre_gateway_dispatch")
    adapter = _FakeSyncAdapter(user_id="@recorder_bot:srv")
    gateway = _matrix_gateway(adapter)

    _fire_wiring(pre_dispatch, gateway)

    # bot_mxid carried over from adapter.user_id
    assert recorder.bot_mxid == "@recorder_bot:srv"

    # send is wrapped — calling it persists an outbound section.
    adapter.send("!room:srv", "ack")
    day_file = next(tmp_path.rglob("*.md"))
    content = day_file.read_text()
    assert "<!-- event:$outbound1:srv -->" in content
    assert "stage:sent" in content
    assert "ack" in content


def test_wrap_send_handles_hermes_kwarg_call_shape(tmp_path: Path) -> None:
    """Regression: Hermes invokes ``send(chat_id=..., content=...,
    reply_to=..., metadata=...)`` — keyword args, with ``content``, not
    ``text``. An earlier wrapper hardcoded ``text`` as a required
    positional, so this call shape crashed and the bot never delivered
    replies."""
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")
    adapter = _FakeSyncAdapter()
    _fire_wiring(pre_dispatch, _matrix_gateway(adapter))

    # Exact call shape Hermes uses — keyword args, with `content`, not `text`.
    result = adapter.send(
        chat_id="!room:srv",
        content="hello from hermes",
        reply_to=None,
        metadata={"thread_id": "$t:srv"},
    )
    assert result.message_id  # didn't crash

    md = next(tmp_path.rglob("*.md")).read_text()
    assert "hello from hermes" in md
    assert "stage:sent" in md


def test_wiring_is_idempotent(tmp_path: Path) -> None:
    """Firing the wiring twice (gateway restart) must NOT double-wrap
    and double-record."""
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    assert recorder is not None
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    adapter = _FakeSyncAdapter()
    gateway = _matrix_gateway(adapter)
    _fire_wiring(pre_dispatch, gateway)
    _fire_wiring(pre_dispatch, gateway)

    adapter.send("!room:srv", "hi")
    content = next(tmp_path.rglob("*.md")).read_text()
    # Exactly ONE outbound section, not two.
    assert content.count("<!-- event:") == 1


def test_wiring_with_missing_gateway_kwarg_is_safe(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")
    # No exception, no crash — wiring callback simply doesn't fire
    # when no gateway is supplied.
    _fire_wiring(pre_dispatch, None)
    pre_dispatch(event=_neutral_event(), session_store=None)


def test_failed_send_is_not_recorded(tmp_path: Path) -> None:
    """SendResult.success=False means the message never reached the
    chat — recording it would archive a phantom reply."""
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")
    adapter = _FakeSyncAdapter(send_success=False)
    _fire_wiring(pre_dispatch, _matrix_gateway(adapter))

    adapter.send("!room:srv", "this never arrived")
    assert list(tmp_path.rglob("*.md")) == []


def test_wiring_resolves_download_callable(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    assert recorder is not None
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    adapter = _FakeSyncAdapter()
    _fire_wiring(pre_dispatch, _matrix_gateway(adapter))

    # The download callable now points at the adapter's download_media.
    assert recorder._download_media is not None
    assert recorder._download_media("mxc://x") == b"AUDIO_BYTES"


# ---------------------------------------------------------------------------
# Multi-platform adapter wiring
# ---------------------------------------------------------------------------


def test_all_adapters_get_send_wrapped(tmp_path: Path) -> None:
    """Every platform adapter — not just Matrix — must have its send
    wrapped so outbound replies land in the vault under the right
    platform folder."""
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    matrix_adapter = _FakeSyncAdapter()
    telegram_adapter = _FakeSyncAdapter(user_id="")
    gateway = SimpleNamespace(
        adapters={"matrix": matrix_adapter, "telegram": telegram_adapter}
    )
    _fire_wiring(pre_dispatch, gateway)

    matrix_adapter.send("!room:srv", "matrix reply")
    telegram_adapter.send("-1001234", "telegram reply")

    matrix_files = list((tmp_path / "matrix").rglob("*.md"))
    telegram_files = list((tmp_path / "telegram").rglob("*.md"))
    assert len(matrix_files) == 1
    assert len(telegram_files) == 1
    assert "matrix reply" in matrix_files[0].read_text()
    assert "telegram reply" in telegram_files[0].read_text()


def test_gateway_with_no_matrix_adapter_still_wraps_others(tmp_path: Path) -> None:
    """A gateway running only Telegram (no Matrix at all) still gets
    outbound recording; the Matrix-only extras are simply skipped."""
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    telegram_adapter = _FakeSyncAdapter(user_id="")
    gateway = SimpleNamespace(adapters={"telegram": telegram_adapter})
    _fire_wiring(pre_dispatch, gateway)

    assert recorder.bot_mxid == ""  # no Matrix → no MXID
    telegram_adapter.send("-5", "hi")
    assert len(list((tmp_path / "telegram").rglob("*.md"))) == 1


def test_adapter_without_send_is_skipped_safely(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    gateway = SimpleNamespace(adapters={"webhook": object()})
    _fire_wiring(pre_dispatch, gateway)  # no exception


def test_finds_adapters_when_dict_keyed_by_enum(tmp_path: Path) -> None:
    """Hermes's GatewayRunner.adapters is Dict[Platform, BasePlatformAdapter]
    — keyed by enum, not string. The wiring must normalize keys via
    ``.value`` rather than relying on dict-string lookup."""
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    # Simulate Platform enum members.
    class _PlatformEnum:
        def __init__(self, value: str) -> None:
            self.value = value

    adapter = _FakeSyncAdapter()
    gateway = SimpleNamespace(adapters={_PlatformEnum("matrix"): adapter})

    _fire_wiring(pre_dispatch, gateway)

    # If the lookup found the adapter, send was wrapped — verify by
    # firing send and checking the vault gets the outbound section.
    adapter.send("!room:srv", "ack")
    content = next(tmp_path.rglob("*.md")).read_text()
    assert "ack" in content
    assert "stage:sent" in content


# ---------------------------------------------------------------------------
# Adapter wiring — async adapter shape
# ---------------------------------------------------------------------------


class _FakeAsyncAdapter:
    def __init__(self):
        self.user_id = "@recorder_bot:srv"
        self.sent: list = []

    async def send(self, chat_id: str, content: str | None = None, **kwargs):
        text = content or kwargs.get("text", "") or ""
        self.sent.append((chat_id, text))
        return _FakeSendResult(message_id=f"$async{len(self.sent)}:srv")

    async def download_media(self, mxc: str) -> bytes:
        return b"AUDIO_BYTES_ASYNC"


def test_wiring_handles_async_send_adapter(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    adapter = _FakeAsyncAdapter()
    _fire_wiring(pre_dispatch, _matrix_gateway(adapter))

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
        send=lambda *a, **k: _FakeSendResult(message_id="$x:srv"),
    )
    _fire_wiring(pre_dispatch, _matrix_gateway(adapter))
    assert recorder.bot_mxid == "@from_config:srv"


# ---------------------------------------------------------------------------
# Sync↔async bridges
# ---------------------------------------------------------------------------


class _SyncReturningCoroAdapter:
    """The trap shape: ``send`` looks sync (not declared ``async def``)
    but returns an awaitable. ``inspect.iscoroutinefunction`` returns
    False, so a naive sync wrapper would record the coroutine object
    instead of the awaited result — and the send might never complete.
    The wrapper must detect via ``isawaitable`` and bridge through the
    background loop."""

    def __init__(self) -> None:
        self.user_id = "@recorder_bot:srv"

    def send(self, chat_id: str, content: str | None = None, **_):  # type: ignore[no-untyped-def]
        async def _real_send():
            await asyncio.sleep(0.01)
            return _FakeSendResult(message_id="$bridged:srv")

        return _real_send()


def test_wrap_send_handles_sync_function_returning_coroutine(tmp_path: Path) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    adapter = _SyncReturningCoroAdapter()
    _fire_wiring(pre_dispatch, _matrix_gateway(adapter))

    # Caller is sync; result must be awaited via the background loop
    # before we record. Without the bridge, the recorded event_id would
    # have been a synthetic timestamp fallback.
    result = adapter.send("!room:srv", "hi via sync-returning-coro")
    assert isinstance(result, _FakeSendResult)
    assert result.message_id == "$bridged:srv"

    content = next(tmp_path.rglob("*.md")).read_text()
    assert "<!-- event:$bridged:srv -->" in content
    assert "hi via sync-returning-coro" in content


class _AsyncDownloadAdapter:
    def __init__(self) -> None:
        self.user_id = "@recorder_bot:srv"
        self.send_calls: list = []

    def send(self, chat_id, content=None, **kwargs):
        text = content or kwargs.get("text", "") or ""
        self.send_calls.append((chat_id, text))
        return _FakeSendResult(message_id="$x")

    async def download_media(self, mxc: str) -> bytes:
        await asyncio.sleep(0.01)
        return b"AUDIO_FROM_ASYNC"


# ---------------------------------------------------------------------------
# Name resolver wiring
# ---------------------------------------------------------------------------


class _FakeNameClient:
    """Mautrix-shaped client surface used by _wire_name_resolver.

    Async methods exercise the background-loop bridge.
    """

    def __init__(self) -> None:
        self.room_names = {"!room1:srv": "Matt & Annika"}
        self.displaynames = {"@matt:srv": "Matt Hall", "@recorder_bot:srv": "Recorder"}
        # Members per room: room_id -> {mxid: {"displayname": str}}
        self.members = {
            "!dmroom:srv": {
                "@recorder_bot:srv": {"displayname": "Recorder"},
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
        user_id="@recorder_bot:srv",
        client=client,
        send=lambda *a, **k: _FakeSendResult(message_id="$x:srv"),
    )
    _fire_wiring(pre_dispatch, _matrix_gateway(adapter))

    assert recorder.resolver.chat_slug("matrix", "!room1:srv") == "Matt-and-Annika"
    assert recorder.resolver.user_display("matrix", "@matt:srv") == "Matt Hall"


def test_name_resolver_falls_back_to_dm_peer_when_room_name_missing(
    tmp_path: Path,
) -> None:
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    client = _FakeNameClient()
    adapter = SimpleNamespace(
        user_id="@recorder_bot:srv",
        client=client,
        send=lambda *a, **k: _FakeSendResult(message_id="$x:srv"),
    )
    _fire_wiring(pre_dispatch, _matrix_gateway(adapter))

    # !dmroom:srv has no m.room.name; resolver should pick the peer's
    # display name (skipping the bot itself).
    assert recorder.resolver.chat_slug("matrix", "!dmroom:srv") == "Matt-Hall"


def test_name_resolver_safe_when_adapter_has_no_client(tmp_path: Path) -> None:
    """No client → resolver keeps its default fallbacks (slug-from-id,
    MXID localpart). No exception."""
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    adapter = SimpleNamespace(
        user_id="@recorder_bot:srv",
        send=lambda *a, **k: _FakeSendResult(message_id="$x:srv"),
    )
    _fire_wiring(pre_dispatch, _matrix_gateway(adapter))

    # Falls back to slug-from-chat-id.
    assert recorder.resolver.chat_slug("matrix", "!abc:srv") == "abc"
    # Falls back to MXID localpart.
    assert recorder.resolver.user_display("matrix", "@matt:srv") == "matt"


def test_async_download_callable_works_from_inside_running_loop(tmp_path: Path) -> None:
    """``_resolve_download_callable`` wraps the adapter's async
    download_media for sync callers. A naive ``asyncio.run`` bridge
    raises ``RuntimeError: asyncio.run() cannot be called from a
    running event loop`` when called from a thread that already has a
    loop running — exactly Hermes's hot path. The bridge must route
    through the dedicated background-loop singleton instead."""
    hooks: list = []
    ctx = _build_ctx({"vault_root": str(tmp_path)}, hooks)
    recorder = register(ctx)
    assert recorder is not None
    pre_dispatch = _take(hooks, "pre_gateway_dispatch")

    adapter = _AsyncDownloadAdapter()
    _fire_wiring(pre_dispatch, _matrix_gateway(adapter))
    assert recorder._download_media is not None

    async def _drive() -> bytes:
        # Inside a running event loop on this thread. The bridge MUST
        # still return bytes synchronously.
        return recorder._download_media("mxc://x/y")

    result = asyncio.run(_drive())
    assert result == b"AUDIO_FROM_ASYNC"
