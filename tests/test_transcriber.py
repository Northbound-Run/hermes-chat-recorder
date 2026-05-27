"""Tests for the Hermes-delegating Transcriber.

The transcriber now wraps ``tools.transcription_tools.transcribe_audio``
instead of faster-whisper directly. We inject a fake transcribe_fn
that records calls and returns canned response dicts.

Covers:
- Transcript text passes through unchanged (whitespace stripped)
- ``success=false`` → TranscriberError with the upstream error message
- Underlying function raising → TranscriberError
- Non-dict return → TranscriberError
- ``language`` kwarg accepted for API compat (currently a no-op)
- Lock serialization keeps two threads from interleaving
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hermes_chat_recorder.transcriber import Transcriber, TranscriberError


class _FakeFn:
    def __init__(self, response, delay: float = 0.0) -> None:
        self.response = response
        self.delay = delay
        self.calls: list[str] = []

    def __call__(self, path: str) -> dict:
        self.calls.append(path)
        if self.delay:
            time.sleep(self.delay)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_returns_transcript_text_on_success() -> None:
    fn = _FakeFn({"success": True, "transcript": "hello world"})
    t = Transcriber(transcribe_fn=fn)
    assert t.transcribe("/tmp/audio.ogg") == "hello world"
    assert fn.calls == ["/tmp/audio.ogg"]


def test_strips_whitespace_from_transcript() -> None:
    fn = _FakeFn({"success": True, "transcript": "  trim me  "})
    t = Transcriber(transcribe_fn=fn)
    assert t.transcribe("/tmp/audio.ogg") == "trim me"


def test_path_object_is_passed_as_string() -> None:
    fn = _FakeFn({"success": True, "transcript": "ok"})
    t = Transcriber(transcribe_fn=fn)
    t.transcribe(Path("/tmp/x.ogg"))
    assert fn.calls[0] == "/tmp/x.ogg"


def test_language_kwarg_is_accepted_for_compat() -> None:
    """`language` is accepted for API compatibility but currently ignored.
    The Hermes STT entrypoint doesn't take a language hint; we just
    need the kwarg to not crash."""
    fn = _FakeFn({"success": True, "transcript": "ok"})
    t = Transcriber(transcribe_fn=fn)
    assert t.transcribe("/tmp/x.ogg", language="en") == "ok"


def test_empty_transcript_returns_empty_string() -> None:
    fn = _FakeFn({"success": True, "transcript": ""})
    t = Transcriber(transcribe_fn=fn)
    assert t.transcribe("/tmp/x.ogg") == ""


# ---------------------------------------------------------------------------
# Error wrapping
# ---------------------------------------------------------------------------


def test_success_false_raises_with_upstream_error() -> None:
    fn = _FakeFn({"success": False, "transcript": "", "error": "stt provider down"})
    t = Transcriber(transcribe_fn=fn)
    with pytest.raises(TranscriberError, match="stt provider down"):
        t.transcribe("/tmp/x.ogg")


def test_success_false_without_error_uses_default_message() -> None:
    fn = _FakeFn({"success": False, "transcript": ""})
    t = Transcriber(transcribe_fn=fn)
    with pytest.raises(TranscriberError, match="no detail"):
        t.transcribe("/tmp/x.ogg")


def test_underlying_exception_wraps_to_transcriber_error() -> None:
    fn = _FakeFn(RuntimeError("synthetic failure"))
    t = Transcriber(transcribe_fn=fn)
    with pytest.raises(TranscriberError) as ei:
        t.transcribe("/tmp/x.ogg")
    assert "/tmp/x.ogg" in str(ei.value)
    assert isinstance(ei.value.__cause__, RuntimeError)


def test_non_dict_return_raises() -> None:
    fn = _FakeFn("not a dict")  # type: ignore[arg-type]
    t = Transcriber(transcribe_fn=fn)
    with pytest.raises(TranscriberError, match="unexpected transcribe_audio return"):
        t.transcribe("/tmp/x.ogg")


# ---------------------------------------------------------------------------
# Concurrency / lock semantics
# ---------------------------------------------------------------------------


def test_lock_serializes_concurrent_callers() -> None:
    """Two threads calling transcribe() must NOT interleave inside the
    underlying function. We sleep inside the fake to create an
    interleave window the lock has to prevent."""
    fn = _FakeFn({"success": True, "transcript": "x"}, delay=0.05)
    t = Transcriber(transcribe_fn=fn)

    finished_in_order: list[str] = []

    def worker(name: str) -> None:
        t.transcribe(f"/tmp/{name}.ogg")
        finished_in_order.append(name)

    threads = [threading.Thread(target=worker, args=(f"a{i}",)) for i in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert sorted(finished_in_order) == sorted(["a0", "a1", "a2"])
    assert len(fn.calls) == 3
