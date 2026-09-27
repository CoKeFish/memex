"""GoogleMapsProvider con respx (sin red): geocode + distancematrix, status-en-body, retries.

Google responde HTTP 200 con el resultado lógico en `status`; los tests cubren ese mapeo, la
key en query (no en logs), el departure_time con tráfico, y la lógica de retry/4xx.
"""

from __future__ import annotations

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
)
from memex.geo.config import GeoConfig
from memex.geo.google import GoogleMapsProvider

BASE_URL = "https://maps.googleapis.com"
GEOCODE = "/maps/api/geocode/json"
MATRIX = "/maps/api/distancematrix/json"


def _provider() -> GoogleMapsProvider:
    cfg = GeoConfig(
        provider="google",
        api_key=SecretStr("GKEY"),
        base_url=BASE_URL,
        backoff_base=0.001,
        max_retries=3,
    )
    return GoogleMapsProvider(cfg)


def test_satisfies_protocol() -> None:
    assert isinstance(_provider(), GeoProvider)


@pytest.mark.asyncio
async def test_geocode_ok_and_key_in_query() -> None:
    body = {
        "status": "OK",
        "results": [
            {
                "formatted_address": "Av X 123",
                "place_id": "PID",
                "geometry": {"location": {"lat": -34.6, "lng": -58.4}},
            }
        ],
    }
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(GEOCODE).respond(json=body)
        async with _provider() as p:
            r = await p.geocode("Av X 123")
    assert route.called
    assert r.point == GeoPoint(-34.6, -58.4)
    assert r.formatted_address == "Av X 123"
    assert r.provider_place_id == "PID"
    req = route.calls[0].request
    assert req.url.params["key"] == "GKEY"
    assert req.url.params["address"] == "Av X 123"


@pytest.mark.asyncio
async def test_geocode_zero_results_not_found() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get(GEOCODE).respond(json={"status": "ZERO_RESULTS", "results": []})
        async with _provider() as p:
            with pytest.raises(GeoNotFoundError):
                await p.geocode("nowhere")


@pytest.mark.asyncio
async def test_geocode_over_query_limit_quota() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get(GEOCODE).respond(json={"status": "OVER_QUERY_LIMIT"})
        async with _provider() as p:
            with pytest.raises(GeoQuotaError):
                await p.geocode("x")


@pytest.mark.asyncio
async def test_geocode_request_denied_provider_error() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get(GEOCODE).respond(json={"status": "REQUEST_DENIED", "error_message": "bad key"})
        async with _provider() as p:
            with pytest.raises(GeoProviderError) as ei:
                await p.geocode("x")
    assert not isinstance(ei.value, GeoQuotaError)


@pytest.mark.asyncio
async def test_travel_estimate_ok() -> None:
    body = {
        "status": "OK",
        "rows": [
            {
                "elements": [
                    {
                        "status": "OK",
                        "duration": {"value": 1200, "text": "20 min"},
                        "distance": {"value": 5000, "text": "5 km"},
                    }
                ]
            }
        ],
    }
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(MATRIX).respond(json=body)
        async with _provider() as p:
            est = await p.travel_estimate(GeoPoint(-34.6, -58.4), GeoPoint(-34.8, -58.5))
    assert est.duration_s == 1200
    assert est.distance_m == 5000
    assert est.duration_in_traffic_s is None
    req = route.calls[0].request
    assert req.url.params["origins"] == "-34.6,-58.4"
    assert req.url.params["destinations"] == "-34.8,-58.5"
    assert req.url.params["mode"] == "driving"
    assert "departure_time" not in req.url.params


@pytest.mark.asyncio
async def test_travel_estimate_future_departure_sends_traffic() -> None:
    body = {
        "status": "OK",
        "rows": [
            {
                "elements": [
                    {
                        "status": "OK",
                        "duration": {"value": 1200},
                        "distance": {"value": 5000},
                        "duration_in_traffic": {"value": 1500},
                    }
                ]
            }
        ],
    }
    future = datetime.now(UTC) + timedelta(days=1)
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(MATRIX).respond(json=body)
        async with _provider() as p:
            est = await p.travel_estimate(
                GeoPoint(0.0, 0.0), GeoPoint(1.0, 1.0), departure_time=future
            )
    assert est.duration_in_traffic_s == 1500
    req = route.calls[0].request
    assert "departure_time" in req.url.params
    assert req.url.params["traffic_model"] == "best_guess"


@pytest.mark.asyncio
async def test_travel_estimate_element_not_found() -> None:
    body = {"status": "OK", "rows": [{"elements": [{"status": "NOT_FOUND"}]}]}
    with respx.mock(base_url=BASE_URL) as router:
        router.get(MATRIX).respond(json=body)
        async with _provider() as p:
            with pytest.raises(GeoNotFoundError):
                await p.travel_estimate(GeoPoint(0.0, 0.0), GeoPoint(1.0, 1.0))


@pytest.mark.asyncio
async def test_retry_then_success_on_429() -> None:
    body = {
        "status": "OK",
        "results": [{"formatted_address": "X", "geometry": {"location": {"lat": 1.0, "lng": 2.0}}}],
    }
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(GEOCODE)
        route.side_effect = [httpx.Response(429), httpx.Response(200, json=body)]
        async with _provider() as p:
            r = await p.geocode("x")
    assert r.point == GeoPoint(1.0, 2.0)
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_retry_exhausts_on_500() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(GEOCODE)
        route.side_effect = [httpx.Response(500) for _ in range(4)]
        async with _provider() as p:
            with pytest.raises(GeoProviderError):
                await p.geocode("x")
    assert route.call_count == 4


@pytest.mark.asyncio
async def test_4xx_immediate_no_retry() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(GEOCODE).respond(404)
        async with _provider() as p:
            with pytest.raises(GeoProviderError):
                await p.geocode("x")
    assert route.call_count == 1
