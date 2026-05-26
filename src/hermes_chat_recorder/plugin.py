"""Plugin wiring — binds hermes-chat-recorder into the Hermes plugin loader.

This module exposes the ``register(ctx)`` entry-point that Hermes calls
once per discovered plugin. We read config off ``ctx``, build the
:class:`Recorder`, and bind its callbacks to the appropriate Hermes
hooks. The actual recording logic lives in :mod:`recorder`; this file
is intentionally thin so it's easy to audit.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from hermes_chat_recorder.config import ConfigError, load_config
from hermes_chat_recorder.gate import Gate
from hermes_chat_recorder.recorder import Recorder
from hermes_chat_recorder.writer import VaultWriter

logger = logging.getLogger(__name__)


def register(ctx: Any) -> Recorder | None:
    """Hermes plugin entry-point.

    Returns the constructed :class:`Recorder` for callers (including
    tests) that want to inspect what was wired up. Returns ``None`` when
    the plugin is disabled in config (caller treats this as "loaded but
    inert" — no hook bindings happen).

    The function tolerates the various shapes Hermes might pass for
    ``ctx``; we read with duck-typing so we don't take a hard dep on
    a specific PluginContext class.
    """
    plugin_block = _read_plugin_block(ctx)
    try:
        config = load_config(plugin_block)
    except ConfigError as exc:
        logger.error("hermes_chat_recorder: invalid config, plugin will NOT load: %s", exc)
        raise

    if not config.enabled:
        logger.info("hermes_chat_recorder: plugin disabled via config, skipping")
        return None

    writer = VaultWriter(vault_root=config.vault_root, timezone=config.timezone)
    gate = Gate(config.nicknames)
    recorder = Recorder(
        config=config,
        writer=writer,
        gate=gate,
        # Transcriber and describer are constructed lazily on first use
        # so plugin load stays cheap even when faster-whisper is in the
        # venv but no audio has been processed yet.
    )

    if not hasattr(ctx, "register_hook"):
        logger.error(
            "hermes_chat_recorder: ctx has no register_hook(); the plugin loader changed "
            "shape upstream. Plugin loaded but no hooks bound."
        )
        return recorder

    ctx.register_hook("pre_gateway_dispatch", recorder.on_pre_gateway_dispatch)
    ctx.register_hook(
        "on_session_start", lambda **kw: _wire_matrix_adapter(recorder, **kw)
    )

    logger.info(
        "hermes_chat_recorder: registered (vault_root=%s, nicknames=%s)",
        config.vault_root,
        list(config.nicknames),
    )
    return recorder


def _read_plugin_block(ctx: Any) -> dict | None:
    """Best-effort extraction of the ``plugins.chat_recorder`` block.

    Tries (in order): ``ctx.config["plugins"]["chat_recorder"]``,
    ``ctx.config.get("plugins", {}).get("chat_recorder")``, then a
    bare ``ctx.config["chat_recorder"]`` fallback.
    """
    cfg = getattr(ctx, "config", None)
    if cfg is None:
        return None
    if isinstance(cfg, dict):
        plugins = cfg.get("plugins")
        if isinstance(plugins, dict):
            block = plugins.get("chat_recorder")
            if isinstance(block, dict):
                return block
        fallback = cfg.get("chat_recorder")
        if isinstance(fallback, dict):
            return fallback
    return None


def _wire_matrix_adapter(recorder: Recorder, **kwargs: Any) -> None:
    """Locate the live Matrix adapter at session-start and wire it up.

    We look up the adapter via ``gateway.adapters`` (a dict keyed by
    platform name). On finding it we:

    1. Capture a callable that downloads bytes for an ``mxc://`` URL.
       Adapter method names differ across mautrix versions — try a few.
    2. Read the bot's MXID from the adapter's config.
    3. Wrap the adapter's ``send`` method so outbound replies land in
       the vault.
    """
    gateway = kwargs.get("gateway") or kwargs.get("gateway_runner")
    if gateway is None:
        logger.warning(
            "hermes_chat_recorder: on_session_start called without a gateway kwarg; "
            "outbound recording and media download will be unavailable."
        )
        return

    adapter = _find_matrix_adapter(gateway)
    if adapter is None:
        logger.warning(
            "hermes_chat_recorder: no live matrix adapter found in gateway.adapters; "
            "outbound recording and media download will be unavailable."
        )
        return

    bot_mxid = _read_bot_mxid(adapter)
    if bot_mxid:
        recorder.set_bot_mxid(bot_mxid)
        logger.info("hermes_chat_recorder: bot MXID resolved as %s", bot_mxid)

    download = _resolve_download_callable(adapter)
    if download is not None:
        recorder.set_download_media(download)

    _wrap_send(adapter, recorder)


def _find_matrix_adapter(gateway: Any) -> Any | None:
    adapters = getattr(gateway, "adapters", None)
    if isinstance(adapters, dict):
        return adapters.get("matrix") or adapters.get("Matrix")
    if isinstance(adapters, (list, tuple)):
        for a in adapters:
            platform = getattr(a, "platform", None) or getattr(a, "name", None)
            value = getattr(platform, "value", None) or str(platform)
            if str(value).lower() == "matrix":
                return a
    return None


def _read_bot_mxid(adapter: Any) -> str:
    for attr in ("user_id", "mxid", "bot_user_id"):
        val = getattr(adapter, attr, None)
        if isinstance(val, str) and val:
            return val
    cfg = getattr(adapter, "config", None)
    if cfg is not None:
        for attr in ("user_id", "mxid", "bot_user_id"):
            val = getattr(cfg, attr, None)
            if isinstance(val, str) and val:
                return val
    return ""


def _resolve_download_callable(adapter: Any):
    """Return a sync callable: ``mxc_url -> bytes``, or None.

    mautrix exposes ``client.download_media(mxc_uri)`` and similar.
    The adapter may proxy this via ``adapter.download_media`` or
    ``adapter.client.download_media``. We try a few patterns.

    All known shapes are async; the recorder runs in a sync hook, so
    we bridge via a dedicated background asyncio loop (see
    :mod:`_background_loop`). ``asyncio.run`` is NOT safe here — it
    raises ``RuntimeError: asyncio.run() cannot be called from a
    running event loop`` when invoked from the gateway's main thread.
    """
    import inspect

    candidates = []
    for attr in ("download_media", "download_mxc"):
        fn = getattr(adapter, attr, None)
        if fn is not None:
            candidates.append(fn)
    client = getattr(adapter, "client", None)
    if client is not None:
        for attr in ("download_media", "download_mxc"):
            fn = getattr(client, attr, None)
            if fn is not None:
                candidates.append(fn)
    if not candidates:
        return None

    download_fn = candidates[0]

    def _sync_download(mxc_url: str) -> bytes:
        result = download_fn(mxc_url)
        if inspect.isawaitable(result):
            from hermes_chat_recorder._background_loop import get_background_loop

            return get_background_loop().run_coro_sync(result)
        return result

    return _sync_download


def _wrap_send(adapter: Any, recorder: Recorder) -> None:
    """Idempotently replace ``adapter.send`` with a wrapper that
    records outbound replies after a successful send.

    Handles three function shapes:

    * Pure sync — ``def send(...) -> SendResult``
    * Pure async — ``async def send(...) -> SendResult``
    * Sync function returning awaitable — ``def send(...) -> Coroutine``
      (this is the trap Codex caught — ``iscoroutinefunction`` returns
      False for these, but the result needs awaiting before we record)
    """
    if getattr(adapter, "_chat_recorder_send_wrapped", False):
        return  # already wrapped
    original_send = getattr(adapter, "send", None)
    if not callable(original_send):
        return

    import inspect

    is_coro_fn = inspect.iscoroutinefunction(original_send)
    bot_mxid = recorder.bot_mxid

    if is_coro_fn:

        async def wrapped(chat_id, text, *args, **kwargs):
            result = await original_send(chat_id, text, *args, **kwargs)
            _record_outbound(recorder, adapter, chat_id, text, result, bot_mxid)
            return result
    else:

        def wrapped(chat_id, text, *args, **kwargs):  # type: ignore[misc]
            result = original_send(chat_id, text, *args, **kwargs)
            # Some adapter decorators present a sync surface but return
            # a coroutine. If we record on the coroutine object the
            # event_id is missing and the recorded "send" might never
            # actually complete. Bridge via the background loop so we
            # both record the real result AND propagate any errors.
            if inspect.isawaitable(result):
                from hermes_chat_recorder._background_loop import get_background_loop

                result = get_background_loop().run_coro_sync(result)
            _record_outbound(recorder, adapter, chat_id, text, result, bot_mxid)
            return result

    adapter.send = wrapped  # type: ignore[assignment]
    adapter._chat_recorder_send_wrapped = True


def _record_outbound(
    recorder: Recorder,
    adapter: Any,
    chat_id: str,
    text: str,
    send_result: Any,
    bot_mxid: str,
) -> None:
    """Pull the event_id out of the adapter's send result and persist."""
    event_id = ""
    for attr in ("event_id", "message_id", "id"):
        val = getattr(send_result, attr, None)
        if isinstance(val, str) and val:
            event_id = val
            break
    if not event_id:
        # Some adapters return dicts.
        if isinstance(send_result, dict):
            event_id = (
                str(send_result.get("event_id") or send_result.get("id") or "")
            )
    if not event_id:
        # Fallback so the vault has something stable to anchor against.
        event_id = f"out:{datetime.now(timezone.utc).isoformat()}"

    sender_display = bot_mxid or "bot"
    try:
        recorder.record_outbound(
            room_id=chat_id,
            sender_display=sender_display,
            text=text,
            event_id=event_id,
            timestamp=datetime.now(timezone.utc),
        )
    except Exception as exc:  # noqa: BLE001 - never let vault failure break send
        logger.warning("hermes_chat_recorder: record_outbound failed: %s", exc)
