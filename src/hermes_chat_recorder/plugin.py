"""Plugin wiring — registers hooks with the Hermes plugin context.

This module is intentionally thin. All real logic lives in :mod:`recorder`,
:mod:`writer`, :mod:`gate`, :mod:`transcriber`, and :mod:`describer`. The
goal here is just to bind callbacks to the right Hermes hook names.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Bind plugin callbacks to Hermes hooks.

    Called once at gateway startup by Hermes's plugin loader. We register:

    * ``pre_gateway_dispatch`` — inbound message interception. Used for
      recording every event to the vault and applying the wake gate. See
      :func:`hermes_chat_recorder.recorder.on_pre_gateway_dispatch`.
    * ``on_session_start`` — fires once when the gateway session boots.
      We use it to wrap the live Matrix adapter's ``send`` method so
      outbound replies also land in the vault.
    """
    # Implementation lands in P2. Stubbed for the scaffolded repo so
    # imports succeed and the plugin loader sees a valid `register`.
    raise NotImplementedError(
        "hermes-chat-recorder plugin scaffold — register() not yet implemented. "
        "See docs/DESIGN.md for the wiring plan."
    )
