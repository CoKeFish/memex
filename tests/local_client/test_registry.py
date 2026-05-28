from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from memex_local_client.discovery import discover_plugins
from memex_local_client.registry import (
    RegistryError,
    attach_source_id,
    disable,
    enable,
    install_plugin,
    list_views,
    uninstall_plugin,
)
from memex_local_client.state import State

_VALID = """
from collections.abc import Mapping
from typing import Any

name = "p1"
version = "0.1.0"
source_type = "fake"
default_schedule = "PT1M"

def build_source(local_config: Mapping[str, Any]):
    class _S:
        type = "fake"
        def fetch(self, checkpoint): return iter(())
        def advance_checkpoint(self, checkpoint, last): return {}
    return _S()

def validate_requirements(local_config: Mapping[str, Any]): return []
"""


def test_install_copies_files(local_home: Path, tmp_path: Path) -> None:
    src = tmp_path / "p1"
    src.mkdir()
    (src / "__init__.py").write_text(_VALID, encoding="utf-8")
    name = install_plugin(src)
    assert name == "p1"
    assert (local_home / "plugins" / "p1" / "__init__.py").exists()


def test_install_overwrites(local_home: Path, tmp_path: Path) -> None:
    src = tmp_path / "p1"
    src.mkdir()
    (src / "__init__.py").write_text(_VALID, encoding="utf-8")
    install_plugin(src)
    # cambiar contenido y reinstalar
    (src / "__init__.py").write_text(_VALID.replace("0.1.0", "0.2.0"), encoding="utf-8")
    install_plugin(src)
    body = (local_home / "plugins" / "p1" / "__init__.py").read_text(encoding="utf-8")
    assert "0.2.0" in body


def test_enable_marks_in_state(local_home: Path, plugin_dir_factory: Any) -> None:
    plugin_dir_factory.make("p1", _VALID)
    disc = discover_plugins(plugin_dir_factory.root)
    with State(":memory:") as state:
        enable("p1", state, disc.plugins)
        row = state.get_plugin("p1")
        assert row is not None
        assert row.enabled is True
        assert row.schedule == "PT1M"


def test_enable_unknown_plugin_raises(plugin_dir_factory: Any) -> None:
    disc = discover_plugins(plugin_dir_factory.root)
    with State(":memory:") as state, pytest.raises(RegistryError):
        enable("not-there", state, disc.plugins)


def test_disable(local_home: Path, plugin_dir_factory: Any) -> None:
    plugin_dir_factory.make("p1", _VALID)
    disc = discover_plugins(plugin_dir_factory.root)
    with State(":memory:") as state:
        enable("p1", state, disc.plugins)
        disable("p1", state)
        row = state.get_plugin("p1")
        assert row is not None
        assert row.enabled is False


def test_list_views_combines_fs_and_state(local_home: Path, plugin_dir_factory: Any) -> None:
    plugin_dir_factory.make("p1", _VALID)
    plugin_dir_factory.make("p2", _VALID.replace('"p1"', '"p2"'))
    disc = discover_plugins(plugin_dir_factory.root)
    with State(":memory:") as state:
        enable("p1", state, disc.plugins)
        views = list_views(state, plugins_root=plugin_dir_factory.root)
        names = {v.name: v for v in views}
        assert names["p1"].enabled is True
        assert names["p1"].installed is True
        assert names["p2"].enabled is False
        assert names["p2"].installed is True


def test_uninstall_removes_fs_and_state(local_home: Path, plugin_dir_factory: Any) -> None:
    plugin_dir_factory.make("p1", _VALID)
    disc = discover_plugins(plugin_dir_factory.root)
    with State(":memory:") as state:
        enable("p1", state, disc.plugins)
        removed = uninstall_plugin("p1", dest_root=plugin_dir_factory.root, state=state)
        assert removed is True
        assert not (plugin_dir_factory.root / "p1").exists()
        assert state.get_plugin("p1") is None


def test_attach_source_id_persists(plugin_dir_factory: Any) -> None:
    plugin_dir_factory.make("p1", _VALID)
    disc = discover_plugins(plugin_dir_factory.root)
    with State(":memory:") as state:
        enable("p1", state, disc.plugins)
        attach_source_id(state, "p1", 99)
        assert state.get_plugin("p1").source_id == 99  # type: ignore[union-attr]
