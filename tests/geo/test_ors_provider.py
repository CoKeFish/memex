"""OpenRouteServiceProvider con respx: swap de coordenadas (lng,lat↔lat,lng), auth header, matrix.

ORS no modela tráfico (departure_time ignorado) ni TRANSIT, y la key va en el header
`Authorization` (no en la URL). Los tests fijan esas garantías.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr

from memex.geo.client import (
    GeoNotFoundError,
    GeoPoint,
    GeoProvider,
    GeoProviderError,
    GeoQuotaError,
    TravelMode,
)
from memex.geo.config import GeoConfig
from memex.geo.ors import OpenRouteServiceProvider

BASE_URL = "https://api.openrouteservice.org"
GEOCODE = "/geocode/search"
MATRIX = "/v2/matrix/driving-car"


def _provider() -> OpenRouteServiceProvider:
    cfg = GeoConfig(
        provider="ors",
        api_key=SecretStr("OKEY"),
        base_url=BASE_URL,
        backoff_base=0.001,
        max_retries=3,
    )
    return OpenRouteServiceProvider(cfg)


def test_satisfies_protocol() -> None:
    assert isinstance(_provider(), GeoProvider)


@pytest.mark.asyncio
async def test_geocode_swaps_coordinates_and_uses_auth_header() -> None:
    body = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"label": "London", "confidence": 0.8, "gid": "G1"},
                "geometry": {"type": "Point", "coordinates": [-0.1275, 51.5074]},
            }
        ],
    }
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(GEOCODE).respond(json=body)
        async with _provider() as p:
            r = await p.geocode("London")
    # coordinates [lng, lat] = [-0.1275, 51.5074] → GeoPoint(lat=51.5074, lng=-0.1275)
    assert r.point.lat == 51.5074
    assert r.point.lng == -0.1275
    assert r.formatted_address == "London"
    assert r.confidence == 0.8
    assert r.provider_place_id == "G1"
    req = route.calls[0].request
    assert req.url.params["text"] == "London"
    assert req.headers["Authorization"] == "OKEY"
    assert "OKEY" not in str(req.url)


@pytest.mark.asyncio
async def test_geocode_empty_features_not_found() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get(GEOCODE).respond(json={"type": "FeatureCollection", "features": []})
        async with _provider() as p:
            with pytest.raises(GeoNotFoundError):
                await p.geocode("nowhere")


@pytest.mark.asyncio
async def test_matrix_swaps_and_parses() -> None:
    body = {"durations": [[1320.0]], "distances": [[5400.0]]}
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(MATRIX).respond(json=body)
        async with _provider() as p:
            est = await p.travel_estimate(GeoPoint(51.5, -0.12), GeoPoint(51.6, -0.10))
    assert est.duration_s == 1320
    assert est.distance_m == 5400
    assert est.duration_in_traffic_s is None
    sent = json.loads(route.calls[0].request.content)
    assert sent["locations"] == [[-0.12, 51.5], [-0.10, 51.6]]  # [lng, lat]
    assert sent["sources"] == [0]
    assert sent["destinations"] == [1]


@pytest.mark.asyncio
async def test_matrix_null_no_route_not_found() -> None:
    body = {"durations": [[None]], "distances": [[None]]}
    with respx.mock(base_url=BASE_URL) as router:
        router.post(MATRIX).respond(json=body)
        async with _provider() as p:
            with pytest.raises(GeoNotFoundError):
                await p.travel_estimate(GeoPoint(0.0, 0.0), GeoPoint(1.0, 1.0))


@pytest.mark.asyncio
async def test_departure_time_ignored_no_traffic() -> None:
    body = {"durations": [[600.0]], "distances": [[2000.0]]}
    future = datetime.now(UTC) + timedelta(days=1)
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(MATRIX).respond(json=body)
        async with _provider() as p:
            est = await p.travel_estimate(
                GeoPoint(0.0, 0.0), GeoPoint(1.0, 1.0), departure_time=future
            )
    assert est.duration_in_traffic_s is None
    sent = json.loads(route.calls[0].request.content)
    assert "departure" not in json.dumps(sent).lower()


@pytest.mark.asyncio
async def test_transit_not_supported() -> None:
    async with _provider() as p:
        with pytest.raises(GeoProviderError):
            await p.travel_estimate(GeoPoint(0.0, 0.0), GeoPoint(1.0, 1.0), mode=TravelMode.TRANSIT)


@pytest.mark.asyncio
async def test_429_quota() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get(GEOCODE).respond(429)
        async with _provider() as p:
            with pytest.raises(GeoQuotaError):
                await p.geocode("x")


@pytest.mark.asyncio
async def test_5xx_retry_exhausts() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(GEOCODE)
        route.side_effect = [httpx.Response(503) for _ in range(4)]
        async with _provider() as p:
            with pytest.raises(GeoProviderError):
                await p.geocode("x")
    assert route.call_count == 4
