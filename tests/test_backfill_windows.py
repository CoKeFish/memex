"""Tests de la aritmética pura de ventanas del backfill (sin DB)."""

from __future__ import annotations

from datetime import date

import pytest

from memex.backfill import windows


def test_add_months_simple() -> None:
    assert windows.add_months(date(2026, 1, 15), 1) == date(2026, 2, 15)
    assert windows.add_months(date(2026, 1, 15), 2) == date(2026, 3, 15)
    assert windows.add_months(date(2026, 1, 15), 0) == date(2026, 1, 15)


def test_add_months_end_of_month_clamp() -> None:
    assert windows.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)  # no bisiesto
    assert windows.add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)  # bisiesto
    assert windows.add_months(date(2026, 3, 31), 1) == date(2026, 4, 30)  # abril tiene 30


def test_add_months_year_rollover() -> None:
    assert windows.add_months(date(2026, 11, 10), 3) == date(2027, 2, 10)
    assert windows.add_months(date(2026, 12, 31), 1) == date(2027, 1, 31)


def test_add_window_units() -> None:
    assert windows.add_window(date(2026, 1, 1), "day", 5) == date(2026, 1, 6)
    assert windows.add_window(date(2026, 1, 1), "week", 2) == date(2026, 1, 15)
    assert windows.add_window(date(2026, 1, 1), "month", 1) == date(2026, 2, 1)


def test_add_window_rejects_bad_count() -> None:
    with pytest.raises(ValueError, match="count"):
        windows.add_window(date(2026, 1, 1), "day", 0)


def test_next_window_clamps_last_partial() -> None:
    # 1 mes desde 2026-01-01 pero el rango termina antes → ventana recortada al fin
    assert windows.next_window(date(2026, 1, 1), date(2026, 1, 20), "month", 1) == (
        date(2026, 1, 1),
        date(2026, 1, 20),
    )
    # ventana completa cuando entra dentro del rango
    assert windows.next_window(date(2026, 1, 1), date(2026, 6, 1), "month", 1) == (
        date(2026, 1, 1),
        date(2026, 2, 1),
    )


def test_is_done() -> None:
    assert windows.is_done(date(2026, 6, 1), date(2026, 6, 1)) is True
    assert windows.is_done(date(2026, 6, 2), date(2026, 6, 1)) is True
    assert windows.is_done(date(2026, 5, 31), date(2026, 6, 1)) is False


def test_progress_pct() -> None:
    rs, re = date(2026, 1, 1), date(2026, 1, 11)  # 10 días de rango
    assert windows.progress_pct(rs, re, date(2026, 1, 1)) == 0.0
    assert windows.progress_pct(rs, re, date(2026, 1, 6)) == 50.0
    assert windows.progress_pct(rs, re, date(2026, 1, 11)) == 100.0
    assert windows.progress_pct(rs, re, date(2026, 2, 1)) == 100.0  # clamp más allá del fin
