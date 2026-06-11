"""Catálogo de lugares (`geo/places.py`): resolve-or-create con caché por texto.

OJO: `tests/geo/test_places.py` (existente) cubre la resolución coordenada→POI de pings; este
archivo cubre el CATÁLOGO (texto→lugar, `geo_places` + `geo_place_resolutions`).
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.geo.client import GeocodeResult, GeoNotFoundError, GeoPoint
from memex.geo.places import get_place, list_places, resolve_place

_PT = GeoPoint(lat=4.6286, lng=-74.0650)


def _result(pid: str | None = "PID-1", address: str = "Cra 7 #40-62, Bogotá") -> GeocodeResult:
    return GeocodeResult(point=_PT, formatted_address=address, provider_place_id=pid)


class FakeProvider:
    """Geocoder fake: mapa texto→resultado/excepción + contador. Cumple `GeoProvider`."""

    name: ClassVar[str] = "fake"

    def __init__(self, results: dict[str, GeocodeResult | Exception]) -> None:
        self.results = results
        self.calls = 0

    async def geocode(self, address: str) -> GeocodeResult:
        self.calls += 1
        out = self.results[address]
        if isinstance(out, Exception):
            raise out
        return out

    # El resto del Protocol no se usa en el catálogo: stubs para cumplir `GeoProvider`.
    async def travel_estimate(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def reverse_geocode(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def nearby_place(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


async def _resolve(provider: FakeProvider, query: str, *, user_id: int = 1) -> int | None:
    with connection() as conn:
        return await resolve_place(conn, user_id, query, provider)


def _places_count() -> int:
    with connection() as c:
        return int(c.execute(text("SELECT count(*) FROM geo_places")).scalar_one())


def _resolutions() -> list[tuple[str, int | None]]:
    with connection() as c:
        rows = c.execute(
            text("SELECT query_norm, place_id FROM geo_place_resolutions ORDER BY id")
        ).all()
    return [(str(q), int(p) if p is not None else None) for q, p in rows]


@pytest.mark.asyncio
async def test_miss_creates_place_and_resolution() -> None:
    fake = FakeProvider({"Aula 301 Módulo B": _result()})
    place_id = await _resolve(fake, "Aula 301 Módulo B")

    assert place_id is not None
    assert fake.calls == 1
    with connection() as c:
        place = get_place(c, 1, place_id)
    assert place is not None
    assert place.name == "Aula 301 Módulo B"  # el texto crudo strippeado, no la dirección
    assert place.formatted_address == "Cra 7 #40-62, Bogotá"
    assert (place.lat, place.lng) == (_PT.lat, _PT.lng)
    assert place.provider == "fake"
    assert place.provider_place_id == "PID-1"
    assert _resolutions() == [("aula 301 módulo b", place_id)]


@pytest.mark.asyncio
async def test_hit_by_normalization_makes_zero_calls() -> None:
    fake = FakeProvider({"Aula 301": _result()})
    first = await _resolve(fake, "Aula 301")
    again = await _resolve(fake, "  aula   301 ")  # case + whitespace distintos, mismo norm

    assert again == first
    assert fake.calls == 1  # la segunda salió del caché
    assert _places_count() == 1


@pytest.mark.asyncio
async def test_zero_results_cached_and_not_retried() -> None:
    fake = FakeProvider({"lugar inexistente": GeoNotFoundError("ZERO_RESULTS")})
    assert await _resolve(fake, "lugar inexistente") is None
    assert await _resolve(fake, "lugar inexistente") is None

    assert fake.calls == 1  # el NULL también se cachea
    assert _places_count() == 0
    assert _resolutions() == [("lugar inexistente", None)]


@pytest.mark.asyncio
async def test_distinct_texts_collapse_by_provider_place_id() -> None:
    fake = FakeProvider(
        {
            "Gabriel Giraldo S.J. 3-507": _result(pid="PID-GG"),
            "gabriel giraldo 3507": _result(pid="PID-GG", address="Otra dirección"),
        }
    )
    a = await _resolve(fake, "Gabriel Giraldo S.J. 3-507")
    b = await _resolve(fake, "gabriel giraldo 3507")

    assert fake.calls == 2  # textos con norm distinto: dos llamadas...
    assert a == b  # ...pero UN solo lugar (colapso por place_id del proveedor)
    assert _places_count() == 1
    with connection() as c:
        place = get_place(c, 1, int(a or 0))
    assert place is not None
    assert place.name == "Gabriel Giraldo S.J. 3-507"  # el primer texto gana, nada se pisa
    assert place.formatted_address == "Cra 7 #40-62, Bogotá"
    assert len(_resolutions()) == 2  # ambas resoluciones apuntan al mismo lugar


@pytest.mark.asyncio
async def test_provider_without_place_id_creates_loose_rows() -> None:
    fake = FakeProvider({"sitio a": _result(pid=None), "sitio b": _result(pid=None)})
    a = await _resolve(fake, "sitio a")
    b = await _resolve(fake, "sitio b")
    assert a != b  # sin id estable no hay colapso: fila suelta por texto
    assert _places_count() == 2

    again = await _resolve(fake, "sitio a")  # el caché por texto sí aplica
    assert again == a
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_empty_text_is_noop() -> None:
    fake = FakeProvider({})
    assert await _resolve(fake, "   ") is None
    assert fake.calls == 0
    assert _resolutions() == []


@pytest.mark.asyncio
async def test_get_place_scoped_by_user(seed_user2: int) -> None:
    fake = FakeProvider({"Aula 301": _result()})
    place_id = await _resolve(fake, "Aula 301")
    assert place_id is not None
    with connection() as c:
        assert get_place(c, seed_user2, place_id) is None  # de otro user → no existe
        assert get_place(c, 1, 999999) is None


@pytest.mark.asyncio
async def test_list_places_counts_calendar_refs(seed_user2: int) -> None:
    fake = FakeProvider({"Aula 301": _result(pid="P1"), "Consultorio": _result(pid="P2")})
    aula = await _resolve(fake, "Aula 301")
    consultorio = await _resolve(fake, "Consultorio")

    with connection() as c:
        for i, (pid, deleted) in enumerate(
            [(aula, False), (aula, False), (aula, True), (consultorio, False)]
        ):
            c.execute(
                text(
                    "INSERT INTO mod_calendar_consolidated "
                    "(user_id, title, starts_on, place_id, deleted, deleted_source) "
                    "VALUES (1, :t, DATE '2026-07-01', :p, :d, CASE WHEN :d THEN 'user' END)"
                ),
                {"t": f"Evento {i}", "p": pid, "d": deleted},
            )

    with connection() as c:
        items = list_places(c, 1)
        other = list_places(c, seed_user2)

    assert other == []
    assert [(i["name"], i["event_count"]) for i in items] == [
        ("Aula 301", 2),  # el tombstoneado no cuenta
        ("Consultorio", 1),
    ]
