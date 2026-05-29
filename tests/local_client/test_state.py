from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memex_local_client.state import State


@pytest.fixture
def state() -> State:
    return State(":memory:")


def test_ensure_columns_adds_filtered_to_legacy_db(tmp_path: Path) -> None:
    """Una DB creada antes de la columna `filtered` la gana al abrir State.

    `CREATE TABLE IF NOT EXISTS` no altera tablas existentes, así que sin el
    ALTER idempotente de `_ensure_columns` una DB vieja rompería al leer/escribir
    `filtered`. Acá simulamos el schema viejo y verificamos que se migra solo.
    """
    db = tmp_path / "legacy.db"
    legacy = sqlite3.connect(str(db))
    legacy.execute(
        """
        CREATE TABLE runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin_name  TEXT NOT NULL,
            started_at   TEXT NOT NULL,
            finished_at  TEXT,
            status       TEXT NOT NULL,
            posted       INTEGER NOT NULL DEFAULT 0,
            inserted     INTEGER NOT NULL DEFAULT 0,
            duplicates   INTEGER NOT NULL DEFAULT 0,
            errors       INTEGER NOT NULL DEFAULT 0,
            error_msg    TEXT
        )
        """
    )
    legacy.commit()
    legacy.close()

    with State(db) as state:
        state.upsert_plugin("p1")
        with state.start_run("p1") as run_id:
            state.finalize_run(run_id, status="ok", filtered=3)
        # La fila vieja se lee sin romper y la columna nueva quedó funcional.
        assert state.recent_runs("p1")[0].filtered == 3


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
        state.finalize_run(run_id, status="ok", posted=3, inserted=2, duplicates=1, filtered=1)
    runs = state.recent_runs("p1")
    assert len(runs) == 1
    r = runs[0]
    assert r.status == "ok"
    assert r.inserted == 2
    assert r.duplicates == 1
    assert r.filtered == 1
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
