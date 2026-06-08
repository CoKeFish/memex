"""Resolución de lugares: reverse geocoding + POI, service.resolve_place, caché y movimiento.

Los providers se prueban con respx (sin red); el servicio/caché/movimiento con un provider fake que
cuenta llamadas (para verificar que la caché evita re-llamar y que el tránsito no toca Maps). Sin
llamadas reales a Google/ORS.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import ClassVar

import pytest
import respx
from pydantic import SecretStr

from memex.geo.client import (
    GeocodeResult,
    GeoNotFoundError,
    GeoPoint,
    GeoProviderError,
    PlaceResult,
    TravelEstimate,
    TravelMode,
)
from memex.geo.config import GeoConfig
from memex.geo.domain import resolve_place_at, resolve_place_cached
from memex.geo.google import GoogleMapsProvider
from memex.geo.ors import OpenRouteServiceProvider
from memex.geo.service import resolve_place
from memex.geo.store import PingInput, insert_pings

PT = GeoPoint(4.65, -74.05)
WHEN = datetime(2026, 6, 8, 12, tzinfo=UTC)
GMAPS = "https://maps.googleapis.com"
PLACES = "https://places.googleapis.com/v1/places:searchNearby"
ORS = "https://api.openrouteservice.org"


def _google() -> GoogleMapsProvider:
    cfg = GeoConfig(
        provider="google", api_key=SecretStr("GKEY"), base_url=GMAPS, backoff_base=0.001
    )
    return GoogleMapsProvider(cfg)


def _ors() -> OpenRouteServiceProvider:
    cfg = GeoConfig(provider="ors", api_key=SecretStr("OKEY"), base_url=ORS, backoff_base=0.001)
    return OpenRouteServiceProvider(cfg)


# ---------------- providers (respx) ----------------


@pytest.mark.asyncio
async def test_google_reverse_geocode_ok() -> None:
    body = {
        "status": "OK",
        "results": [{"formatted_address": "Cra 7 #1-2, Bogotá", "place_id": "PID"}],
    }
    with respx.mock(base_url=GMAPS) as router:
        route = router.get("/maps/api/geocode/json").respond(json=body)
        async with _google() as p:
            r = await p.reverse_geocode(PT)
    assert route.called
    assert r.formatted_address == "Cra 7 #1-2, Bogotá"
    assert r.provider_place_id == "PID"
    assert route.calls[0].request.url.params["latlng"] == "4.65,-74.05"


@pytest.mark.asyncio
async def test_google_nearby_place_ok() -> None:
    body = {
        "places": [
            {
                "displayName": {"text": "Juan Valdez Café"},
                "formattedAddress": "Cra 7 #1-2",
                "types": ["cafe", "restaurant"],
                "id": "PLACEID",
            }
        ]
    }
    with respx.mock() as router:
        route = router.post(PLACES).respond(json=body)
        async with _google() as p:
            r = await p.nearby_place(PT, radius_m=40.0)
    assert route.called
    assert r.name == "Juan Valdez Café"
    assert r.formatted_address == "Cra 7 #1-2"
    assert r.provider_place_id == "PLACEID"
    assert r.types == ("cafe", "restaurant")
    req = route.calls[0].request
    assert req.headers["X-Goog-Api-Key"] == "GKEY"
    assert "displayName" in req.headers["X-Goog-FieldMask"]
    sent = json.loads(req.content)
    assert sent["rankPreference"] == "DISTANCE"
    assert sent["locationRestriction"]["circle"]["center"] == {
        "latitude": 4.65,
        "longitude": -74.05,
    }
    assert sent["locationRestriction"]["circle"]["radius"] == 40.0


@pytest.mark.asyncio
async def test_google_nearby_place_empty_is_not_found() -> None:
    with respx.mock() as router:
        router.post(PLACES).respond(json={"places": []})
        async with _google() as p:
            with pytest.raises(GeoNotFoundError):
                await p.nearby_place(PT)


@pytest.mark.asyncio
async def test_ors_reverse_geocode_ok() -> None:
    body = {
        "features": [
            {
                "properties": {"label": "London, UK", "gid": "G1", "confidence": 0.9},
                "geometry": {"coordinates": [-0.12, 51.5]},
            }
        ]
    }
    with respx.mock(base_url=ORS) as router:
        route = router.get("/geocode/reverse").respond(json=body)
        async with _ors() as p:
            r = await p.reverse_geocode(GeoPoint(51.5, -0.12))
    assert r.formatted_address == "London, UK"
    assert r.provider_place_id == "G1"
    req = route.calls[0].request
    assert req.url.params["point.lat"] == "51.5"
    assert req.url.params["point.lon"] == "-0.12"


@pytest.mark.asyncio
async def test_ors_nearby_place_unsupported() -> None:
    async with _ors() as p:
        with pytest.raises(GeoProviderError):
            await p.nearby_place(PT)


# ---------------- service.resolve_place (provider fake, sin red) ----------------


class _FakeProvider:
    """Provider fake que cuenta llamadas; solo implementa reverse_geocode/nearby_place de verdad."""

    name: ClassVar[str] = "fake"

    def __init__(
        self,
        *,
        address: GeocodeResult | None = None,
        poi: PlaceResult | None = None,
        reverse_exc: Exception | None = None,
        nearby_exc: Exception | None = None,
    ) -> None:
        self._address = address
        self._poi = poi
        self._reverse_exc = reverse_exc
        self._nearby_exc = nearby_exc
        self.reverse_calls = 0
        self.nearby_calls = 0

    async def geocode(self, address: str) -> GeocodeResult:
        raise NotImplementedError

    async def travel_estimate(
        self,
        origin: GeoPoint,
        destination: GeoPoint,
        *,
        mode: TravelMode = TravelMode.DRIVING,
        departure_time: datetime | None = None,
    ) -> TravelEstimate:
        raise NotImplementedError

    async def reverse_geocode(self, point: GeoPoint) -> GeocodeResult:
        self.reverse_calls += 1
        if self._reverse_exc is not None:
            raise self._reverse_exc
        return self._address or GeocodeResult(point=point, formatted_address="addr")

    async def nearby_place(
        self,
        point: GeoPoint,
        *,
        radius_m: float = 50.0,
        included_types: tuple[str, ...] | None = None,
    ) -> PlaceResult:
        self.nearby_calls += 1
        if self._nearby_exc is not None:
            raise self._nearby_exc
        if self._poi is None:
            raise GeoNotFoundError(point.as_latlng())
        return self._poi

    async def aclose(self) -> None:
        return None


def _poi(name: str = "Café X") -> PlaceResult:
    return PlaceResult(
        name=name, formatted_address="Calle 1", point=PT, provider_place_id="P1", types=("cafe",)
    )


@pytest.mark.asyncio
async def test_resolve_place_with_poi() -> None:
    provider = _FakeProvider(
        address=GeocodeResult(point=PT, formatted_address="Calle 1", provider_place_id="A1"),
        poi=_poi(),
    )
    place = await resolve_place(provider, PT)
    assert place.name == "Café X"
    assert place.formatted_address == "Calle 1"
    assert place.provider_place_id == "P1"  # gana el place_id del POI
    assert place.in_transit is False


@pytest.mark.asyncio
async def test_resolve_place_degrades_to_address_when_no_poi() -> None:
    # nearby levanta GeoProviderError (como ORS): se devuelve solo la dirección.
    provider = _FakeProvider(
        address=GeocodeResult(point=PT, formatted_address="Calle 1"),
        nearby_exc=GeoProviderError(0, "sin POIs"),
    )
    place = await resolve_place(provider, PT)
    assert place.name is None
    assert place.formatted_address == "Calle 1"


@pytest.mark.asyncio
async def test_resolve_place_nothing_found_raises() -> None:
    provider = _FakeProvider(reverse_exc=GeoNotFoundError("x"), poi=None)
    with pytest.raises(GeoNotFoundError):
        await resolve_place(provider, PT)


# ---------------- caché (geo_place_cache) ----------------


@pytest.mark.asyncio
async def test_resolve_place_cached_miss_then_hit(conn: object) -> None:
    provider = _FakeProvider(
        address=GeocodeResult(point=PT, formatted_address="Calle 1"), poi=_poi()
    )
    first = await resolve_place_cached(conn, provider, PT)  # type: ignore[arg-type]
    assert first.name == "Café X"
    assert (provider.reverse_calls, provider.nearby_calls) == (1, 1)

    second = await resolve_place_cached(conn, provider, PT)  # type: ignore[arg-type]
    assert second.name == "Café X"
    assert (provider.reverse_calls, provider.nearby_calls) == (1, 1)  # hit: no re-llama


# ---------------- movimiento (resolve_place_at) ----------------


@pytest.mark.asyncio
async def test_resolve_place_at_in_transit_skips_maps(conn: object) -> None:
    insert_pings(
        conn,  # type: ignore[arg-type]
        user_id=1,
        pings=[PingInput(lat=4.65, lng=-74.05, captured_at=WHEN, speed_mps=8.0)],
    )
    provider = _FakeProvider(
        address=GeocodeResult(point=PT, formatted_address="Calle 1"), poi=_poi()
    )
    res = await resolve_place_at(conn, provider, 1, WHEN)  # type: ignore[arg-type]
    assert res is not None
    assert res.in_transit is True
    assert (provider.reverse_calls, provider.nearby_calls) == (0, 0)  # tránsito: no toca Maps


@pytest.mark.asyncio
async def test_resolve_place_at_stationary_resolves(conn: object) -> None:
    insert_pings(
        conn,  # type: ignore[arg-type]
        user_id=1,
        pings=[PingInput(lat=4.65, lng=-74.05, captured_at=WHEN, speed_mps=0.2)],
    )
    provider = _FakeProvider(
        address=GeocodeResult(point=PT, formatted_address="Calle 1"), poi=_poi()
    )
    res = await resolve_place_at(conn, provider, 1, WHEN)  # type: ignore[arg-type]
    assert res is not None
    assert res.in_transit is False
    assert res.name == "Café X"


@pytest.mark.asyncio
async def test_resolve_place_at_no_fix_returns_none(conn: object) -> None:
    provider = _FakeProvider(
        address=GeocodeResult(point=PT, formatted_address="Calle 1"), poi=_poi()
    )
    res = await resolve_place_at(conn, provider, 1, WHEN)  # type: ignore[arg-type]
    assert res is None
