"""Tests for the OpenRouter vision describer.

The real API costs money + needs network; we inject a fake HTTP
client that records the request and returns canned responses. The
tests cover:

- Happy-path describe (DESCRIPTION + TEXT sections parsed correctly)
- "(none)" TEXT sentinel collapses to empty string
- Description-only response (no TEXT header)
- Missing both headers — whole content becomes description
- Empty image bytes rejected early
- HTTP 4xx / 5xx → ImageDescriberError
- Transport error → ImageDescriberError
- Malformed JSON / missing choices key → ImageDescriberError
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from hermes_chat_recorder.describer import (
    DEFAULT_BASE_URL,
    ImageDescriber,
    ImageDescriberError,
    parse_description,
)


@dataclass
class _FakeResp:
    status_code: int
    payload: Any = None
    text: str = ""

    def json(self) -> Any:
        if self.payload is None:
            raise ValueError("no json payload set")
        return self.payload

    def raise_for_status(self) -> None:  # pragma: no cover - unused
        pass


class _FakeHttp:
    def __init__(self, response: _FakeResp | Exception):
        self.response = response
        self.calls: list[dict] = []

    def post(self, url: str, *, headers, json, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _ok_response(content: str) -> _FakeResp:
    return _FakeResp(
        status_code=200,
        payload={"choices": [{"message": {"content": content}}]},
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_round_trip_describes_image_with_both_sections() -> None:
    http = _FakeHttp(
        _ok_response(
            "DESCRIPTION:\nA whiteboard photo from a low angle.\n\nTEXT:\nPilot scope\nRisk"
        )
    )
    d = ImageDescriber(api_key="key", http=http)
    result = d.describe(b"\x89PNG\r\n\x1a\nfakepayload", mime="image/png")
    assert result.description == "A whiteboard photo from a low angle."
    assert result.text == "Pilot scope\nRisk"
    assert "DESCRIPTION:" in result.raw

    # Verify the HTTP call shape.
    call = http.calls[0]
    assert call["url"] == f"{DEFAULT_BASE_URL}/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer key"
    body = call["json"]
    assert body["model"] == "google/gemini-3-flash-preview"
    parts = body["messages"][0]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_text_block_with_none_sentinel_collapses_to_empty() -> None:
    http = _FakeHttp(
        _ok_response("DESCRIPTION:\nA bird in flight.\n\nTEXT:\n(none)")
    )
    d = ImageDescriber(api_key="k", http=http)
    result = d.describe(b"x")
    assert result.description == "A bird in flight."
    assert result.text == ""


def test_only_description_header_present() -> None:
    http = _FakeHttp(_ok_response("DESCRIPTION:\nA forest at dawn."))
    d = ImageDescriber(api_key="k", http=http)
    result = d.describe(b"x")
    assert result.description == "A forest at dawn."
    assert result.text == ""


def test_no_section_headers_uses_whole_content_as_description() -> None:
    http = _FakeHttp(_ok_response("Just a plain description with no headers."))
    d = ImageDescriber(api_key="k", http=http)
    result = d.describe(b"x")
    assert result.description == "Just a plain description with no headers."
    assert result.text == ""


def test_case_insensitive_section_headers() -> None:
    http = _FakeHttp(_ok_response("description:\nLower-case header.\n\ntext:\nLABEL"))
    d = ImageDescriber(api_key="k", http=http)
    result = d.describe(b"x")
    assert result.description == "Lower-case header."
    assert result.text == "LABEL"


def test_custom_model_and_base_url_passed_through() -> None:
    http = _FakeHttp(_ok_response("DESCRIPTION:\nx"))
    d = ImageDescriber(
        api_key="k",
        model="anthropic/claude-3-5-sonnet",
        base_url="https://example.invalid/v1/",
        http=http,
    )
    d.describe(b"x")
    call = http.calls[0]
    assert call["url"] == "https://example.invalid/v1/chat/completions"
    assert call["json"]["model"] == "anthropic/claude-3-5-sonnet"


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_empty_image_bytes_rejected_early() -> None:
    http = _FakeHttp(_ok_response("DESCRIPTION:\nshould not be called"))
    d = ImageDescriber(api_key="k", http=http)
    with pytest.raises(ImageDescriberError, match="empty image"):
        d.describe(b"")
    assert http.calls == []


@pytest.mark.parametrize("code", [400, 401, 403, 429, 500, 503])
def test_http_errors_raise_describer_error(code: int) -> None:
    http = _FakeHttp(_FakeResp(status_code=code, text=f"err{code}"))
    d = ImageDescriber(api_key="k", http=http)
    with pytest.raises(ImageDescriberError, match=str(code)):
        d.describe(b"x")


def test_transport_exception_wraps_to_describer_error() -> None:
    http = _FakeHttp(ConnectionError("dns went sideways"))
    d = ImageDescriber(api_key="k", http=http)
    with pytest.raises(ImageDescriberError, match="dns went sideways"):
        d.describe(b"x")


def test_missing_choices_key_raises() -> None:
    http = _FakeHttp(_FakeResp(status_code=200, payload={"unexpected": "shape"}))
    d = ImageDescriber(api_key="k", http=http)
    with pytest.raises(ImageDescriberError, match="response shape"):
        d.describe(b"x")


def test_non_string_message_content_raises() -> None:
    http = _FakeHttp(
        _FakeResp(
            status_code=200,
            payload={"choices": [{"message": {"content": ["not", "a", "string"]}}]},
        )
    )
    d = ImageDescriber(api_key="k", http=http)
    with pytest.raises(ImageDescriberError, match="string message content"):
        d.describe(b"x")


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
