"""Configuration loading and validation for the recorder.

The Hermes config block lives under ``plugins.chat_recorder`` in
``config.yaml``. Env vars override matching fields when set. STT and
vision are now delegated to Hermes's built-in services — this package
no longer carries model/provider/credential settings of its own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RecorderConfig:
    """Validated, typed config for :class:`Recorder`."""

    enabled: bool = True
    vault_root: Path = Path("/data/vault/transcripts")
    record_outbound: bool = True
    timezone: str = "America/Los_Angeles"

    raw_block: dict[str, Any] = field(default_factory=dict, compare=False)


_TRUTHY = {"true", "1", "yes", "on", "y", "t"}
_FALSY = {"false", "0", "no", "off", "n", "f", ""}

# Fields the config block used to accept that we now delegate to
# Hermes. We accept them silently (don't fail readiness on old configs
# carried over from v0.2 and earlier) but they have no effect.
_DEPRECATED_FIELDS = frozenset(
    {
        "image_describer_model",
        "whisper_model_size",
        "openrouter_api_key",
        "prewarm_whisper",
        "pending_voice_ttl_seconds",
    }
)


def _coerce_bool(value: Any, *, default: bool, field_name: str) -> bool:
    """Defensively coerce a YAML / env value to a bool.

    Naive ``bool(value)`` is unsafe — ``bool("false")`` returns True
    because non-empty strings are truthy. We recognize the standard
    YAML true/false vocabulary and refuse anything we don't recognize.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in _TRUTHY:
            return True
        if norm in _FALSY:
            return False
        raise ConfigError(
            f"{field_name} got unparseable boolean string {value!r}; "
            "use true/false, yes/no, or on/off"
        )
    raise ConfigError(
        f"{field_name} expected bool, got {type(value).__name__}: {value!r}"
    )


class ConfigError(ValueError):
    """Raised when the config block is malformed in a way that should
    LOUDLY fail readiness, not silently degrade."""


def load_config(
    plugin_block: dict[str, Any] | None,
    *,
    env: dict[str, str] | None = None,
) -> RecorderConfig:
    """Parse a Hermes plugin block + env overrides into a RecorderConfig.

    Parameters
    ----------
    plugin_block:
        The ``plugins.chat_recorder`` mapping from Hermes's loaded
        config. ``None`` is treated as an empty mapping.
    env:
        Environment overrides. Defaults to ``os.environ`` when None.
        Listed override keys:

        * ``TRANSCRIPT_TZ`` — overrides ``timezone``.

    Raises
    ------
    ConfigError
        If a required field has an invalid type or ``vault_root`` /
        ``timezone`` is unparseable.
    """
    block = plugin_block or {}
    if not isinstance(block, dict):
        raise ConfigError(
            f"plugins.chat_recorder must be a mapping, got {type(block).__name__}"
        )

    env_map = env if env is not None else dict(os.environ)

    def _opt(key: str, default: Any) -> Any:
        val = block.get(key, default)
        return val if val is not None else default

    enabled = _coerce_bool(_opt("enabled", True), default=True, field_name="enabled")

    vault_root_raw = _opt("vault_root", "/data/vault/transcripts")
    if not isinstance(vault_root_raw, (str, Path)):
        raise ConfigError(
            f"vault_root must be a string path, got {type(vault_root_raw).__name__}"
        )
    vault_root = Path(vault_root_raw)
    if not str(vault_root).strip():
        raise ConfigError("vault_root is required and cannot be empty")

    record_outbound = _coerce_bool(
        _opt("record_outbound", True), default=True, field_name="record_outbound"
    )

    tz = env_map.get("TRANSCRIPT_TZ") or str(_opt("timezone", "America/Los_Angeles"))
    if not tz:
        raise ConfigError("timezone cannot be empty")

    for unsupported in ("record_image_bytes", "record_audio_bytes"):
        if unsupported in block:
            raise ConfigError(
                f"{unsupported} is not implemented in this version of "
                "hermes-chat-recorder; remove it from your config."
            )

    return RecorderConfig(
        enabled=enabled,
        vault_root=vault_root,
        record_outbound=record_outbound,
        timezone=tz,
        raw_block=block,
    )
