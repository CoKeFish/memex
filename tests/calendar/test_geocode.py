"""Geocoding automático en la consolidación de calendar: resolución contra el catálogo.

`run_consolidation` → `_geocode_consolidated` (gateado por MEMEX_CALENDAR_GEOCODE) resuelve el
`location` de cada consolidado contra `geo_places` vía `memex.geo.places`: FK `place_id` + coords
denormalizadas. Dedupe doble: por texto normalizado EN la corrida y por caché persistente
(`geo_place_resolutions`). Provider fake vía monkeypatch de `build_provider_from_env` (sin red).
"""

from __future__ import annotations

from datetime import date, time
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.geo.client import GeocodeResult, GeoNotFoundError, GeoPoint
from memex.modules.calendar import consolidate as cons_mod
from memex.modules.calendar.consolidate import run_consolidation

_PT = GeoPoint(lat=4.6286, lng=-74.065)


class _FakeGeocoder:
    """Provider fake: `geocode` + `aclose` + `name` (lo usa el catálogo). Cuenta llamadas.

    Tres modos: `result` fijo, `exc` fija, o `results` (mapa texto→resultado/excepción)."""

    name = "fake"

    def __init__(
        self,
        *,
        result: GeocodeResult | None = None,
        exc: Exception | None = None,
        results: dict[str, GeocodeResult | Exception] | None = None,
    ) -> None:
        self._result = result
        self._exc = exc
        self._results = results
        self.calls = 0

    async def geocode(self, address: str) -> GeocodeResult:
        self.calls += 1
        if self._results is not None:
            out = self._results[address]
            if isinstance(out, Exception):
                raise out
            return out
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result

    async def aclose(self) -> None:
        return None


def _seed_event(location: str, *, title: str = "Cita", day: int = 8) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_calendar_events "
                    "(user_id, source_inbox_ids, title, starts_on, start_time, location, "
                    " priority_rank, protected, override_policy) "
                    "VALUES (1, ARRAY[]::bigint[], :t, :d, :st, :loc, 0, false, 'replace') "
                    "RETURNING id"
                ),
                {"t": title, "d": date(2026, 6, day), "st": time(10, 0), "loc": location},
            ).scalar_one()
        )


def _geo(title: str = "Cita") -> dict[str, Any]:
    with connection() as c:
        return dict(
            c.execute(
                text(
                    "SELECT place_id, geo_lat, geo_lng, geo_place_id, geo_geocoded_from, "
                    "geo_geocoded_at FROM mod_calendar_consolidated "
                    "WHERE user_id = 1 AND NOT deleted AND title = :t"
                ),
                {"t": title},
            )
            .mappings()
            .one()
        )


def _places() -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(
                text("SELECT id, name, provider_place_id FROM geo_places ORDER BY id")
            )
            .mappings()
            .all()
        ]


def _result(pid: str = "PID") -> GeocodeResult:
    return GeocodeResult(point=_PT, formatted_address="Cra 7 #45-23, Bogotá", provider_place_id=pid)


def _use_fake(monkeypatch: pytest.MonkeyPatch, provider: object) -> None:
    monkeypatch.setenv("MEMEX_CALENDAR_GEOCODE", "1")
    monkeypatch.setattr(cons_mod, "build_provider_from_env", lambda *a, **k: provider)


