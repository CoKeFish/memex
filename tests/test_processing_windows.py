"""Windowing puro del pipeline (sin DB ni LLM)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memex.processing.windows import MAX_WINDOW_SIZE, WorkRow, plan_windows

_BASE = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)


def _row(inbox_id: int, source_id: int, minutes: int, tier: str = "batch") -> WorkRow:
    return WorkRow(
        inbox_id=inbox_id,
        source_id=source_id,
        occurred_at=_BASE + timedelta(minutes=minutes),
        payload={},
        tier=tier,
    )


def test_individual_one_window_each() -> None:
    ws = plan_windows([_row(1, 5, 0, "individual"), _row(2, 5, 10, "individual")])
    assert len(ws) == 2
    assert all(len(w.rows) == 1 and w.tier == "individual" for w in ws)


def test_batch_same_source_is_one_window() -> None:
    ws = plan_windows([_row(1, 5, 0), _row(2, 5, 30), _row(3, 5, 60)])
    assert len(ws) == 1
    assert len(ws[0].rows) == 3


def test_batch_large_time_gap_does_not_split() -> None:
    # El ventaneo NO mira los timestamps: dos correos separados por DÍAS siguen en UNA ventana.
    # La cadencia temporal (cada cuánto se procesa) es del daemon/scheduler, no del agrupado.
    ws = plan_windows([_row(1, 5, 0), _row(2, 5, 7 * 24 * 60)])  # 7 días de gap
    assert len(ws) == 1
    assert len(ws[0].rows) == 2


def test_batch_count_cap_splits() -> None:
    ws = plan_windows([_row(i, 5, i) for i in range(MAX_WINDOW_SIZE + 5)])
    assert len(ws) == 2
    assert len(ws[0].rows) == MAX_WINDOW_SIZE


def test_different_sources_dont_mix() -> None:
    ws = plan_windows([_row(1, 5, 0), _row(2, 6, 1)])
    assert len(ws) == 2
    assert {w.source_id for w in ws} == {5, 6}


# ----- perilla ajustable (max_window_size) --------------------------------------- #


def test_custom_max_window_size_splits() -> None:
    ws = plan_windows([_row(i, 5, i) for i in range(3)], max_window_size=2)
    assert len(ws) == 2
    assert [len(w.rows) for w in ws] == [2, 1]


def test_custom_max_window_size_one_per_message() -> None:
    ws = plan_windows([_row(i, 5, i) for i in range(3)], max_window_size=1)
    assert len(ws) == 3
