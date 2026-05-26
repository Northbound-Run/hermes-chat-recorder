"""Image description via OpenRouter's multimodal chat completions.

Sync HTTPS, base64-encoded image payload, two-section prompt asking
for a freeform DESCRIPTION and an OCR'd TEXT block. Mirrors the
TypeScript implementation at
``~/Git/northbound-os/src/mastra/channels/transcript/image-describer.ts``
in protocol — the parse function accepts the same prompt shape.

Per ``docs/DESIGN.md §4`` and ``§6`` we keep DESCRIPTION and TEXT
separate at the dataclass level so the wake gate can use TEXT only
(model-generated DESCRIPTION must never be allowed to false-wake the
agent).
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Protocol

from hermes_chat_recorder.types import DescribeResult

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "google/gemini-3-flash-preview"
DEFAULT_TIMEOUT_SEC = 120.0
DEFAULT_PROMPT = """You will receive an image. Produce TWO sections.

DESCRIPTION:
Write 1-3 short sentences describing what is in the image: subject, setting, notable details. Avoid speculation about who people are.

TEXT:
Transcribe any literal text visible in the image, preserving line breaks. If no text appears in the image, write "(none)"."""


class HttpResponse(Protocol):
    """Minimal response surface — what we need from httpx.Response."""

    @property
    def status_code(self) -> int: ...
    def json(self) -> Any: ...
    @property
    def text(self) -> str: ...
    def raise_for_status(self) -> None: ...


class HttpClient(Protocol):
    """Minimal client surface — what we need from httpx.Client."""

    def post(
        self, url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float | None = ...
    ) -> HttpResponse: ...


class ImageDescriberError(Exception):
    """Raised when describing fails terminally.

    Per ``docs/DESIGN.md §6``, callers catch this and mark the section
    ``stage:describe_failed``. Don't let it propagate to the gateway
    dispatch loop.
    """


class ImageDescriber:
    """Describe an image via OpenRouter, return parsed DESCRIPTION + TEXT."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        prompt: str = DEFAULT_PROMPT,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        http: HttpClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._prompt = prompt
        self._timeout_sec = timeout_sec
        if http is not None:
            self._http: HttpClient = http
        else:
            import httpx

            self._http = httpx.Client(timeout=timeout_sec)

    def describe(self, image_bytes: bytes, *, mime: str = "image/png") -> DescribeResult:
        """Describe an image. Returns parsed sections; raises on failure."""
        if not image_bytes:
            raise ImageDescriberError("empty image bytes")

        data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = self._http.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self._timeout_sec,
            )
        except Exception as exc:  # noqa: BLE001 - wrap transport errors uniformly
            raise ImageDescriberError(f"openrouter transport error: {exc}") from exc

        if resp.status_code >= 400:
            raise ImageDescriberError(
                f"openrouter returned HTTP {resp.status_code}: {resp.text[:200]!r}"
            )

        try:
            body = resp.json()
            raw_content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise ImageDescriberError(f"unexpected openrouter response shape: {exc}") from exc

        if not isinstance(raw_content, str):
            raise ImageDescriberError(
                f"expected string message content, got {type(raw_content).__name__}"
            )

        return parse_description(raw_content)


def parse_description(raw: str) -> DescribeResult:
    """Split a two-section response into DESCRIPTION + TEXT.

    Robust to:

    - Missing section headers (treats whole body as description)
    - Different case on headers (DESCRIPTION:, description:)
    - Surrounding whitespace
    - "(none)" sentinel in TEXT
    """
    raw = raw or ""
    lower = raw.lower()

    def _find(label: str) -> int:
        idx = lower.find(label)
        return idx

    desc_idx = _find("description:")
    text_idx = _find("text:")

    description = ""
    text = ""

    if desc_idx == -1 and text_idx == -1:
        # No section headers — treat the whole blob as description.
        description = raw.strip()
        return DescribeResult(description=description, text="", raw=raw)

    if desc_idx != -1:
        desc_start = desc_idx + len("description:")
        if text_idx != -1 and text_idx > desc_idx:
            description = raw[desc_start:text_idx].strip()
            text = raw[text_idx + len("text:"):].strip()
        else:
            description = raw[desc_start:].strip()

    if text_idx != -1 and desc_idx == -1:
        text = raw[text_idx + len("text:"):].strip()

    if text == "(none)":
        text = ""

    return DescribeResult(description=description, text=text, raw=raw)
