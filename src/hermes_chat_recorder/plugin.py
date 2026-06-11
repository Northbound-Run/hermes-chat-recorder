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
from hermes_chat_recorder.name_resolver import NameResolver, mxid_localpart
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

    writer = VaultWriter(
        vault_root=config.vault_root,
        timezone=config.timezone,
        flat_layout=(config.bot_type == "1on1"),
    )

    # Recorder holds the wiring callback and fires it lazily on the
    # first ``pre_gateway_dispatch`` (Hermes's ``on_session_start``
    # hook only ships ``session_id``, never the gateway object — so we
    # can't wire there).
    def _wire(gateway: Any) -> None:
        _wire_adapters(recorder, gateway=gateway)

    recorder = Recorder(
        config=config,
        writer=writer,
        resolver=NameResolver(
            room_overrides=config.room_overrides,
            user_overrides=config.user_overrides,
        ),
        wire_gateway_once=_wire,
    )

    if not hasattr(ctx, "register_hook"):
        logger.error(
            "hermes_chat_recorder: ctx has no register_hook(); the plugin loader changed "
            "shape upstream. Plugin loaded but no hooks bound."
        )
        return recorder

    _assert_hooks_available()

    ctx.register_hook("pre_gateway_dispatch", recorder.on_pre_gateway_dispatch)

    logger.info(
        "hermes_chat_recorder: registered (vault_root=%s, bot_type=%s, platforms=%s, "
        "room_overrides=%d, user_overrides=%d) — STT and vision delegated to Hermes",
        config.vault_root,
        config.bot_type,
        sorted(config.platforms) if config.platforms else "all",
        len(config.room_overrides),
        len(config.user_overrides),
    )
    return recorder


def _assert_hooks_available() -> None:
    """Best-effort guard against upstream Hermes renaming our hooks.

    If Hermes ever renames ``pre_gateway_dispatch``, our plugin would
    silently stop intercepting messages (the loader would log
    "unknown hook" and skip). Checking ``VALID_HOOKS`` at register time
    fails LOUDLY instead.

    In test environments where ``hermes_cli.plugins`` isn't importable
    we skip the check rather than artificially fail.
    """
    try:
        from hermes_cli.plugins import VALID_HOOKS  # type: ignore[import-not-found]
    except Exception:
        logger.debug(
            "hermes_chat_recorder: VALID_HOOKS unavailable (probably running outside Hermes); "
            "skipping hook-name assertion."
        )
        return

    required = {"pre_gateway_dispatch"}
    missing = required - set(VALID_HOOKS)
    if missing:
        raise RuntimeError(
            "hermes_chat_recorder: required Hermes hooks missing from VALID_HOOKS: "
            f"{sorted(missing)}. Has Hermes renamed them upstream?"
        )


def _read_plugin_block(ctx: Any) -> dict | None:
    """Extract the ``plugins.chat_recorder`` block from Hermes's config.

    PluginContext doesn't expose the loaded config directly. Hermes's
    convention (see ``plugins/memory/holographic/__init__.py``) is for
    plugins to import ``hermes_cli.config.load_config`` and read via
    ``cfg_get``. We mirror that. The ``ctx.config`` lookup path is kept
    as a fallback for any future test ctx that provides config that way.
    """
    # Hermes's canonical path.
    try:
        from hermes_cli.config import cfg_get  # type: ignore[import-not-found]
        from hermes_cli.config import (
            load_config as _load_hermes_config,  # type: ignore[import-not-found]
        )

        all_config = _load_hermes_config()
        block = cfg_get(all_config, "plugins", "chat_recorder", default=None)
        if isinstance(block, dict):
            return block
    except Exception as exc:
        logger.debug(
            "hermes_chat_recorder: hermes_cli.config unavailable (%s); "
            "falling back to ctx.config lookup.",
            exc,
        )

    # Fallback path — useful for tests that build a fake ctx with .config.
    cfg = getattr(ctx, "config", None)
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


