"""Tests for the faster-whisper wrapper.

The real model is too heavy to load in unit tests, so we inject a fake
that conforms to the WhisperLike protocol. The tests verify:

- Segment joining + whitespace handling
- Empty / blank-segment behaviour
- TranscriberError wrapping on model exceptions
- Lock serialization keeps two threads from interleaving model access
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import pytest

from hermes_chat_recorder.transcriber import Transcriber, TranscriberError


@dataclass
class _Seg:
    text: str


class _FakeModel:
    def __init__(self, segments: list[_Seg], delay: float = 0.0) -> None:
        self.segments = segments
        self.delay = delay
        self.calls: list[tuple[str, dict]] = []

    def transcribe(self, audio: str, **kwargs):
        self.calls.append((audio, dict(kwargs)))
        if self.delay:
            time.sleep(self.delay)
        # faster-whisper returns (iterator, info)
        info = object()
        return iter(self.segments), info


class _FakeRaisingModel:
    def transcribe(self, audio: str, **kwargs):
        raise RuntimeError("synthetic failure")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_concatenates_segments_with_single_spaces() -> None:
    fake = _FakeModel(
        [_Seg(text=" hello "), _Seg(text="world  "), _Seg(text="  again")]
    )
    t = Transcriber(model=fake)
    assert t.transcribe("/tmp/audio.ogg") == "hello world again"


def test_skips_blank_segments() -> None:
    fake = _FakeModel([_Seg(text=""), _Seg(text="  "), _Seg(text="real content")])
    t = Transcriber(model=fake)
    assert t.transcribe("/tmp/audio.ogg") == "real content"


def test_empty_segment_list_returns_empty_string() -> None:
    t = Transcriber(model=_FakeModel([]))
    assert t.transcribe("/tmp/empty.ogg") == ""


def test_path_is_passed_as_string_to_model() -> None:
    fake = _FakeModel([_Seg(text="ok")])
    t = Transcriber(model=fake)
    from pathlib import Path

    t.transcribe(Path("/tmp/x.ogg"))
    assert fake.calls[0][0] == "/tmp/x.ogg"


def test_language_kwarg_is_forwarded_when_set() -> None:
    fake = _FakeModel([_Seg(text="ok")])
    t = Transcriber(model=fake)
    t.transcribe("/tmp/x.ogg", language="en")
    assert fake.calls[0][1].get("language") == "en"


def test_language_defaults_to_none() -> None:
    fake = _FakeModel([_Seg(text="ok")])
    t = Transcriber(model=fake)
    t.transcribe("/tmp/x.ogg")
    assert fake.calls[0][1].get("language") is None


def test_model_size_property_is_recorded() -> None:
    t = Transcriber(model=_FakeModel([]), model_size="small")
    assert t.model_size == "small"


# ---------------------------------------------------------------------------
# Error wrapping
# ---------------------------------------------------------------------------


def test_model_exception_becomes_transcriber_error() -> None:
    t = Transcriber(model=_FakeRaisingModel())
    with pytest.raises(TranscriberError) as ei:
        t.transcribe("/tmp/x.ogg")
    assert "/tmp/x.ogg" in str(ei.value)
    assert isinstance(ei.value.__cause__, RuntimeError)


# ---------------------------------------------------------------------------
# Concurrency / lock semantics
# ---------------------------------------------------------------------------


def test_lock_serializes_concurrent_callers() -> None:
    """Two threads calling transcribe() must NOT interleave inside the
    model. The fake records call entries; we sleep inside the model to
    create an interleave window the lock has to prevent."""
    fake = _FakeModel([_Seg(text="x")], delay=0.05)
    t = Transcriber(model=fake)

    finished_in_order: list[str] = []

    def worker(name: str) -> None:
        t.transcribe(f"/tmp/{name}.ogg")
        finished_in_order.append(name)

    threads = [threading.Thread(target=worker, args=(f"a{i}",)) for i in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    # All three got through.
    assert sorted(finished_in_order) == sorted(["a0", "a1", "a2"])
    # And model.calls reflects all three call entries in some order.
    assert len(fake.calls) == 3
