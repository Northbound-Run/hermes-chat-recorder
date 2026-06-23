"""Repo-root entry module for the ``hermes plugins install`` git-clone path.

Hermes has two plugin-discovery paths:

* **Pip / entry-point** — ``pip install hermes-chat-recorder`` exposes the
  ``hermes_agent.plugins`` entry point and Hermes imports the real package
  ``hermes_chat_recorder`` from ``src/`` directly. This shim is NOT used.
* **Directory install** — ``hermes plugins install Northbound-Run/hermes-chat-recorder``
  git-clones this repo into ``~/.hermes/plugins/chat_recorder/`` and the
  directory loader execs *this* file, then calls ``register(ctx)`` on it.

This repo uses a ``src/`` layout, so the actual plugin code lives in
``src/hermes_chat_recorder/`` rather than next to this file. The shim puts
that ``src/`` directory on ``sys.path`` and re-exports the package's public
surface, so the directory loader finds a working ``register`` at the clone
root. The pip path is unaffected — it never imports this module.
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_chat_recorder import __version__, register  # noqa: E402

__all__ = ["__version__", "register"]
