"""Vault Markdown writer with stage-based section lifecycle.

The writer is the canonical persistence layer. Everything else in the
package feeds Section dataclasses into :class:`VaultWriter`, and the
writer handles:

* day-file path resolution (per-event local-date)
* section append for new events (idempotency anchor = HTML comment)
* in-place section replacement for stage transitions
* no-op when an already-terminal section is rewritten at the same stage
* per-(room, date) ``threading.Lock`` so concurrent writers don't
  corrupt each other

See ``docs/DESIGN.md §3`` for the design rationale and full lifecycle.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9 fallback, but pyproject requires 3.10+
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from hermes_chat_recorder.types import (
    TERMINAL_STAGES,
    Section,
    WriteOutcome,
)


# Used to read the stage marker off an existing rendered section.
_STAGE_PATTERN = re.compile(r"·\s*stage:(?P<stage>[A-Za-z_]+)")
# Section terminator. Anchored to start-of-line so a literal "---" inside
# a transcript body won't be misread as a terminator.
_TERMINATOR = "\n---\n"


def _anchor(event_id: str) -> str:
    """Render the HTML-comment idempotency anchor for an event."""
    return f"<!-- event:{event_id} -->"


def render_section(section: Section) -> str:
    """Render a :class:`Section` to its canonical Markdown form.

    Format follows ``docs/DESIGN.md §3``::

        <!-- event:$id -->
        ### HH:MM Sender · kind · stage:<stage>
        **field:** value
        ...

        body content

        ---

    The trailing terminator (``\\n---\\n``) is included so concatenating
    rendered sections yields a valid day file.
    """
    lines: list[str] = []
    lines.append(_anchor(section.event_id))
    lines.append(
        f"### {section.timestamp.strftime('%H:%M')} {section.sender} "
        f"· {section.kind} · stage:{section.stage}"
    )
    for key, value in section.fields.items():
        lines.append(f"**{key}:** {value}")
    if section.body:
        lines.append("")
        lines.append(section.body.rstrip("\n"))
    lines.append("")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _find_existing_section(content: str, event_id: str) -> tuple[int, int, str] | None:
    """Locate an existing section by its event_id anchor.

    Returns ``(start, end, section_text)`` if found, else None.
    ``start`` is the index of the anchor; ``end`` is the index ONE PAST
    the trailing ``\\n---\\n`` (so ``content[start:end]`` is the full
    section, replaceable wholesale).
    """
    anchor = _anchor(event_id)
    idx = content.find(anchor)
    if idx == -1:
        return None
    # Search for the terminator after the anchor.
    term_idx = content.find(_TERMINATOR, idx + len(anchor))
    if term_idx == -1:
        # Malformed (no terminator). Treat rest of file as the section
        # so a subsequent write can replace it cleanly.
        end = len(content)
    else:
        end = term_idx + len(_TERMINATOR)
    return idx, end, content[idx:end]


def _extract_stage(section_text: str) -> str | None:
    """Return the stage marker from an existing rendered section, or None."""
    m = _STAGE_PATTERN.search(section_text)
    return m.group("stage") if m else None


class VaultWriter:
    """Thread-safe writer for stage-based markdown vault sections.

    Construct once and share across all callers in the process — locks
    are cached internally per (room_slug, date) so concurrent calls to
    different day files don't contend.
    """

    def __init__(self, *, vault_root: Path | str, timezone: str = "America/Los_Angeles") -> None:
        self._vault_root = Path(vault_root)
        self._tz = ZoneInfo(timezone)
        # Maps (room_slug, "YYYY-MM-DD") -> Lock. Created lazily.
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_lock = threading.Lock()

    @property
    def vault_root(self) -> Path:
        return self._vault_root

    @property
    def timezone(self) -> ZoneInfo:
        return self._tz

    def day_file_path(self, room_slug: str, timestamp: datetime) -> Path:
        """Return the absolute path of the per-day file an event belongs in."""
        local = self._localize(timestamp)
        return self._vault_root / room_slug / f"{local.strftime('%Y-%m-%d')}.md"

    def write_section(self, section: Section, *, room_slug: str) -> WriteOutcome:
        """Append or update a section in the appropriate day file.

        Behaviour matches ``docs/DESIGN.md §3``:

        * **APPENDED** — no existing section with this event_id; section
          added at the end of the day file.
        * **REPLACED** — section existed at a non-terminal stage, OR at
          a terminal stage different from the new one; section content
          replaced in-place.
        * **NO_OP** — section existed at the SAME terminal stage. Nothing
          written. Returned so callers can distinguish a true no-op from
          a successful write.
        """
        local_ts = self._localize(section.timestamp)
        date_str = local_ts.strftime("%Y-%m-%d")
        day_file = self.day_file_path(room_slug, section.timestamp)

        lock = self._get_lock(room_slug, date_str)
        with lock:
            day_file.parent.mkdir(parents=True, exist_ok=True)
            existing_content = day_file.read_text(encoding="utf-8") if day_file.exists() else ""

            found = _find_existing_section(existing_content, section.event_id)
            new_section = render_section(section)

            if found is None:
                # Append.
                if existing_content and not existing_content.endswith("\n"):
                    existing_content += "\n"
                day_file.write_text(existing_content + new_section, encoding="utf-8")
                return WriteOutcome.APPENDED

            start, end, existing_section_text = found
            existing_stage = _extract_stage(existing_section_text)

            if (
                existing_stage is not None
                and existing_stage in TERMINAL_STAGES
                and existing_stage == section.stage
            ):
                # Already at the same terminal stage. Idempotent skip.
                return WriteOutcome.NO_OP

            replaced = existing_content[:start] + new_section + existing_content[end:]
            day_file.write_text(replaced, encoding="utf-8")
            return WriteOutcome.REPLACED

    def has_event(self, event_id: str, room_slug: str, timestamp: datetime) -> bool:
        """Return True if a section for this event_id already exists in
        the appropriate day file. Useful for sync-replay short-circuit."""
        day_file = self.day_file_path(room_slug, timestamp)
        if not day_file.exists():
            return False
        return _anchor(event_id) in day_file.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _localize(self, ts: datetime) -> datetime:
        if ts.tzinfo is None:
            # Naive datetime — assume the caller meant the writer's tz.
            return ts.replace(tzinfo=self._tz)
        return ts.astimezone(self._tz)

    def _get_lock(self, room_slug: str, date_str: str) -> threading.Lock:
        key = (room_slug, date_str)
        with self._locks_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock
