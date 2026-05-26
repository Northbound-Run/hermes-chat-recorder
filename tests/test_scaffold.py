"""Smoke tests covering the scaffolded package shape.

These exist so the test suite has something green to run before P2 lands.
They verify the package is importable, the public surface (``register``,
``__version__``) exists, and the stub fails with a useful message instead
of a confusing AttributeError.
"""

from __future__ import annotations

import pytest


def test_package_imports() -> None:
    import hermes_chat_recorder

    assert hasattr(hermes_chat_recorder, "register")
    assert callable(hermes_chat_recorder.register)


def test_version_present_and_string() -> None:
    import hermes_chat_recorder

    assert isinstance(hermes_chat_recorder.__version__, str)
    assert hermes_chat_recorder.__version__.count(".") == 2


def test_register_is_not_yet_implemented() -> None:
    """The scaffold ships a stub. Real wiring lands in P2."""
    import hermes_chat_recorder

    class _DummyCtx:
        pass

    with pytest.raises(NotImplementedError):
        hermes_chat_recorder.register(_DummyCtx())
