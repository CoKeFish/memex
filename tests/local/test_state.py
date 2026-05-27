from __future__ import annotations

import pytest

from memex_local.state import State


@pytest.fixture
def state() -> State:
    return State(":memory:")


def test_upsert_and_get_plugin(state: State) -> None:
    state.upsert_plugin("p1", version="0.1", schedule="PT5M", source_id=42)
    row = state.get_plugin("p1")
    assert row is not None
    assert row.name == "p1"
    assert row.version == "0.1"
    assert row.schedule == "PT5M"
    assert row.source_id == 42
    assert row.enabled is False


def test_set_enabled_disable(state: State) -> None:
    state.upsert_plugin("p1")
    assert state.set_enabled("p1", True) is True
    assert state.get_plugin("p1").enabled is True  # type: ignore[union-attr]
    assert state.set_enabled("p1", False) is True
    assert state.get_plugin("p1").enabled is False  # type: ignore[union-attr]


def test_set_enabled_unknown_returns_false(state: State) -> None:
    assert state.set_enabled("does-not-exist", True) is False


def test_upsert_preserves_existing_fields(state: State) -> None:
    state.upsert_plugin("p1", version="0.1", schedule="PT5M", source_id=42)
    # nuevo upsert sin source_id: no debería borrarlo
    state.upsert_plugin("p1", version="0.2")
    row = state.get_plugin("p1")
    assert row is not None
    assert row.version == "0.2"
    assert row.source_id == 42  # preservado


def test_list_enabled_only_returns_enabled(state: State) -> None:
    state.upsert_plugin("a")
    state.upsert_plugin("b")
    state.set_enabled("a", True)
    names = [p.name for p in state.list_enabled()]
    assert names == ["a"]


def test_remove_plugin(state: State) -> None:
    state.upsert_plugin("p1")
    assert state.remove_plugin("p1") is True
    assert state.get_plugin("p1") is None
    assert state.remove_plugin("p1") is False


def test_start_run_and_finalize_ok(state: State) -> None:
    state.upsert_plugin("p1")
    with state.start_run("p1") as run_id:
        state.finalize_run(run_id, status="ok", posted=3, inserted=2, duplicates=1)
    runs = state.recent_runs("p1")
    assert len(runs) == 1
    r = runs[0]
    assert r.status == "ok"
    assert r.inserted == 2
    assert r.duplicates == 1
    assert r.finished_at is not None


def test_start_run_marks_error_on_exception(state: State) -> None:
    state.upsert_plugin("p1")
    with pytest.raises(RuntimeError), state.start_run("p1") as _run_id:
        raise RuntimeError("boom")
    runs = state.recent_runs("p1")
    assert runs[0].status == "error"
    assert "boom" in (runs[0].error_msg or "")


def test_mark_seen_updates_timestamp(state: State) -> None:
    state.upsert_plugin("p1")
    assert state.get_plugin("p1").last_seen_at is None  # type: ignore[union-attr]
    state.mark_seen("p1")
    assert state.get_plugin("p1").last_seen_at is not None  # type: ignore[union-attr]
