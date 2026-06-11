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
    "recorded",
    "transcribed",
    "described",
    "transcribe_failed",
    "describe_failed",
    "sent",
    "edited",
]

# Logical kind of a message — picked so the recorder and writer don't
# need to know any platform's wire-level constants. "voice" and "image"
# go through the STT / vision pipelines; "video", "file", and
# "location" are recorded as-is (stage "recorded") without processing.
MessageKind = Literal[
    "text", "voice", "image", "video", "file", "location", "reply"
]


# Stages whose sections should NOT be replaced once written, except by
# the SAME stage (in which case the write is a no-op). "recorded" is
# the terminal stage for kinds that have no processing pipeline.
TERMINAL_STAGES: frozenset[str] = frozenset(
    {
        "recorded",
        "transcribed",
        "described",
        "transcribe_failed",
        "describe_failed",
        "sent",
        "edited",
    }
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
class DescribeResult:
    """Output of :func:`hermes_chat_recorder.describer.ImageDescriber.describe`."""

    description: str          # freeform AI prose describing the image
    text: str                 # OCR'd / literal text visible in the image
    raw: str                  # full raw response, kept for debugging
