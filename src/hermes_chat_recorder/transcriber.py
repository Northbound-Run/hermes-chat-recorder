"""Voice-to-text via ``faster-whisper``.

The transcriber is intentionally tiny — a thin sync wrapper around a
single ``WhisperModel`` instance with a ``threading.Lock`` to serialize
calls (CTranslate2's thread safety isn't documented and we'd rather be
safe than chase a heisenbug).

Sync is the right shape because the Hermes ``pre_gateway_dispatch``
hook is itself sync; see ``docs/DESIGN.md §5``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Protocol


class WhisperLike(Protocol):
    """Minimal model surface we depend on.

    Real implementation: ``faster_whisper.WhisperModel``. Tests pass a
    fake conforming to this protocol so they don't need the C++ runtime.
    """

    def transcribe(self, audio: str, **kwargs):  # noqa: D401 - faster-whisper signature
        ...


class TranscriberError(Exception):
    """Raised when transcription fails terminally.

    Per ``docs/DESIGN.md §6``, callers catch this and mark the section
    ``stage:transcribe_failed`` — they should not let it bubble up to
    the gateway dispatch loop.
    """


class Transcriber:
    """Wraps a single Whisper model with a serialization lock."""

    def __init__(
        self,
        *,
        model_size: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        model: WhisperLike | None = None,
    ) -> None:
        """Construct.

        Pass ``model`` to inject a fake (tests). When ``model`` is None,
        the real ``faster_whisper.WhisperModel`` is constructed with the
        given size / device / compute_type. The import is lazy so the
        package can be imported in environments where faster-whisper
        isn't installed yet.
        """
        if model is not None:
            self._model: WhisperLike = model
        else:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._lock = threading.Lock()
        self.model_size = model_size

    def transcribe(self, audio_path: str | Path, *, language: str | None = None) -> str:
        """Return the concatenated transcript text.

        Blocks on ``self._lock`` so concurrent calls serialize. The
        Whisper segment generator is consumed inside the lock — the
        heavy compute happens during iteration, not during the
        ``transcribe()`` call itself.
        """
        path_str = str(audio_path)
        with self._lock:
            try:
                segments, _info = self._model.transcribe(path_str, language=language)
                pieces: list[str] = []
                for seg in segments:
                    text = getattr(seg, "text", "")
                    if isinstance(text, str):
                        stripped = text.strip()
                        if stripped:
                            pieces.append(stripped)
                return " ".join(pieces)
            except Exception as exc:  # noqa: BLE001 - we wrap all model errors as one type
                raise TranscriberError(f"faster-whisper failed on {path_str!r}: {exc}") from exc
