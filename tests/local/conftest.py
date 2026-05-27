"""Fixtures locales — apuntan MEMEX_LOCAL_HOME a un tmp_path por test."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def local_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("MEMEX_LOCAL_HOME", str(tmp_path))
    yield tmp_path


@pytest.fixture
def plugin_dir_factory(local_home: Path) -> _PluginFactory:
    return _PluginFactory(local_home / "plugins")


class _PluginFactory:
    """Crea plugins fake en disco para tests de discovery/scheduler/run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def make(self, name: str, body: str) -> Path:
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").write_text(body, encoding="utf-8")
        return d
