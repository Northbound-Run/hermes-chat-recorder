"""Tests for the Hermes-delegating ImageDescriber.

The describer now wraps Hermes's ``tools.vision_tools.vision_analyze_tool``
instead of calling OpenRouter directly. We inject a fake ``vision_fn``
that records the request and returns canned JSON strings.

Covers:
- Happy-path describe (DESCRIPTION + TEXT sections parsed correctly)
- "(none)" TEXT sentinel collapses to empty string
- Description-only response (no TEXT header)
- Missing both headers — whole content becomes description
- Empty image bytes rejected early
- Vision tool returning ``success=false`` → ImageDescriberError
- Vision tool raising → ImageDescriberError
- Malformed JSON → ImageDescriberError
- parse_description edge cases
"""

from __future__ import annotations

import json

import pytest

from hermes_chat_recorder.describer import (
    DEFAULT_PROMPT,
    ImageDescriber,
    ImageDescriberError,
    parse_description,
)


def _ok(analysis: str) -> str:
    return json.dumps({"success": True, "analysis": analysis})


def _fail(message: str) -> str:
    return json.dumps({"success": False, "analysis": message})


class _FakeVision:
    """Sync callable that records the call. Tests don't need async
    semantics here because the describer treats sync and awaitable
    return values uniformly."""

    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def __call__(self, path: str, prompt: str):
        self.calls.append((path, prompt))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_round_trip_describes_image_with_both_sections() -> None:
    vision = _FakeVision(
        _ok("DESCRIPTION:\nA whiteboard photo from a low angle.\n\nTEXT:\nPilot scope\nRisk")
    )
    d = ImageDescriber(vision_fn=vision)
    result = d.describe(b"\x89PNG\r\n\x1a\nfakepayload", mime="image/png")

    assert result.description == "A whiteboard photo from a low angle."
    assert result.text == "Pilot scope\nRisk"
    assert "DESCRIPTION:" in result.raw

    # Verify the vision call used a tempfile path (not bytes) and the
    # default two-section prompt.
    assert len(vision.calls) == 1
    path, prompt = vision.calls[0]
    assert path.endswith(".png")
    assert prompt == DEFAULT_PROMPT


def test_text_block_with_none_sentinel_collapses_to_empty() -> None:
    vision = _FakeVision(_ok("DESCRIPTION:\nA bird in flight.\n\nTEXT:\n(none)"))
    d = ImageDescriber(vision_fn=vision)
    result = d.describe(b"x")
    assert result.description == "A bird in flight."
    assert result.text == ""


def test_only_description_header_present() -> None:
    vision = _FakeVision(_ok("DESCRIPTION:\nA forest at dawn."))
    d = ImageDescriber(vision_fn=vision)
    result = d.describe(b"x")
    assert result.description == "A forest at dawn."
    assert result.text == ""


def test_no_section_headers_uses_whole_content_as_description() -> None:
    vision = _FakeVision(_ok("Just a plain description with no headers."))
    d = ImageDescriber(vision_fn=vision)
    result = d.describe(b"x")
    assert result.description == "Just a plain description with no headers."
    assert result.text == ""


def test_case_insensitive_section_headers() -> None:
    vision = _FakeVision(_ok("description:\nLower-case header.\n\ntext:\nLABEL"))
    d = ImageDescriber(vision_fn=vision)
    result = d.describe(b"x")
    assert result.description == "Lower-case header."
    assert result.text == "LABEL"


def test_custom_prompt_passed_to_vision() -> None:
    custom = "Just describe in one word."
    vision = _FakeVision(_ok("Cat"))
    d = ImageDescriber(prompt=custom, vision_fn=vision)
    d.describe(b"x")
    assert vision.calls[0][1] == custom


@pytest.mark.parametrize(
    "mime,suffix",
    [
        ("image/jpeg", ".jpg"),
        ("image/png", ".png"),
        ("image/gif", ".gif"),
        ("image/webp", ".webp"),
        ("application/octet-stream", ".img"),
    ],
)
def test_tempfile_uses_correct_extension_for_mime(mime: str, suffix: str) -> None:
    vision = _FakeVision(_ok("DESCRIPTION:\nx"))
    d = ImageDescriber(vision_fn=vision)
    d.describe(b"x", mime=mime)
    assert vision.calls[0][0].endswith(suffix)


# ---------------------------------------------------------------------------
# Vision returning an awaitable (async function) is awaited via run_async
# ---------------------------------------------------------------------------


def test_async_vision_response_is_awaited() -> None:
    async def _async_vision(path: str, prompt: str):
        return _ok("DESCRIPTION:\nAsync.")

    # Inject a simple run_async that drives the coroutine to completion.
    import asyncio

    def _run(coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    d = ImageDescriber(vision_fn=_async_vision, run_async=_run)
    result = d.describe(b"x")
    assert result.description == "Async."


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_empty_image_bytes_rejected_early() -> None:
    vision = _FakeVision(_ok("DESCRIPTION:\nshould not be called"))
    d = ImageDescriber(vision_fn=vision)
    with pytest.raises(ImageDescriberError, match="empty image"):
        d.describe(b"")
    assert vision.calls == []


def test_vision_returns_failure_raises() -> None:
    vision = _FakeVision(_fail("vision provider unavailable"))
    d = ImageDescriber(vision_fn=vision)
    with pytest.raises(ImageDescriberError, match="vision provider unavailable"):
        d.describe(b"x")


def test_vision_exception_wraps_to_describer_error() -> None:
    vision = _FakeVision(ConnectionError("dns went sideways"))
    d = ImageDescriber(vision_fn=vision)
    with pytest.raises(ImageDescriberError, match="dns went sideways"):
        d.describe(b"x")


def test_malformed_json_raises() -> None:
    vision = _FakeVision("this is not json at all")
    d = ImageDescriber(vision_fn=vision)
    with pytest.raises(ImageDescriberError, match="not valid JSON"):
        d.describe(b"x")


def test_non_dict_parsed_response_raises() -> None:
    vision = _FakeVision(json.dumps(["not", "a", "dict"]))
    d = ImageDescriber(vision_fn=vision)
    with pytest.raises(ImageDescriberError, match="response shape"):
        d.describe(b"x")


def test_dict_response_accepted_directly() -> None:
    """Tolerate already-parsed dict responses (forward-compat with
    upstream Hermes if it ever stops JSON-encoding its return)."""
    vision = _FakeVision({"success": True, "analysis": "DESCRIPTION:\nx"})
    d = ImageDescriber(vision_fn=vision)
    result = d.describe(b"x")
    assert result.description == "x"


# ---------------------------------------------------------------------------
# parse_description() — exercised directly for edge coverage
# ---------------------------------------------------------------------------


def test_parse_description_empty_string() -> None:
    result = parse_description("")
    assert result.description == ""
    assert result.text == ""
    assert result.raw == ""


def test_parse_description_only_text_block() -> None:
    """A response that's only a TEXT block (no DESCRIPTION header) —
    rare but possible. Should populate text only."""
    result = parse_description("TEXT:\nVISIBLE LABEL")
    assert result.description == ""
    assert result.text == "VISIBLE LABEL"


def test_parse_description_whitespace_around_sections() -> None:
    raw = "\n\nDESCRIPTION:\n\n   A photo.   \n\nTEXT:\n\n  Hello  \n\n"
    result = parse_description(raw)
    assert result.description == "A photo."
    assert result.text == "Hello"
