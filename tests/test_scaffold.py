"""Smoke tests covering the scaffolded package shape.

After P3 these verify the package surface is sound — the deep behaviour
checks live in the module-specific test files.
"""

from __future__ import annotations


def test_package_imports() -> None:
    import hermes_chat_recorder

    assert hasattr(hermes_chat_recorder, "register")
    assert callable(hermes_chat_recorder.register)


def test_version_present_and_string() -> None:
    import hermes_chat_recorder

    assert isinstance(hermes_chat_recorder.__version__, str)
    assert hermes_chat_recorder.__version__.count(".") == 2


def test_register_callable_with_minimal_ctx(tmp_path) -> None:
    """register() should accept a duck-typed ctx and return a Recorder."""
    from types import SimpleNamespace

    import hermes_chat_recorder

    hooks_bound: list[tuple[str, object]] = []

    ctx = SimpleNamespace(
        config={
            "plugins": {
                "chat_recorder": {
                    "enabled": True,
                    "vault_root": str(tmp_path),
                }
            }
        },
        register_hook=lambda name, cb: hooks_bound.append((name, cb)),
    )

    result = hermes_chat_recorder.register(ctx)
    assert result is not None
    assert {name for name, _ in hooks_bound} == {
        "pre_gateway_dispatch",
        "on_session_start",
    }
