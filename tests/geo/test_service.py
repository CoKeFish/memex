"""Funciones de servicio: el seam LocationSource (ManualLocationSource) y la delegación."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

import pytest

from memex.geo.client import (
    GeocodeResult,
    GeoPoint,
    GeoProvider,
    ManualLocationSource,
    TravelEstimate,
    TravelMode,
)
from memex.geo.service import estimate_trip_from_source, geocode_address


class _StubProvider:
    """Proveedor de prueba que captura el origen recibido (sin red)."""

    name: ClassVar[str] = "stub"

    def __init__(self) -> None:
        self.geocoded: list[str] = []
        self.last_origin: GeoPoint | None = None

    async def geocode(self, address: str) -> GeocodeResult:
        self.geocoded.append(address)
        return GeocodeResult(point=GeoPoint(10.0, 20.0), formatted_address=address)

    async def travel_estimate(
        self,
        origin: GeoPoint,
        destination: GeoPoint,
        *,
        mode: TravelMode = TravelMode.DRIVING,
        departure_time: datetime | None = None,
    ) -> TravelEstimate:
        self.last_origin = origin
        return TravelEstimate(duration_s=100, distance_m=200, mode=mode)

    async def aclose(self) -> None:
        return None


def test_stub_satisfies_protocol() -> None:
    assert isinstance(_StubProvider(), GeoProvider)


@pytest.mark.asyncio
async def test_estimate_trip_from_source_uses_manual_point() -> None:
    provider = _StubProvider()
    x = GeoPoint(-34.6, -58.4)
    est = await estimate_trip_from_source(provider, ManualLocationSource(x), GeoPoint(1.0, 1.0))
    assert provider.last_origin == x
    assert est.duration_s == 100


@pytest.mark.asyncio
async def test_geocode_address_delegates() -> None:
    provider = _StubProvider()
    result = await geocode_address(provider, "Somewhere")
    assert result.point == GeoPoint(10.0, 20.0)
    assert provider.geocoded == ["Somewhere"]
