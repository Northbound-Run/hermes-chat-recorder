"""Voice-to-text via Hermes's built-in transcription service.

Delegates to ``tools.transcription_tools.transcribe_audio``, which
routes to whichever STT provider Hermes is configured for (local
faster-whisper, Groq, OpenAI, Mistral, xAI). Hermes manages the model
download, lifetime, and per-provider credentials — this package
carries no STT-specific dependencies or knobs of its own.

The hook that calls us is sync, so this wrapper stays sync. A
``threading.Lock`` serializes calls because the underlying provider's
thread-safety story varies (and CTranslate2 has historically been
flaky under concurrent access).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Signature: (audio_path: str) ->
#   {"success": bool, "transcript": str, "error"?: str, "provider"?: str}
TranscribeFn = Callable[[str], dict]


class TranscriberError(Exception):
    """Raised when transcription fails terminally.

    Callers catch this and mark the section ``stage:transcribe_failed``
    — never let it propagate to the gateway dispatch loop.
    """


def _default_transcribe_fn(path: str) -> dict[str, Any]:
    """Lazy import of Hermes's STT entrypoint.

    Imported inside the function so the package remains importable
    outside Hermes (tests inject a fake transcribe_fn and never trigger
    this code path).
    """
    from tools.transcription_tools import transcribe_audio  # type: ignore[import-not-found]

    return transcribe_audio(path)


class Transcriber:
    """Sync wrapper around Hermes's STT."""

    def __init__(self, *, transcribe_fn: TranscribeFn | None = None) -> None:
        """Construct.

        Pass ``transcribe_fn`` to inject a fake in tests. When ``None``,
        the wrapper resolves the real function lazily on first call so
        the package can be imported outside Hermes.
        """
        self._transcribe_fn: TranscribeFn = transcribe_fn or _default_transcribe_fn
        self._lock = threading.Lock()

    def transcribe(self, audio_path: str | Path, *, language: str | None = None) -> str:
        """Return the transcript text.

        Raises :class:`TranscriberError` on any failure (provider down,
        STT disabled in config, malformed audio, etc.). The
        ``language`` parameter is accepted for API compatibility with
        the old faster-whisper-backed implementation but is currently
        ignored — Hermes's ``transcribe_audio`` doesn't take a hint.
        """
        path_str = str(audio_path)
        with self._lock:
            try:
                result = self._transcribe_fn(path_str)
            except Exception as exc:
                raise TranscriberError(
                    f"hermes STT raised on {path_str!r}: {exc}"
                ) from exc

        if not isinstance(result, dict):
            raise TranscriberError(
                f"unexpected transcribe_audio return type: {type(result).__name__}"
            )
        if not result.get("success"):
            err = result.get("error") or "transcription failed (no detail)"
            raise TranscriberError(str(err))
        text = result.get("transcript", "")
        return text.strip() if isinstance(text, str) else ""
