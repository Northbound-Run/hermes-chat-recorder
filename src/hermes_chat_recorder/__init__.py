"""hermes-chat-recorder — Hermes plugin that records Matrix chat to a vault.

See README.md and docs/DESIGN.md for the full architecture. Public surface
is the :func:`register` function, which Hermes's plugin loader invokes
when the plugin is discovered (either as a bundled `plugin.yaml` install
or via the ``hermes_agent.plugins`` entry-point group).
"""

from __future__ import annotations

__version__ = "0.0.1"

__all__ = ["register"]


def register(ctx) -> None:  # pragma: no cover - thin shim, exercised by integration tests
    """Plugin entry point called by Hermes's plugin loader.

    Wires the :func:`hermes_chat_recorder.recorder.on_pre_gateway_dispatch`
    callback into the ``pre_gateway_dispatch`` hook, and arranges the
    outbound-message wrapper via ``on_session_start``. The actual logic
    lives in submodules so this entry-point stays small and easy to audit.

    Parameters
    ----------
    ctx:
        Hermes plugin context. Provides ``register_hook(name, callback)``
        among other surfaces. See ``hermes_cli/plugins.py`` upstream for
        the full ``PluginContext`` interface.
    """
    # Late imports keep the package import cheap when the plugin is
    # discovered but Hermes hasn't yet wired everything up.
    from hermes_chat_recorder.plugin import register as _register

    _register(ctx)
