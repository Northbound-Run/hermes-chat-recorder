"""Tests for the sync↔async bridge.

The bridge MUST work when called from a thread that already has a
running event loop (which is the failure mode that motivated this
module). The tests run the bridge call from the main thread first
(easy case), then from inside a running asyncio.run() to simulate the
Hermes gateway hot path.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from hermes_chat_recorder._background_loop import (
    _reset_for_tests,
    get_background_loop,
)


@pytest.fixture(autouse=True)
def _isolate_singleton():
    """Fresh background loop for each test so a misbehaving loop in
    one test doesn't poison another."""
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_get_background_loop_returns_singleton() -> None:
    a = get_background_loop()
    b = get_background_loop()
    assert a is b
    assert a.is_running()


def test_run_coro_sync_resolves_awaitable() -> None:
    async def _slow() -> int:
        await asyncio.sleep(0.01)
        return 42

    loop = get_background_loop()
    assert loop.run_coro_sync(_slow()) == 42


def test_run_coro_sync_propagates_exception() -> None:
    async def _boom() -> None:
        raise RuntimeError("synthetic")

    loop = get_background_loop()
    with pytest.raises(RuntimeError, match="synthetic"):
        loop.run_coro_sync(_boom())


def test_run_coro_sync_from_inside_a_running_loop() -> None:
    """The killer scenario: called from inside an active asyncio loop.

    Hermes's `pre_gateway_dispatch` callback runs synchronously on the
    gateway's loop thread. ``asyncio.run`` would raise here. The
    background-loop singleton MUST work because it owns its own loop
    on a separate thread.
    """

    async def _async_caller() -> int:
        # We're inside a running event loop right now. Calling
        # get_background_loop().run_coro_sync MUST work despite that.
        loop = get_background_loop()

        async def _work() -> int:
            await asyncio.sleep(0.01)
            return 7

        return loop.run_coro_sync(_work())

    # Top-level asyncio.run gives us a running loop on the main thread.
    # The bridge call from inside _async_caller is the trap.
    assert asyncio.run(_async_caller()) == 7


def test_run_coro_sync_concurrent_calls_serialize_cleanly() -> None:
    """Twenty threads firing background work simultaneously each get
    their own result — no shared-state interference."""
    loop = get_background_loop()

    results: list[int] = [-1] * 20

    def worker(i: int) -> None:
        async def _go() -> int:
            await asyncio.sleep(0.01)
            return i * 2

        results[i] = loop.run_coro_sync(_go())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [i * 2 for i in range(20)]
