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
    assert cfg.record_outbound is True


def test_block_values_override_defaults() -> None:
    cfg = load_config(
        {
            "enabled": True,
            "vault_root": "/tmp/vault",
            "record_outbound": False,
            "timezone": "Europe/London",
        },
        env={},
    )
    assert cfg.vault_root == Path("/tmp/vault")
    assert cfg.record_outbound is False
    assert cfg.timezone == "Europe/London"


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
    """Media-byte preservation isn't implemented — reject the flag
    explicitly so users don't think it works."""
    with pytest.raises(ConfigError, match="not implemented"):
        load_config({"vault_root": "/x", field: True}, env={})


@pytest.mark.parametrize(
    "deprecated",
    [
        "image_describer_model",
        "whisper_model_size",
        "openrouter_api_key",
        "prewarm_whisper",
        "pending_voice_ttl_seconds",
    ],
)
def test_deprecated_fields_are_accepted_silently(deprecated: str) -> None:
    """v0.3 delegates STT/vision to Hermes. We accept the old keys (they
    might still be in users' config.yaml from v0.2 and earlier) but
    they have no effect — the load must NOT raise."""
    cfg = load_config({"vault_root": "/x", deprecated: "whatever"}, env={})
    assert cfg.vault_root == Path("/x")
    # Preserved in raw_block for diagnostics, just not surfaced as an
    # attribute on the dataclass.
    assert deprecated in cfg.raw_block


def test_env_override_for_timezone() -> None:
    cfg = load_config(
        {"timezone": "America/Los_Angeles"},
        env={"TRANSCRIPT_TZ": "UTC"},
    )
    assert cfg.timezone == "UTC"


def test_empty_env_does_not_override() -> None:
    """Env vars set to empty strings should NOT override config values."""
    cfg = load_config(
        {"timezone": "America/Los_Angeles"},
        env={"TRANSCRIPT_TZ": ""},
    )
    assert cfg.timezone == "America/Los_Angeles"


def test_non_mapping_block_raises() -> None:
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config("not a dict", env={})  # type: ignore[arg-type]


def test_bad_vault_root_type_raises() -> None:
    with pytest.raises(ConfigError, match="vault_root"):
        load_config({"vault_root": 123}, env={})


def test_empty_vault_root_string_raises() -> None:
    with pytest.raises(ConfigError, match="vault_root"):
        load_config({"vault_root": "  "}, env={})


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


# ---------------------------------------------------------------------------
# Manual name overrides
# ---------------------------------------------------------------------------


def test_no_name_overrides_means_empty_dicts() -> None:
    cfg = load_config({"vault_root": "/x"}, env={})
    assert cfg.room_overrides == {}
    assert cfg.user_overrides == {}


def test_name_overrides_parsed() -> None:
    cfg = load_config(
        {
            "vault_root": "/x",
            "name_overrides": {
                "rooms": {"!abc:srv": "Family"},
                "users": {"@signal_xyz:srv": "Matt"},
            },
        },
        env={},
    )
    assert cfg.room_overrides == {"!abc:srv": "Family"}
    assert cfg.user_overrides == {"@signal_xyz:srv": "Matt"}


def test_name_overrides_strips_whitespace() -> None:
    cfg = load_config(
        {
            "vault_root": "/x",
            "name_overrides": {"rooms": {"  !abc:srv  ": "  Family  "}},
        },
        env={},
    )
    assert cfg.room_overrides == {"!abc:srv": "Family"}


def test_name_overrides_partial_section_ok() -> None:
    """Either rooms or users alone should work — not both required."""
    cfg = load_config(
        {"vault_root": "/x", "name_overrides": {"rooms": {"!abc:srv": "Family"}}},
        env={},
    )
    assert cfg.user_overrides == {}


def test_name_overrides_rejects_non_mapping() -> None:
    with pytest.raises(ConfigError, match="name_overrides must be a mapping"):
        load_config({"vault_root": "/x", "name_overrides": "nope"}, env={})


def test_name_overrides_rejects_non_mapping_rooms() -> None:
    with pytest.raises(ConfigError, match="name_overrides.rooms must be a mapping"):
        load_config(
            {"vault_root": "/x", "name_overrides": {"rooms": ["not", "a", "dict"]}},
            env={},
        )


def test_name_overrides_rejects_blank_value() -> None:
    with pytest.raises(ConfigError, match="must be non-empty strings"):
        load_config(
            {
                "vault_root": "/x",
                "name_overrides": {"rooms": {"!abc:srv": ""}},
            },
            env={},
        )


def test_name_overrides_rejects_non_string_value() -> None:
    with pytest.raises(ConfigError, match="must be non-empty strings"):
        load_config(
            {
                "vault_root": "/x",
                "name_overrides": {"users": {"@matt:srv": 12345}},
            },
            env={},
        )
