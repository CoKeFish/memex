"""memex.version — sello de versión del código (env → git → unknown)."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest

from memex import version as v


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    v.code_version.cache_clear()
    yield
    v.code_version.cache_clear()


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(v.ENV_VAR, " abc1234 ")
    assert v.code_version() == "abc1234"


def test_unknown_without_env_or_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(v.ENV_VAR, raising=False)
    monkeypatch.setattr(v, "_git_version", lambda: None)
    assert v.code_version() == v.UNKNOWN


def test_git_version_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """En el repo: sha corto (+ `-dirty`); fuera de un repo: None. Nunca revienta."""
    monkeypatch.delenv(v.ENV_VAR, raising=False)
    g = v._git_version()
    assert g is None or re.fullmatch(r"[0-9a-f]{7,12}(-dirty)?", g), g
    assert v.code_version() == (g or v.UNKNOWN)