def test_geocodes_consolidated_location(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_event("Carrera 7 #45-23")
    fake = _FakeGeocoder(result=_result())
    _use_fake(monkeypatch, fake)

    run_consolidation(1)

    g = _geo()
    assert fake.calls == 1
    # FK al catálogo + denorm coherente con el LUGAR (no con la llamada suelta)
    assert g["place_id"] is not None
    assert (g["geo_lat"], g["geo_lng"]) == (_PT.lat, _PT.lng)
    assert g["geo_place_id"] == "PID"
    assert g["geo_geocoded_from"] == "Carrera 7 #45-23"
    assert g["geo_geocoded_at"] is not None
    places = _places()
    assert len(places) == 1
    assert places[0]["name"] == "Carrera 7 #45-23"  # el texto crudo que lo resolvió
    with connection() as c:
        n = c.execute(text("SELECT count(*) FROM geo_place_resolutions")).scalar_one()
    assert n == 1


def test_empty_location_not_geocoded(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_event("")
    fake = _FakeGeocoder(result=_result())
    _use_fake(monkeypatch, fake)

    run_consolidation(1)

    assert _geo()["place_id"] is None
    assert _geo()["geo_lat"] is None
    assert fake.calls == 0
    assert _places() == []


def test_url_location_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_event("https://zoom.us/j/123")  # evento virtual → no se geocodifica
    fake = _FakeGeocoder(result=_result())
    _use_fake(monkeypatch, fake)

    run_consolidation(1)

    assert _geo()["place_id"] is None
    assert fake.calls == 0
    assert _places() == []


def test_rerun_does_not_regeocode(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_event("Carrera 7 #45-23")
    fake = _FakeGeocoder(result=_result())
    _use_fake(monkeypatch, fake)

    run_consolidation(1)
    run_consolidation(1)  # location sin cambios → guard evita re-llamar a Maps

    assert fake.calls == 1


def test_not_found_cached_and_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_event("Lugar Inexistente XYZ")
    fake = _FakeGeocoder(exc=GeoNotFoundError("Lugar Inexistente XYZ"))
    _use_fake(monkeypatch, fake)

    run_consolidation(1)
    run_consolidation(1)  # el ZERO_RESULTS quedó cacheado (fix: antes re-llamaba eternamente)

    g = _geo()
    assert fake.calls == 1
    assert g["place_id"] is None
    assert g["geo_lat"] is None
    assert g["geo_geocoded_from"] == "Lugar Inexistente XYZ"
    with connection() as c:
        row = c.execute(text("SELECT place_id FROM geo_place_resolutions WHERE user_id = 1")).one()
    assert row[0] is None  # NULL cacheado en el catálogo


def test_flag_off_skips_geocoding(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_event("Carrera 7 #45-23")
    monkeypatch.delenv("MEMEX_CALENDAR_GEOCODE", raising=False)

    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("no debería construir el provider con el flag apagado")

    monkeypatch.setattr(cons_mod, "build_provider_from_env", _boom)

    run_consolidation(1)

    assert _geo()["place_id"] is None


def test_same_text_many_rows_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # 3 eventos DISTINTOS (títulos/días distintos: no los dedupe calendar) con el mismo lugar.
    for title, day in (("Clase A", 8), ("Clase B", 9), ("Clase C", 10)):
        _seed_event("Aula 301 Módulo B", title=title, day=day)
    fake = _FakeGeocoder(result=_result())
    _use_fake(monkeypatch, fake)

    run_consolidation(1)

    assert fake.calls == 1  # dedupe por texto EN la corrida
    assert len(_places()) == 1
    fks = {(_geo(t))["place_id"] for t in ("Clase A", "Clase B", "Clase C")}
    assert len(fks) == 1 and None not in fks  # las 3 filas apuntan al MISMO lugar


def test_distinct_texts_collapse_by_place_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_event("Gabriel Giraldo S.J. 3-507", title="Teórica", day=8)
    _seed_event("gabriel giraldo 3507", title="Práctica", day=9)
    fake = _FakeGeocoder(
        results={
            "Gabriel Giraldo S.J. 3-507": _result(pid="PID-GG"),
            "gabriel giraldo 3507": _result(pid="PID-GG"),
        }
    )
    _use_fake(monkeypatch, fake)

    run_consolidation(1)

    assert fake.calls == 2  # textos con norm distinto: dos llamadas...
    places = _places()
    assert len(places) == 1  # ...pero UN lugar (colapso por place_id del proveedor)
    assert places[0]["name"] == "Gabriel Giraldo S.J. 3-507"  # el primero gana
    assert _geo("Teórica")["place_id"] == _geo("Práctica")["place_id"]


def test_legacy_coords_without_fk_backfilled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simula una fila geocodificada por el código VIEJO: coords + geocoded_from pero sin FK.
    _seed_event("Carrera 7 #45-23")
    monkeypatch.delenv("MEMEX_CALENDAR_GEOCODE", raising=False)
    run_consolidation(1)  # crea el consolidado sin geo
    with connection() as c:
        c.execute(
            text(
                "UPDATE mod_calendar_consolidated SET geo_lat = 1.0, geo_lng = 2.0, "
                "geo_place_id = 'OLD', geo_geocoded_from = location, geo_geocoded_at = NOW() "
                "WHERE user_id = 1"
            )
        )

    fake = _FakeGeocoder(result=_result())
    _use_fake(monkeypatch, fake)
    run_consolidation(1)

    g = _geo()
    assert fake.calls == 1  # el backfill re-resuelve aunque el texto no cambió
    assert g["place_id"] is not None
    assert (g["geo_lat"], g["geo_lng"]) == (_PT.lat, _PT.lng)  # denorm refrescada del lugar


def test_deleted_place_reweaves(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_event("Carrera 7 #45-23")
    fake = _FakeGeocoder(result=_result())
    _use_fake(monkeypatch, fake)
    run_consolidation(1)
    assert _geo()["place_id"] is not None

    with connection() as c:  # borrar el lugar: FK → SET NULL, resoluciones → CASCADE
        c.execute(text("DELETE FROM geo_places"))
    run_consolidation(1)

    g = _geo()
    assert fake.calls == 2  # se re-teje gastando una llamada (documentado)
    assert g["place_id"] is not None
    assert len(_places()) == 1


def test_cleared_location_clears_geo(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_event("Carrera 7 #45-23")
    fake = _FakeGeocoder(result=_result())
    _use_fake(monkeypatch, fake)
    run_consolidation(1)
    assert _geo()["place_id"] is not None

    with connection() as c:  # un merge/update puede vaciar el location
        c.execute(text("UPDATE mod_calendar_consolidated SET location = '' WHERE user_id = 1"))
    run_consolidation(1)

    g = _geo()
    assert fake.calls == 1  # sin llamadas nuevas
    assert g["place_id"] is None
    assert g["geo_lat"] is None
    assert g["geo_geocoded_from"] is None  # sin lugar fantasma
