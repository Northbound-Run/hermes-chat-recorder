"""Dedicated background asyncio loop for bridging sync→async work.

Hermes's plugin hooks (notably ``pre_gateway_dispatch``) run
synchronously on the gateway's main event loop thread. Calling
``asyncio.run()`` from there raises ``RuntimeError: asyncio.run()
cannot be called from a running event loop``. ``asyncio.run_coroutine
_threadsafe`` works ONLY when invoked from a thread that's NOT the
loop's own thread.

So we own a tiny private loop on a daemon thread and route async
work to it via ``run_coro_sync``. Callers stay synchronous; the loop
stays out of Hermes's way.

Singleton because there's no good reason to have more than one of
these per process — and a single shared loop means coroutines that
need to talk to each other can do so naturally.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_instance: "BackgroundLoop | None" = None
_singleton_lock = threading.Lock()


class BackgroundLoop:
    """A dedicated asyncio event loop running on a daemon thread."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="hcr-background-loop", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        except Exception:  # pragma: no cover - daemon thread, log + crash
            logger.exception("hermes_chat_recorder background loop crashed")
        finally:
            try:
                self._loop.close()
            except Exception:  # pragma: no cover
                pass

    def run_coro_sync(self, coro: Awaitable[T], *, timeout: float = 120.0) -> T:
        """Run an awaitable on the background loop, block for the result.

        Raises ``RuntimeError`` if the loop isn't running. Re-raises any
        exception the coroutine raises (after ``Future.result`` unwraps it).
        """
        if not self._loop.is_running():
            raise RuntimeError("background loop is not running")
        future = asyncio.run_coroutine_threadsafe(_ensure_coro(coro), self._loop)
        return future.result(timeout=timeout)

    def is_running(self) -> bool:
        return self._loop.is_running()


async def _ensure_coro(awaitable: Awaitable[T]) -> T:
    """Wrap any awaitable as a coroutine so ``run_coroutine_threadsafe``
    can take it — that function requires an actual coroutine, not just
    any awaitable (futures get rejected)."""
    return await awaitable


def get_background_loop() -> BackgroundLoop:
    """Lazily construct the process-wide singleton."""
    global _instance
    with _singleton_lock:
        if _instance is None:
            _instance = BackgroundLoop()
        return _instance


def _reset_for_tests() -> None:
    """Test-only escape hatch — discard the singleton so each test
    that needs a fresh loop can have one."""
    global _instance
    with _singleton_lock:
        _instance = None
