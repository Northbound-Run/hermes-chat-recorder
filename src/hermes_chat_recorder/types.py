"""Shared dataclasses for the recorder pipeline.

Everything here is plain data — no I/O, no Hermes imports. Modules in
the package pass these around; tests construct them by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal

# Stage names match docs/DESIGN.md §3.
Stage = Literal[
    "received",
    "transcribed",
    "described",
    "transcribe_failed",
    "describe_failed",
    "sent",
]

# Logical kind of a Matrix message — picked so the gate and writer don't
# need to know mautrix's wire-level constants.
MessageKind = Literal["text", "voice", "image", "reply"]


# Stages whose sections should NOT be replaced once written, except by
# the SAME stage (in which case the write is a no-op).
TERMINAL_STAGES: frozenset[str] = frozenset(
    {"transcribed", "described", "transcribe_failed", "describe_failed", "sent"}
)


class WriteOutcome(str, Enum):
    """Result of a single VaultWriter.write_section call."""

    APPENDED = "appended"
    REPLACED = "replaced"
    NO_OP = "no_op"  # event already at the same terminal stage


@dataclass(frozen=True)
class Section:
    """A single message section in a per-day vault file.

    `fields` are rendered as ``**key:** value`` lines between the header
    and the body. `body` is freeform Markdown — for voice this is the
    transcript inside a blockquote; for image, the description; for
    text, the message body verbatim.
    """

    event_id: str
    timestamp: datetime          # tz-aware; writer converts to its configured zone
    sender: str                  # display name as it should appear in the header
    kind: MessageKind
    stage: Stage
    fields: dict[str, str] = field(default_factory=dict)
    body: str = ""


@dataclass(frozen=True)
class GateInput:
    """Inputs to :func:`hermes_chat_recorder.gate.Gate.should_wake`.

    Kept as a dataclass so call-sites are explicit about what's being
    passed; misordered positional arguments are a real risk in this
    function and a dataclass makes them all keyword-only at call.
    """

    gate_text: str
    bot_mxid: str
    sender_mxid: str
    mentioned_mxids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DescribeResult:
    """Output of :func:`hermes_chat_recorder.describer.ImageDescriber.describe`."""

    description: str          # freeform AI prose; never gated on
    text: str                 # OCR'd / literal text visible in image; gated on
    raw: str                  # full raw response, kept for debugging
