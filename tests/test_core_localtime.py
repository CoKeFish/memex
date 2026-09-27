"""memex.core.localtime — zona local del usuario + zona explícita del mensaje."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone

from memex.core.localtime import LOCAL_TZ, local_instant, normalize_tz, parse_tz, to_local_wallclock


def test_normalize_tz_forms() -> None:
    assert normalize_tz("UTC") == normalize_tz("gmt") == normalize_tz("Z") == "UTC"
    assert normalize_tz("+00:00") == "UTC"
    assert normalize_tz("-5") == normalize_tz("GMT-5") == normalize_tz("UTC-05:00") == "-05:00"
    assert normalize_tz("-0500") == "-05:00"
    assert normalize_tz("+01:00") == "+01:00"
    assert normalize_tz("+5:30") == "+05:30"
    assert normalize_tz("America/Bogota") == "America/Bogota"
    # ambiguas / basura → None (se asume local, no se adivina)
    assert normalize_tz("PT") is None
    assert normalize_tz("EST") is None
    assert normalize_tz("hora de Colombia") is None
    assert normalize_tz("+25:00") is None
    assert normalize_tz(None) is None
    assert normalize_tz("") is None


def test_parse_tz_offsets() -> None:
    assert parse_tz("UTC") is UTC or parse_tz("UTC") == UTC
    assert parse_tz("-05:00") == timezone(timedelta(hours=-5))
    assert parse_tz("+05:30") == timezone(timedelta(hours=5, minutes=30))
    assert parse_tz("Europe/Madrid").key == "Europe/Madrid"  # type: ignore[union-attr]
    assert parse_tz("PT") is None


def test_local_instant_defaults_to_local_and_honors_explicit() -> None:
    assert local_instant(date(2026, 6, 3), time(14, 30)) == datetime(2026, 6, 3, 19, 30, tzinfo=UTC)
    assert local_instant(date(2026, 6, 3)) == datetime(2026, 6, 3, 5, 0, tzinfo=UTC)
    assert local_instant(date(2026, 6, 3), time(14, 30), UTC) == datetime(
        2026, 6, 3, 14, 30, tzinfo=UTC
    )
    assert local_instant(date(2026, 6, 3), tz=UTC) == datetime(2026, 6, 3, 0, 0, tzinfo=UTC)
    assert local_instant(date(2026, 6, 3), time(9, 0)).tzinfo is LOCAL_TZ


def test_to_local_wallclock_crosses_midnight() -> None:
    assert to_local_wallclock(date(2026, 6, 3), time(15, 0), UTC) == (date(2026, 6, 3), time(10, 0))
    assert to_local_wallclock(date(2026, 6, 3), time(2, 0), UTC) == (date(2026, 6, 2), time(21, 0))
    madrid = parse_tz("Europe/Madrid")
    assert madrid is not None
    # 09:00 en Madrid (CEST, +2) = 07:00Z = 02:00 Bogotá
    assert to_local_wallclock(date(2026, 6, 3), time(9, 0), madrid) == (
        date(2026, 6, 3),
        time(2, 0),
    )
