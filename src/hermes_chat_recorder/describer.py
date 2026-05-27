"""Image description via Hermes's built-in vision service.

Delegates to ``tools.vision_tools.vision_analyze_tool``, which routes
the request through Hermes's vision pipeline (whatever auxiliary
provider Hermes is configured for — main LLM with vision, OpenRouter
Gemini, etc.). This package no longer carries an OpenRouter dep or
manages an API key directly.

Hermes's vision tool is async; we bridge to sync via the existing
background-loop singleton in :mod:`_background_loop` so the recorder
(a sync hook) can call us transparently.

We keep the two-section DESCRIPTION / TEXT prompt that the previous
implementation used: the recorder needs OCR'd text separated from
freeform description so the section it writes to the vault stays
human-readable and structured.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import tempfile
from collections.abc import Callable
from typing import Any

from hermes_chat_recorder.types import DescribeResult

logger = logging.getLogger(__name__)


DEFAULT_PROMPT = """You will receive an image. Produce TWO sections.

DESCRIPTION:
Write 1-3 short sentences describing what is in the image: subject, setting, notable details. Avoid speculation about who people are.

TEXT:
Transcribe any literal text visible in the image, preserving line breaks. If no text appears in the image, write "(none)"."""


# Signature: (image_path_or_url: str, user_prompt: str) -> JSON string or awaitable
VisionFn = Callable[[str, str], Any]


class ImageDescriberError(Exception):
    """Raised when describing fails terminally.

    Callers catch this and mark the section ``stage:describe_failed``.
    Don't let it propagate to the gateway dispatch loop.
    """


def _default_vision_fn(image_path_or_url: str, user_prompt: str):
    """Lazy import of Hermes's vision entrypoint.

    Imported inside the function so the package can be loaded outside
    Hermes (tests pass a fake and never trigger this).
    """
    from tools.vision_tools import vision_analyze_tool  # type: ignore[import-not-found]

    return vision_analyze_tool(image_path_or_url, user_prompt)


class ImageDescriber:
    """Describe an image via Hermes vision, return parsed DESCRIPTION + TEXT."""

    def __init__(
        self,
        *,
        prompt: str = DEFAULT_PROMPT,
        vision_fn: VisionFn | None = None,
        run_async: Callable[[Any], Any] | None = None,
    ) -> None:
        """Construct.

        Pass ``vision_fn`` to inject a fake in tests. Pass ``run_async``
        to override the sync↔async bridge (defaults to the background
        loop singleton in :mod:`_background_loop`). Production code
        should let both default.
        """
        self._prompt = prompt
        self._vision_fn: VisionFn = vision_fn or _default_vision_fn
        self._run_async = run_async

    def describe(self, image_bytes: bytes, *, mime: str = "image/png") -> DescribeResult:
        """Describe an image. Returns parsed sections; raises on failure.

        Hermes's vision tool takes either a URL or a local file path.
        We spill the bytes to a tempfile so we can pass a path
        regardless of whether the homeserver gave us encrypted or
        plaintext media.
        """
        if not image_bytes:
            raise ImageDescriberError("empty image bytes")

        ext = _ext_for_mime(mime)
        fd, path = tempfile.mkstemp(suffix=ext)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(image_bytes)

            try:
                result = self._vision_fn(path, self._prompt)
            except Exception as exc:
                raise ImageDescriberError(f"hermes vision raised: {exc}") from exc

            if inspect.isawaitable(result):
                result = self._await(result)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        return _parse_vision_response(result)

    def _await(self, awaitable: Any) -> Any:
        if self._run_async is not None:
            return self._run_async(awaitable)
        from hermes_chat_recorder._background_loop import get_background_loop

        return get_background_loop().run_coro_sync(awaitable)


def _ext_for_mime(mime: str) -> str:
    m = (mime or "").lower()
    if "jpeg" in m or "jpg" in m:
        return ".jpg"
    if "png" in m:
        return ".png"
    if "gif" in m:
        return ".gif"
    if "webp" in m:
        return ".webp"
    if "heic" in m or "heif" in m:
        return ".heic"
    return ".img"


def _parse_vision_response(raw: Any) -> DescribeResult:
    """Extract the analysis string from Hermes's vision response.

    ``vision_analyze_tool`` returns a JSON string of the form
    ``{"success": bool, "analysis": str}``. We tolerate both already-
    parsed dicts and raw JSON strings so tests / future Hermes
    refactors don't break us.
    """
    obj: Any = raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ImageDescriberError(
                f"vision response not valid JSON: {raw[:200]!r}"
            ) from exc

    if not isinstance(obj, dict):
        raise ImageDescriberError(
            f"unexpected vision response shape: {type(obj).__name__}"
        )

    if not obj.get("success", False):
        err = obj.get("analysis") or obj.get("error") or "vision failed"
        raise ImageDescriberError(str(err))

    analysis = obj.get("analysis", "")
    if not isinstance(analysis, str):
        analysis = str(analysis)
    return parse_description(analysis)


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

    desc_idx = lower.find("description:")
    text_idx = lower.find("text:")

    description = ""
    text = ""

    if desc_idx == -1 and text_idx == -1:
        description = raw.strip()
        return DescribeResult(description=description, text="", raw=raw)

    if desc_idx != -1:
        desc_start = desc_idx + len("description:")
        if text_idx != -1 and text_idx > desc_idx:
            description = raw[desc_start:text_idx].strip()
            text = raw[text_idx + len("text:") :].strip()
        else:
            description = raw[desc_start:].strip()

    if text_idx != -1 and desc_idx == -1:
        text = raw[text_idx + len("text:") :].strip()

    if text == "(none)":
        text = ""

    return DescribeResult(description=description, text=text, raw=raw)
