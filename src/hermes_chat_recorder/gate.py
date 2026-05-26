"""Wake gate — decides whether a recorded message should also trigger an agent reply.

A message wakes the agent if EITHER:

1. The bot is ``@``-mentioned in the Matrix event, OR
2. The text contains any allowlisted nickname (case-insensitive,
   word-boundary).

Outbound messages from the bot's own MXID always fail the gate
(self-echo guard). See ``docs/DESIGN.md §4`` for the design rationale.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from hermes_chat_recorder.types import GateInput


class Gate:
    """Stateless wake-gate built from a nickname allowlist."""

    def __init__(self, nicknames: Iterable[str]) -> None:
        # Normalize: strip, drop empties, lowercase for the allowlist
        # itself. The compiled regex is case-insensitive so users can
        # still type the canonical capitalized form in their config and
        # it still matches lowercase variants in messages.
        clean: list[str] = []
        seen: set[str] = set()
        for raw in nicknames:
            if not isinstance(raw, str):
                continue
            stripped = raw.strip()
            if not stripped:
                continue
            lowered = stripped.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            clean.append(stripped)

        self._nicknames: tuple[str, ...] = tuple(clean)
        if self._nicknames:
            escaped = "|".join(re.escape(n) for n in self._nicknames)
            self._pattern: re.Pattern[str] | None = re.compile(
                rf"\b(?:{escaped})\b", re.IGNORECASE
            )
        else:
            self._pattern = None

    @property
    def nicknames(self) -> tuple[str, ...]:
        """The cleaned, de-duplicated nickname allowlist."""
        return self._nicknames

    def should_wake(self, inp: GateInput) -> bool:
        """Return True iff the message should trigger an agent reply."""
        if inp.sender_mxid == inp.bot_mxid:
            # Self-echo guard. Never wake on the bot's own messages,
            # even if the body happens to contain a nickname or the
            # bot somehow "mentioned itself".
            return False

        if inp.bot_mxid and inp.bot_mxid in inp.mentioned_mxids:
            return True

        if self._pattern is not None and self._pattern.search(inp.gate_text or ""):
            return True

        return False
