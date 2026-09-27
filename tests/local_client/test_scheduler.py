from __future__ import annotations

from typing import Any

import pytest

from memex.ingestors.runner import RunStats
from memex_local_client.discovery import discover_plugins
from memex_local_client.registry import enable
from memex_local_client.scheduler import Scheduler, _build_runtimes, parse_duration
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
        gateway_url="http://localhost:8787",
        api_token=None,
        plugins_root=plugins_root,
    )


def test_run_once_dispatches_enabled_plugin(
    plugin_dir_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_dir_factory.make("p1", _VALID)
    state = State(":memory:")
    disc = discover_plugins(plugin_dir_factory.root)
    enable("p1", state, disc.plugins)
    sched = _make_scheduler(state, plugin_dir_factory.root)

    calls: list[str] = []

    def fake_execute(plugin: Any, **kwargs: Any) -> RunStats:
        # Verifica que el scheduler pasa los args correctos.
        assert kwargs["gateway_url"] == "http://localhost:8787"
        assert kwargs["api_token"] is None
        assert kwargs["state"] is state
        calls.append(plugin.name)
        return RunStats(posted=0, inserted=0, duplicates=0, errors=0, ms_elapsed=1)

    monkeypatch.setattr("memex_local_client.scheduler.execute_plugin", fake_execute)
    sched.run_once()
    assert calls == ["p1"]


def test_run_once_skips_disabled_plugins(
    plugin_dir_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_dir_factory.make("p1", _VALID)
    state = State(":memory:")  # sin enable
    sched = _make_scheduler(state, plugin_dir_factory.root)

    calls: list[str] = []

    def record(plugin: Any, **kw: Any) -> RunStats:
        calls.append(plugin.name)
        return RunStats(0, 0, 0, 0, 1)

    monkeypatch.setattr("memex_local_client.scheduler.execute_plugin", record)
    sched.run_once()
    assert calls == []


def test_plugin_failure_triggers_backoff(
    plugin_dir_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_dir_factory.make("p1", _VALID)
    state = State(":memory:")
    disc = discover_plugins(plugin_dir_factory.root)
    enable("p1", state, disc.plugins)
    sched = _make_scheduler(state, plugin_dir_factory.root)

    def explode(plugin: Any, **kwargs: Any) -> RunStats:
        raise RuntimeError("boom")

    monkeypatch.setattr("memex_local_client.scheduler.execute_plugin", explode)
    # Si no absorbe el error, el run_once levanta.
    sched.run_once()


def test_run_once_handles_bad_schedule_gracefully(
    plugin_dir_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_dir_factory.make("p1", _VALID)
    state = State(":memory:")
    disc = discover_plugins(plugin_dir_factory.root)
    enable("p1", state, disc.plugins)
    state.upsert_plugin("p1", schedule="not-a-duration")
    sched = _make_scheduler(state, plugin_dir_factory.root)

    calls: list[str] = []

    def record(plugin: Any, **kw: Any) -> RunStats:
        calls.append(plugin.name)
        return RunStats(0, 0, 0, 0, 1)

    monkeypatch.setattr("memex_local_client.scheduler.execute_plugin", record)
    sched.run_once()
    assert calls == []


def test_build_runtimes_preserves_timing_with_prev(plugin_dir_factory: Any) -> None:
    """`_build_runtimes(prev=...)` conserva el `next_run_at` ya agendado en vez de resetearlo
    a `now` — el núcleo del fix anti-martilleo (reusa el mismo runtime entre ticks)."""
    plugin_dir_factory.make("p1", _VALID)
    state = State(":memory:")
    disc = discover_plugins(plugin_dir_factory.root)
    enable("p1", state, disc.plugins)

    first = _build_runtimes(disc.plugins, state, 1000.0)
    assert first["p1"].next_run_at == 1000.0
    first["p1"].next_run_at = 1060.0  # como si `_tick` lo hubiera avanzado +PT1M

    again = _build_runtimes(disc.plugins, state, 1001.0, prev=first)
    assert again["p1"] is first["p1"]  # mismo runtime reusado
    assert again["p1"].next_run_at == 1060.0  # timing preservado, no reseteado a 1001


def test_run_forever_respects_interval(
    plugin_dir_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regresión del martilleo: `run_forever` no re-dispara en cada tick.

    Con `PT1M` (60s) y ~10 ticks de 1s simulados solo ocurre la corrida inicial. Antes del fix,
    `run_forever` reconstruía los runtimes en cada tick (vía `run_once`) reseteando
    `next_run_at=now` y disparaba en cada tick.
    """
    plugin_dir_factory.make("p1", _VALID)  # default_schedule = PT1M
    state = State(":memory:")
    disc = discover_plugins(plugin_dir_factory.root)
    enable("p1", state, disc.plugins)
    sched = _make_scheduler(state, plugin_dir_factory.root)

    calls: list[str] = []

    def rec(plugin: Any, **kw: Any) -> RunStats:
        calls.append(plugin.name)
        return RunStats(0, 0, 0, 0, 1)

    monkeypatch.setattr("memex_local_client.scheduler.execute_plugin", rec)

    # Reloj simulado: cada tick (en `_stop.wait`) avanza 1s; paramos tras 10 ticks.
    clock = {"t": 1000.0}
    monkeypatch.setattr("memex_local_client.scheduler.time.monotonic", lambda: clock["t"])

    ticks = {"n": 0}

    def fake_wait(timeout: float | None = None) -> bool:
        ticks["n"] += 1
        clock["t"] += 1.0
        if ticks["n"] >= 10:
            sched.request_stop()
        return sched._stop.is_set()

    monkeypatch.setattr(sched._stop, "wait", fake_wait)

    sched.run_forever()

    # 9s transcurridos < 60s (PT1M): solo la corrida inicial, sin martilleo.
    assert calls == ["p1"]