def _wire_adapters(recorder: Recorder, **kwargs: Any) -> None:
    """Wire every live platform adapter up to the recorder.

    Called lazily from the recorder on the first
    ``pre_gateway_dispatch`` invocation (which is the earliest hook that
    actually receives ``gateway=self`` from Hermes). For EVERY adapter
    we wrap ``send`` so outbound replies land in the vault. The Matrix
    adapter additionally gets:

    1. The bot's MXID read from its config (self-identification).
    2. A media-download fallback for legacy events without a cached
       local file.
    3. Name-resolver lookups bound to the live Matrix client (room
       names, profile display names, DM peers).
    """
    gateway = kwargs.get("gateway") or kwargs.get("gateway_runner")
    if gateway is None:
        logger.warning(
            "hermes_chat_recorder: wiring callback fired without a gateway; "
            "outbound recording will be unavailable."
        )
        return

    adapters = _iter_adapters(gateway)
    if not adapters:
        logger.warning(
            "hermes_chat_recorder: no live adapters found in gateway.adapters; "
            "outbound recording will be unavailable."
        )
        return

    for platform, adapter in adapters:
        if platform == "matrix":
            _wire_matrix_extras(recorder, adapter)
        _wrap_send(adapter, recorder, platform)

    logger.info(
        "hermes_chat_recorder: outbound recording wired for %s",
        ", ".join(sorted(p for p, _ in adapters)),
    )


def _iter_adapters(gateway: Any) -> list[tuple[str, Any]]:
    """Normalize ``gateway.adapters`` to ``[(platform_value, adapter)]``.

    Hermes's ``GatewayRunner.adapters`` is a ``Dict[Platform,
    BasePlatformAdapter]`` — keyed by the Platform enum, NOT by string —
    so keys are normalized via their ``.value``. String-keyed dicts
    (test fixtures) and list/tuple shapes (defensive, in case the
    upstream type changes) are accepted too.
    """
    adapters = getattr(gateway, "adapters", None)
    found: list[tuple[str, Any]] = []
    if isinstance(adapters, dict):
        for platform_key, adapter in adapters.items():
            value = getattr(platform_key, "value", None) or str(platform_key)
            name = str(value).lower()
            if name and name != "none":
                found.append((name, adapter))
        return found
    if isinstance(adapters, (list, tuple)):
        for adapter in adapters:
            platform = getattr(adapter, "platform", None) or getattr(adapter, "name", None)
            if platform is None:
                continue
            value = getattr(platform, "value", None) or str(platform)
            name = str(value).lower()
            if name and name != "none":
                found.append((name, adapter))
    return found


def _wire_matrix_extras(recorder: Recorder, adapter: Any) -> None:
    """Matrix-only wiring: bot MXID, media-download fallback, name lookups."""
    bot_mxid = _read_bot_mxid(adapter)
    if bot_mxid:
        recorder.set_bot_mxid(bot_mxid)
        logger.info("hermes_chat_recorder: bot MXID resolved as %s", bot_mxid)

    download = _resolve_download_callable(adapter)
    if download is not None:
        recorder.set_download_media(download)

    _wire_name_resolver(recorder, adapter)


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


