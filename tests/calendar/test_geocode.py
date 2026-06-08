"""Geocoding automático en la consolidación de calendar (`run_consolidation` → coords del lugar).

Gateado por `MEMEX_CALENDAR_GEOCODE`; provider fake (sin red). Cubre: hit, vacío/URL saltado, guard
de re-geocode (idempotencia), sin-resultado marcado, y flag apagado (no construye provider)."""

from __future__ import annotations

from datetime import date, time
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.geo.client import GeocodeResult, GeoNotFoundError, GeoPoint
from memex.modules.calendar import consolidate as cons_mod
from memex.modules.calendar.consolidate import run_consolidation

_PT = GeoPoint(4.65, -74.05)


class _FakeGeocoder:
    """Provider fake: solo `geocode` + `aclose`. Cuenta llamadas. Sin red."""

    def __init__(
        self, *, result: GeocodeResult | None = None, exc: Exception | None = None
    ) -> None:
        self._result = result
        self._exc = exc
        self.calls = 0

    async def geocode(self, address: str) -> GeocodeResult:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result

    async def aclose(self) -> None:
        return None


def _seed_event(location: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_calendar_events "
                    "(user_id, source_inbox_ids, title, starts_on, start_time, location, "
                    " priority_rank, protected, override_policy) "
                    "VALUES (1, ARRAY[]::bigint[], 'Cita', :d, :st, :loc, 0, false, 'replace') "
                    "RETURNING id"
                ),
                {"d": date(2026, 6, 8), "st": time(10, 0), "loc": location},
            ).scalar_one()
        )


def _geo() -> dict[str, Any]:
    with connection() as c:
        return dict(
            c.execute(
                text(
                    "SELECT geo_lat, geo_lng, geo_place_id, geo_geocoded_from, geo_geocoded_at "
                    "FROM mod_calendar_consolidated WHERE user_id = 1 AND NOT deleted"
                )
            )
            .mappings()
            .one()
        )


def _result() -> GeocodeResult:
    return GeocodeResult(
        point=_PT, formatted_address="Cra 7 #45-23, Bogotá", provider_place_id="PID"
    )


def _use_fake(monkeypatch: pytest.MonkeyPatch, provider: object) -> None:
    monkeypatch.setenv("MEMEX_CALENDAR_GEOCODE", "1")
    monkeypatch.setattr(cons_mod, "build_provider_from_env", lambda *a, **k: provider)


def test_geocodes_consolidated_location(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_event("Carrera 7 #45-23")
    fake = _FakeGeocoder(result=_result())
    _use_fake(monkeypatch, fake)

    run_consolidation(1)

    g = _geo()
    assert (g["geo_lat"], g["geo_lng"]) == (_PT.lat, _PT.lng)
    assert g["geo_place_id"] == "PID"
    assert g["geo_geocoded_from"] == "Carrera 7 #45-23"
    assert g["geo_geocoded_at"] is not None
    assert fake.calls == 1


def test_empty_location_not_geocoded(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_event("")
    fake = _FakeGeocoder(result=_result())
    _use_fake(monkeypatch, fake)

    run_consolidation(1)

    assert _geo()["geo_lat"] is None
    assert fake.calls == 0


def test_url_location_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_event("https://zoom.us/j/123")  # evento virtual → no se geocodifica
    fake = _FakeGeocoder(result=_result())
    _use_fake(monkeypatch, fake)

    run_consolidation(1)

    assert _geo()["geo_lat"] is None
    assert fake.calls == 0


def test_rerun_does_not_regeocode(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_event("Carrera 7 #45-23")
    fake = _FakeGeocoder(result=_result())
    _use_fake(monkeypatch, fake)

    run_consolidation(1)
    run_consolidation(1)  # location sin cambios → guard evita re-llamar a Maps

    assert fake.calls == 1


def test_not_found_marks_geocoded_from_without_coords(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_event("Lugar Inexistente XYZ")
    fake = _FakeGeocoder(exc=GeoNotFoundError("Lugar Inexistente XYZ"))
    _use_fake(monkeypatch, fake)

    run_consolidation(1)

    g = _geo()
    assert g["geo_lat"] is None
    assert g["geo_geocoded_from"] == "Lugar Inexistente XYZ"  # marcado → no reintenta ese texto


def test_flag_off_skips_geocoding(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_event("Carrera 7 #45-23")
    monkeypatch.delenv("MEMEX_CALENDAR_GEOCODE", raising=False)

    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("no debería construir el provider con el flag apagado")

    monkeypatch.setattr(cons_mod, "build_provider_from_env", _boom)

    run_consolidation(1)

    assert _geo()["geo_lat"] is None
