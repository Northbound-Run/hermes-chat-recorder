"""Tests for the Hermes plugin config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_chat_recorder.config import ConfigError, load_config


def test_none_block_returns_defaults() -> None:
    cfg = load_config(None, env={})
    assert cfg.enabled is True
    assert cfg.vault_root == Path("/data/vault/transcripts")
    assert cfg.timezone == "America/Los_Angeles"
    assert cfg.image_describer_model == "google/gemini-3-flash-preview"
    assert cfg.whisper_model_size == "base"
    assert cfg.openrouter_api_key == ""


def test_block_values_override_defaults() -> None:
    cfg = load_config(
        {
            "enabled": True,
            "vault_root": "/tmp/vault",
            "record_outbound": False,
            "timezone": "Europe/London",
            "image_describer_model": "anthropic/claude-3-5-sonnet",
            "whisper_model_size": "small",
            "pending_voice_ttl_seconds": 90,
        },
        env={},
    )
    assert cfg.vault_root == Path("/tmp/vault")
    assert cfg.record_outbound is False
    assert cfg.timezone == "Europe/London"
    assert cfg.image_describer_model == "anthropic/claude-3-5-sonnet"
    assert cfg.whisper_model_size == "small"
    assert cfg.pending_voice_ttl_seconds == 90


@pytest.mark.parametrize(
    "raw,expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("True", True),
        ("YES", True),
        ("on", True),
        ("1", True),
        ("false", False),
        ("False", False),
        ("no", False),
        ("off", False),
        ("0", False),
        ("", False),
        (1, True),
        (0, False),
    ],
)
def test_bool_coercion_accepts_known_values(raw, expected: bool) -> None:
    cfg = load_config(
        {"vault_root": "/x", "record_outbound": raw}, env={}
    )
    assert cfg.record_outbound is expected


@pytest.mark.parametrize("bad", ["maybe", "tru", "nope", "2"])
def test_bool_coercion_rejects_unknown_strings(bad: str) -> None:
    with pytest.raises(ConfigError, match="boolean string"):
        load_config({"vault_root": "/x", "record_outbound": bad}, env={})


def test_bool_coercion_rejects_wrong_type() -> None:
    with pytest.raises(ConfigError, match="expected bool"):
        load_config({"vault_root": "/x", "record_outbound": ["lol"]}, env={})


@pytest.mark.parametrize("field", ["record_image_bytes", "record_audio_bytes"])
def test_unsupported_fields_loudly_rejected(field: str) -> None:
    """v0.0.1 doesn't implement media-byte preservation — reject the
    flag explicitly so users don't think it works."""
    with pytest.raises(ConfigError, match="not yet implemented"):
        load_config({"vault_root": "/x", field: True}, env={})


def test_prewarm_whisper_default_false() -> None:
    cfg = load_config({"vault_root": "/x"}, env={})
    assert cfg.prewarm_whisper is False


def test_prewarm_whisper_can_be_enabled() -> None:
    cfg = load_config({"vault_root": "/x", "prewarm_whisper": True}, env={})
    assert cfg.prewarm_whisper is True


def test_prewarm_whisper_accepts_string_true() -> None:
    cfg = load_config({"vault_root": "/x", "prewarm_whisper": "yes"}, env={})
    assert cfg.prewarm_whisper is True


def test_env_overrides_win_for_listed_keys() -> None:
    cfg = load_config(
        {
            "timezone": "America/Los_Angeles",
            "image_describer_model": "default-model",
            "whisper_model_size": "base",
        },
        env={
            "TRANSCRIPT_TZ": "UTC",
            "IMAGE_DESCRIBER_MODEL": "env-model",
            "WHISPER_MODEL_SIZE": "medium",
            "OPENROUTER_API_KEY": "sk-or-test",
        },
    )
    assert cfg.timezone == "UTC"
    assert cfg.image_describer_model == "env-model"
    assert cfg.whisper_model_size == "medium"
    assert cfg.openrouter_api_key == "sk-or-test"


def test_empty_env_does_not_override() -> None:
    """Env vars set to empty strings should NOT override config values
    (we treat empty as 'not set')."""
    cfg = load_config(
        {"timezone": "America/Los_Angeles"},
        env={"TRANSCRIPT_TZ": "", "OPENROUTER_API_KEY": ""},
    )
    assert cfg.timezone == "America/Los_Angeles"
    assert cfg.openrouter_api_key == ""


def test_non_mapping_block_raises() -> None:
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config("not a dict", env={})  # type: ignore[arg-type]


def test_bad_vault_root_type_raises() -> None:
    with pytest.raises(ConfigError, match="vault_root"):
        load_config({"vault_root": 123}, env={})


def test_empty_vault_root_string_raises() -> None:
    with pytest.raises(ConfigError, match="vault_root"):
        load_config({"vault_root": "  "}, env={})


def test_bad_pending_ttl_raises() -> None:
    with pytest.raises(ConfigError, match="pending_voice_ttl_seconds"):
        load_config({"pending_voice_ttl_seconds": "not-a-number"}, env={})


def test_negative_pending_ttl_raises() -> None:
    with pytest.raises(ConfigError, match="non-negative"):
        load_config({"pending_voice_ttl_seconds": -1}, env={})


def test_pathlib_vault_root_accepted() -> None:
    cfg = load_config({"vault_root": Path("/x")}, env={})
    assert cfg.vault_root == Path("/x")


def test_raw_block_preserved_for_diagnostics() -> None:
    block = {"enabled": True, "vault_root": "/x"}
    cfg = load_config(block, env={})
    assert cfg.raw_block == block


def test_disabled_flag() -> None:
    cfg = load_config({"enabled": False, "vault_root": "/x"}, env={})
    assert cfg.enabled is False