def _wire_name_resolver(recorder: Recorder, adapter: Any) -> None:
    """Bind the resolver's Matrix lookups to the live Matrix client.

    Falls back gracefully when the client is missing or its method
    surface doesn't match mautrix conventions — the resolver already
    has per-event hints plus ID-derived fallbacks, so a wiring failure
    just means we keep using those.
    """
    # Hermes's MatrixAdapter stores the mautrix client on the private
    # attribute ``_client`` (see gateway/platforms/matrix.py). Try the
    # public name first for forward-compat in case upstream renames it,
    # then fall back to the underscore-prefixed real attribute.
    client = getattr(adapter, "client", None) or getattr(adapter, "_client", None)
    if client is None:
        logger.info(
            "hermes_chat_recorder: matrix adapter exposes no .client or ._client; "
            "names will fall back to event hints and ID-derived slugs."
        )
        return

    import inspect

    def _run(maybe_awaitable: Any) -> Any:
        if inspect.isawaitable(maybe_awaitable):
            from hermes_chat_recorder._background_loop import get_background_loop

            return get_background_loop().run_coro_sync(maybe_awaitable)
        return maybe_awaitable

    def _room_name(room_id: str) -> str | None:
        # m.room.name first.
        getter = getattr(client, "get_state_event", None)
        if callable(getter):
            try:
                content = _run(getter(room_id, "m.room.name"))
            except Exception:
                content = None
            name = _extract_name_field(content, "name")
            if name:
                return name
            # Canonical alias as a softer fallback.
            try:
                content = _run(getter(room_id, "m.room.canonical_alias"))
            except Exception:
                content = None
            alias = _extract_name_field(content, "alias")
            if alias:
                # "#room:server" → "room"
                trimmed = alias.lstrip("#")
                return trimmed.split(":", 1)[0] if ":" in trimmed else trimmed
        return None

    def _user_name(mxid: str) -> str | None:
        getter = getattr(client, "get_displayname", None)
        if not callable(getter):
            return None
        try:
            result = _run(getter(mxid))
        except Exception:
            return None
        if isinstance(result, str) and result.strip():
            return result.strip()
        if isinstance(result, dict):
            name = result.get("displayname")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return None

    def _dm_peer(room_id: str) -> str | None:
        getter = getattr(client, "get_joined_members", None) or getattr(
            client, "get_room_members", None
        )
        if not callable(getter):
            return None
        try:
            members = _run(getter(room_id))
        except Exception:
            return None
        if not isinstance(members, dict):
            return None
        bot = recorder.bot_mxid
        for member_mxid, info in members.items():
            if member_mxid == bot:
                continue
            display = getattr(info, "displayname", None) or getattr(
                info, "display_name", None
            )
            if display is None and isinstance(info, dict):
                display = info.get("displayname") or info.get("display_name")
            if isinstance(display, str) and display.strip():
                return display.strip()
            # Last-resort: localpart of the peer's MXID so we still get
            # SOMETHING readable for the chat slug.
            local = mxid_localpart(str(member_mxid))
            if local:
                return local
        return None

    recorder.resolver.set_lookups(
        room_name_lookup=_room_name,
        user_name_lookup=_user_name,
        dm_peer_lookup=_dm_peer,
    )
    logger.info("hermes_chat_recorder: name resolver wired to live Matrix client")


def _extract_name_field(content: Any, field: str) -> str | None:
    """Pull ``field`` off a mautrix state-event content (typed obj or dict)."""
    if content is None:
        return None
    val = getattr(content, field, None)
    if val is None and isinstance(content, dict):
        val = content.get(field)
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


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
    # Public name first, then the mautrix-private ``_client`` Hermes
    # actually uses.
    client = getattr(adapter, "client", None) or getattr(adapter, "_client", None)
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


def _extract_send_args(args: tuple, kwargs: dict) -> tuple[str, str]:
    """Pull ``chat_id`` and the message body out of a ``send`` call.

    Hermes's canonical signature is ``send(chat_id, content, reply_to,
    metadata)`` and the gateway invokes it with keyword arguments.
    Older Hermes builds used ``text`` instead of ``content``. We accept
    both kwarg names and fall back to positional args so the wrapper
    works regardless of how the caller binds the parameters.

    Returns ``(chat_id, message_body)``. Either may be empty string
    when the caller passes an unexpected shape; the recorder treats
    empty values as "skip outbound recording" rather than crashing.
    """
    chat_id = ""
    text = ""
    if args:
        if len(args) >= 1 and isinstance(args[0], str):
            chat_id = args[0]
        if len(args) >= 2 and isinstance(args[1], str):
            text = args[1]
    if not chat_id:
        cid = kwargs.get("chat_id")
        if isinstance(cid, str):
            chat_id = cid
    if not text:
        for key in ("content", "text", "body", "message"):
            val = kwargs.get(key)
            if isinstance(val, str) and val:
                text = val
                break
    return chat_id, text


