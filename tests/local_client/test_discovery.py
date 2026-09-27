from __future__ import annotations

from typing import Any

from memex_local_client.discovery import discover_plugins

_VALID_PLUGIN = '''
"""fake plugin"""
from collections.abc import Mapping
from typing import Any

name = "fake"
version = "0.1.0"
source_type = "fake"
default_schedule = "PT1M"

def build_source(local_config: Mapping[str, Any]):
    class _S:
        type = "fake"
        def fetch(self, checkpoint): return iter(())
        def advance_checkpoint(self, checkpoint, last): return {}
    return _S()

def validate_requirements(local_config: Mapping[str, Any]):
    return []
'''

_BROKEN_PLUGIN_IMPORT = "raise RuntimeError('boom on import')"

_INCOMPLETE_PLUGIN = """
name = "incomplete"
# faltan version, source_type, default_schedule, build_source, validate_requirements
"""

_MISMATCHED_NAME = """
from collections.abc import Mapping
from typing import Any

name = "different-name"
version = "0.1.0"
source_type = "fake"
default_schedule = "PT1M"

def build_source(local_config): return None
def validate_requirements(local_config): return []
"""


def test_discovery_loads_valid_plugin(plugin_dir_factory: Any) -> None:
    plugin_dir_factory.make("fake", _VALID_PLUGIN)
    result = discover_plugins(plugin_dir_factory.root)
    assert "fake" in result.plugins
    assert result.errors == []
    p = result.plugins["fake"]
    assert p.name == "fake"
    assert p.default_schedule == "PT1M"


def test_discovery_reports_import_failure(plugin_dir_factory: Any) -> None:
    plugin_dir_factory.make("explodes", _BROKEN_PLUGIN_IMPORT)
    result = discover_plugins(plugin_dir_factory.root)
    assert "explodes" not in result.plugins
    assert len(result.errors) == 1
    assert "boom on import" in result.errors[0].reason


def test_discovery_rejects_incomplete_protocol(plugin_dir_factory: Any) -> None:
    plugin_dir_factory.make("incomplete", _INCOMPLETE_PLUGIN)
    result = discover_plugins(plugin_dir_factory.root)
    assert "incomplete" not in result.plugins
    assert any("LocalPlugin protocol" in e.reason for e in result.errors)


def test_discovery_rejects_name_mismatch(plugin_dir_factory: Any) -> None:
    plugin_dir_factory.make("mismatch", _MISMATCHED_NAME)
    result = discover_plugins(plugin_dir_factory.root)
    assert "mismatch" not in result.plugins
    assert any("does not match directory name" in e.reason for e in result.errors)


def test_discovery_skips_missing_init(plugin_dir_factory: Any) -> None:
    (plugin_dir_factory.root / "no-init").mkdir()
    result = discover_plugins(plugin_dir_factory.root)
    assert any("missing __init__.py" in e.reason for e in result.errors)


def test_discovery_one_broken_doesnt_block_valid(plugin_dir_factory: Any) -> None:
    plugin_dir_factory.make("fake", _VALID_PLUGIN)
    plugin_dir_factory.make("explodes", _BROKEN_PLUGIN_IMPORT)
    result = discover_plugins(plugin_dir_factory.root)
    assert "fake" in result.plugins
    assert len(result.errors) == 1
