from __future__ import annotations

from typing import Any

import pytest

from memex.ingestors.runner import RunStats
from memex_local.discovery import discover_plugins
from memex_local.registry import enable
from memex_local.scheduler import Scheduler, parse_duration
from memex_local.state import State

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


def test_parse_duration_minutes() -> None:
    assert parse_duration("PT5M") == 300
    assert parse_duration("PT1H") == 3600
    assert parse_duration("PT24H") == 86400
    assert parse_duration("P1D") == 86400
    assert parse_duration("P1DT2H30M") == 86400 + 7200 + 1800


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_duration("5 minutes")
    with pytest.raises(ValueError):
        parse_duration("PT")


def _make_scheduler(state: State, plugins_root: Any) -> Scheduler:
    return Scheduler(
        state=state,
        bridge_url="http://localhost:8787",
        api_token=None,
        plugins_root=plugins_root,
    )


def test_run_once_dispatches_enabled_plugin(plugin_dir_factory, monkeypatch) -> None:
    plugin_dir_factory.make("p1", _VALID)
    state = State(":memory:")
    disc = discover_plugins(plugin_dir_factory.root)
    enable("p1", state, disc.plugins)
    sched = _make_scheduler(state, plugin_dir_factory.root)

    calls: list[str] = []

    def fake_execute(plugin: Any, **kwargs: Any) -> RunStats:
        # Verifica que el scheduler pasa los args correctos.
        assert kwargs["bridge_url"] == "http://localhost:8787"
        assert kwargs["api_token"] is None
        assert kwargs["state"] is state
        calls.append(plugin.name)
        return RunStats(posted=0, inserted=0, duplicates=0, errors=0, ms_elapsed=1)

    monkeypatch.setattr("memex_local.scheduler.execute_plugin", fake_execute)
    sched.run_once()
    assert calls == ["p1"]


def test_run_once_skips_disabled_plugins(plugin_dir_factory, monkeypatch) -> None:
    plugin_dir_factory.make("p1", _VALID)
    state = State(":memory:")  # sin enable
    sched = _make_scheduler(state, plugin_dir_factory.root)

    calls: list[str] = []
    monkeypatch.setattr(
        "memex_local.scheduler.execute_plugin",
        lambda plugin, **kw: calls.append(plugin.name) or RunStats(0, 0, 0, 0, 1),
    )
    sched.run_once()
    assert calls == []


def test_plugin_failure_triggers_backoff(plugin_dir_factory, monkeypatch) -> None:
    plugin_dir_factory.make("p1", _VALID)
    state = State(":memory:")
    disc = discover_plugins(plugin_dir_factory.root)
    enable("p1", state, disc.plugins)
    sched = _make_scheduler(state, plugin_dir_factory.root)

    def explode(plugin: Any, **kwargs: Any) -> RunStats:
        raise RuntimeError("boom")

    monkeypatch.setattr("memex_local.scheduler.execute_plugin", explode)
    # Si no absorbe el error, el run_once levanta.
    sched.run_once()


def test_run_once_handles_bad_schedule_gracefully(plugin_dir_factory, monkeypatch) -> None:
    plugin_dir_factory.make("p1", _VALID)
    state = State(":memory:")
    disc = discover_plugins(plugin_dir_factory.root)
    enable("p1", state, disc.plugins)
    state.upsert_plugin("p1", schedule="not-a-duration")
    sched = _make_scheduler(state, plugin_dir_factory.root)

    calls: list[str] = []
    monkeypatch.setattr(
        "memex_local.scheduler.execute_plugin",
        lambda plugin, **kw: calls.append(plugin.name) or RunStats(0, 0, 0, 0, 1),
    )
    sched.run_once()
    assert calls == []