def _wrap_send(adapter: Any, recorder: Recorder, platform: str) -> None:
    """Idempotently replace ``adapter.send`` with a wrapper that
    records outbound replies after a successful send.

    Handles three function shapes:

    * Pure sync — ``def send(...) -> SendResult``
    * Pure async — ``async def send(...) -> SendResult`` (every
      current Hermes adapter)
    * Sync function returning awaitable — ``def send(...) -> Coroutine``
      (``iscoroutinefunction`` returns False for these, but the result
      needs awaiting before we can read the sent message's ID)
    """
    if getattr(adapter, "_chat_recorder_send_wrapped", False):
        return  # already wrapped
    original_send = getattr(adapter, "send", None)
    if not callable(original_send):
        return

    import inspect

    is_coro_fn = inspect.iscoroutinefunction(original_send)

    if is_coro_fn:

        async def wrapped(*args, **kwargs):
            chat_id, text = _extract_send_args(args, kwargs)
            result = await original_send(*args, **kwargs)
            try:
                _record_outbound(recorder, platform, chat_id, text, result)
            except Exception as exc:
                logger.warning("hermes_chat_recorder: outbound recording failed: %s", exc)
            return result
    else:

        def wrapped(*args, **kwargs):  # type: ignore[misc]
            chat_id, text = _extract_send_args(args, kwargs)
            result = original_send(*args, **kwargs)
            # Some adapter decorators present a sync surface but return
            # a coroutine. If we recorded on the coroutine object the
            # message_id would be missing and the recorded "send" might
            # never actually complete. Bridge via the background loop
            # so we both record the real result AND propagate errors.
            if inspect.isawaitable(result):
                from hermes_chat_recorder._background_loop import get_background_loop

                result = get_background_loop().run_coro_sync(result)
            try:
                _record_outbound(recorder, platform, chat_id, text, result)
            except Exception as exc:
                logger.warning("hermes_chat_recorder: outbound recording failed: %s", exc)
            return result

    adapter.send = wrapped  # type: ignore[assignment]
    adapter._chat_recorder_send_wrapped = True


def _record_outbound(
    recorder: Recorder,
    platform: str,
    chat_id: str,
    text: str,
    send_result: Any,
) -> None:
    """Pull the message ID out of the adapter's send result and persist.

    Hermes adapters return a ``SendResult`` with ``success`` and
    ``message_id`` fields; both are read duck-typed so dict-shaped and
    legacy results keep working. Failed sends are not recorded — the
    message never reached the chat.

    The ENTIRE body is exception-guarded: recording must never make a
    successful ``adapter.send`` look like it failed to the gateway,
    even if the result object's attribute access itself raises.
    """
    try:
        _record_outbound_inner(recorder, platform, chat_id, text, send_result)
    except Exception as exc:
        logger.warning("hermes_chat_recorder: outbound recording failed: %s", exc)


def _record_outbound_inner(
    recorder: Recorder,
    platform: str,
    chat_id: str,
    text: str,
    send_result: Any,
) -> None:
    success = getattr(send_result, "success", None)
    if success is None and isinstance(send_result, dict):
        success = send_result.get("success")
    if success is False:
        return

    event_id = ""
    for attr in ("message_id", "event_id", "id"):
        val = getattr(send_result, attr, None)
        if isinstance(val, str) and val:
            event_id = val
            break
    if not event_id and isinstance(send_result, dict):
        event_id = str(
            send_result.get("message_id")
            or send_result.get("event_id")
            or send_result.get("id")
            or ""
        )
    if not event_id:
        # Fallback so the vault has something stable to anchor against.
        event_id = f"out:{datetime.now(timezone.utc).isoformat()}"

    recorder.record_outbound(
        platform=platform,
        chat_id=chat_id,
        text=text,
        event_id=event_id,
        timestamp=datetime.now(timezone.utc),
    )
