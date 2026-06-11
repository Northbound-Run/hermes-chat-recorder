"""hermes-chat-recorder — Hermes plugin that records gateway chats to a vault.

Records every message the Hermes gateway dispatches — any platform —
into per-platform, per-chat, per-day Markdown files. See README.md and
docs/DESIGN.md for the full architecture. Public surface is the
:func:`register` function, which Hermes's plugin loader invokes when
the plugin is discovered (either as a directory install or via the
``hermes_agent.plugins`` entry-point group).
"""

from __future__ import annotations

__version__ = "0.7.0"

__all__ = ["register"]


def register(ctx):
    """Plugin entry point called by Hermes's plugin loader.

    Delegates to :func:`hermes_chat_recorder.plugin.register`. Returns
    the constructed :class:`Recorder` (or ``None`` when the plugin is
    disabled in config), mirroring the inner function so tests can
    inspect what was wired up.

    Late imports keep the package import cheap when the plugin is
    discovered but Hermes hasn't yet wired everything up.
    """
    from hermes_chat_recorder.plugin import register as _register

    return _register(ctx)
