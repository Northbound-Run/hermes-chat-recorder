"""Configuration loading and validation for the recorder.

The Hermes config block lives under ``plugins.chat_recorder`` in
``config.yaml``. Env vars override matching fields when set. See
``docs/DESIGN.md §7`` for the full schema.
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
    nicknames: tuple[str, ...] = ()
    record_outbound: bool = True
    record_image_bytes: bool = False
    record_audio_bytes: bool = False
    timezone: str = "America/Los_Angeles"
    image_describer_model: str = "google/gemini-3-flash-preview"
    whisper_model_size: str = "base"
    transcribe_failure_visible: bool = True
    pending_voice_ttl_seconds: int = 300

    # Resolved at load time from env. Empty string == not set.
    openrouter_api_key: str = ""

    # Stored for diagnostics + the readiness check.
    raw_block: dict[str, Any] = field(default_factory=dict, compare=False)


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
        config. ``None`` is treated as an empty mapping — useful when
        the user hasn't added a config block yet (the plugin will load
        with defaults; vault_root is required so we'll raise if it's
        unset AND no default applies).
    env:
        Environment overrides. Defaults to ``os.environ`` when None.
        Listed override keys:

        * ``OPENROUTER_API_KEY``
        * ``IMAGE_DESCRIBER_MODEL``
        * ``WHISPER_MODEL_SIZE``
        * ``TRANSCRIPT_TZ``

    Raises
    ------
    ConfigError
        If a required field has an invalid type, or ``vault_root`` is
        absent and no default applies, or ``timezone`` is unparseable.
    """
    block = plugin_block or {}
    if not isinstance(block, dict):
        raise ConfigError(f"plugins.chat_recorder must be a mapping, got {type(block).__name__}")

    env_map = env if env is not None else dict(os.environ)

    def _opt(key: str, default: Any) -> Any:
        val = block.get(key, default)
        return val if val is not None else default

    enabled = bool(_opt("enabled", True))

    vault_root_raw = _opt("vault_root", "/data/vault/transcripts")
    if not isinstance(vault_root_raw, (str, Path)):
        raise ConfigError(
            f"vault_root must be a string path, got {type(vault_root_raw).__name__}"
        )
    vault_root = Path(vault_root_raw)
    if not str(vault_root).strip():
        raise ConfigError("vault_root is required and cannot be empty")

    nicknames_raw = _opt("nicknames", [])
    if not isinstance(nicknames_raw, (list, tuple)):
        raise ConfigError(
            f"nicknames must be a list of strings, got {type(nicknames_raw).__name__}"
        )
    nicknames = tuple(
        str(n).strip() for n in nicknames_raw if isinstance(n, str) and str(n).strip()
    )

    record_outbound = bool(_opt("record_outbound", True))
    record_image_bytes = bool(_opt("record_image_bytes", False))
    record_audio_bytes = bool(_opt("record_audio_bytes", False))

    # timezone: env override wins; raw value validated downstream by ZoneInfo
    tz = env_map.get("TRANSCRIPT_TZ") or str(_opt("timezone", "America/Los_Angeles"))
    if not tz:
        raise ConfigError("timezone cannot be empty")
    # We do NOT validate via ZoneInfo here to keep the config layer
    # pure-stdlib-agnostic. VaultWriter.__init__ raises if the zone is
    # unknown — that's the loud-fail-on-startup path.

    image_describer_model = (
        env_map.get("IMAGE_DESCRIBER_MODEL")
        or str(_opt("image_describer_model", "google/gemini-3-flash-preview"))
    )

    whisper_model_size = (
        env_map.get("WHISPER_MODEL_SIZE")
        or str(_opt("whisper_model_size", "base"))
    )

    transcribe_failure_visible = bool(_opt("transcribe_failure_visible", True))

    pending_voice_ttl_seconds_raw = _opt("pending_voice_ttl_seconds", 300)
    try:
        pending_voice_ttl_seconds = int(pending_voice_ttl_seconds_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"pending_voice_ttl_seconds must be an integer, got {pending_voice_ttl_seconds_raw!r}"
        ) from exc
    if pending_voice_ttl_seconds < 0:
        raise ConfigError("pending_voice_ttl_seconds must be non-negative")

    openrouter_api_key = env_map.get("OPENROUTER_API_KEY", "") or ""

    return RecorderConfig(
        enabled=enabled,
        vault_root=vault_root,
        nicknames=nicknames,
        record_outbound=record_outbound,
        record_image_bytes=record_image_bytes,
        record_audio_bytes=record_audio_bytes,
        timezone=tz,
        image_describer_model=image_describer_model,
        whisper_model_size=whisper_model_size,
        transcribe_failure_visible=transcribe_failure_visible,
        pending_voice_ttl_seconds=pending_voice_ttl_seconds,
        openrouter_api_key=openrouter_api_key,
        raw_block=block,
    )
