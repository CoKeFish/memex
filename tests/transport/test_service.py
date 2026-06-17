"""Orquestación del daemon: cruce calendar+geo+notifier con un provider stub (sin red).

Se siembra en bloques `with connection()` PROPIOS (que commitean), no en la fixture `conn` (tx
abierta), porque `find_next_event` y `StoredLocationSource` abren sus propias conexiones.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.geo.client import (
    GeocodeResult,
    GeoPoint,
    GeoProvider,
    PlaceResult,
    TravelEstimate,
    TravelMode,
)
from memex.geo.store import PingInput, insert_pings
from memex.notifications import Notification
from memex.transport.config import TransportConfig, TransportSettings
from memex.transport.service import run_transport_for_user

_TZ = ZoneInfo("America/Bogota")
_NOW = datetime(2026, 6, 17, 14, 0, tzinfo=_TZ)
_CFG = TransportConfig.from_env(
    TransportSettings(
        mode="driving",
        buffer_min=10,
        lead_min=30,
        compute_window_min=120,
        horizon_hours=24,
        tz="America/Bogota",
    )
)
_LAT, _LNG = 4.65, -74.05


class _StubProvider:
    """`GeoProvider` de prueba: travel-time fijo y contador de llamadas (sin red)."""

    name: ClassVar[str] = "stub"

    def __init__(self, *, duration_s: int = 1800) -> None:
        self._duration_s = duration_s
        self.calls = 0

    async def geocode(self, address: str) -> GeocodeResult:
        return GeocodeResult(point=GeoPoint(0.0, 0.0), formatted_address=address)

    async def travel_estimate(
        self,
        origin: GeoPoint,
        destination: GeoPoint,
        *,
        mode: TravelMode = TravelMode.DRIVING,
        departure_time: datetime | None = None,
    ) -> TravelEstimate:
        self.calls += 1
        return TravelEstimate(duration_s=self._duration_s, distance_m=5000, mode=mode)

    async def reverse_geocode(self, point: GeoPoint) -> GeocodeResult:
        return GeocodeResult(point=point, formatted_address="stub")

    async def nearby_place(
        self,
        point: GeoPoint,
        *,
        radius_m: float = 50.0,
        included_types: tuple[str, ...] | None = None,
    ) -> PlaceResult:
        return PlaceResult(name="Stub", formatted_address="stub", point=point)

    async def aclose(self) -> None:
        return None


class _CollectingNotifier:
    """`Notifier` de prueba: acumula los avisos emitidos."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def notify(self, notification: Notification) -> None:
        self.sent.append(notification)


def _seed_event(*, start_time: time, lat: float | None = _LAT, lng: float | None = _LNG) -> None:
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO mod_calendar_consolidated
                    (user_id, title, starts_on, start_time, location, geo_lat, geo_lng)
                VALUES (1, 'Reunión', DATE '2026-06-17', :st, 'Aula 301', :lat, :lng)
                """
            ),
            {"st": start_time, "lat": lat, "lng": lng},
        )


def _seed_ping() -> None:
    with connection() as c:
        insert_pings(
            c,
            user_id=1,
            pings=[PingInput(lat=4.60, lng=-74.08, captured_at=_NOW - timedelta(minutes=2))],
        )


def test_stub_satisfies_protocol() -> None:
    assert isinstance(_StubProvider(), GeoProvider)


@pytest.mark.asyncio
async def test_leave_now_emits_notification() -> None:
    _seed_event(start_time=time(15, 0))  # evento en 60 min
    _seed_ping()
    provider = _StubProvider(duration_s=30 * 60)  # leave_by 14:20 → slack 20 min <= lead 30
    notifier = _CollectingNotifier()
    stats = await run_transport_for_user(
        user_id=1, provider=provider, notifier=notifier, cfg=_CFG, now=_NOW
    )
    assert stats.verdict == "leave_now"
    assert stats.notified == 1
    assert len(notifier.sent) == 1
    sent = notifier.sent[0]
    assert sent.kind == "transport.leave_by"
    assert sent.dedup_key.endswith(":leave_now")
    assert sent.payload["verdict"] == "leave_now"


@pytest.mark.asyncio
async def test_on_time_does_not_emit() -> None:
    _seed_event(start_time=time(15, 0))
    _seed_ping()
    provider = _StubProvider(duration_s=5 * 60)  # leave_by 14:45 → slack 45 min > lead 30
    notifier = _CollectingNotifier()
    stats = await run_transport_for_user(
        user_id=1, provider=provider, notifier=notifier, cfg=_CFG, now=_NOW
    )
    assert stats.verdict == "on_time"
    assert stats.notified == 0
    assert notifier.sent == []


@pytest.mark.asyncio
async def test_no_location_is_unknown_without_emit() -> None:
    _seed_event(start_time=time(15, 0))  # sin ping → no hay ubicación
    provider = _StubProvider(duration_s=30 * 60)
    notifier = _CollectingNotifier()
    stats = await run_transport_for_user(
        user_id=1, provider=provider, notifier=notifier, cfg=_CFG, now=_NOW
    )
    assert stats.verdict == "unknown"
    assert stats.reason == "no_location"
    assert stats.notified == 0


@pytest.mark.asyncio
async def test_no_upcoming_event_is_checked_zero() -> None:
    provider = _StubProvider()  # sin evento sembrado
    notifier = _CollectingNotifier()
    stats = await run_transport_for_user(
        user_id=1, provider=provider, notifier=notifier, cfg=_CFG, now=_NOW
    )
    assert stats.checked == 0
    assert stats.verdict == "none"
    assert stats.notified == 0


@pytest.mark.asyncio
async def test_far_event_skips_maps() -> None:
    _seed_event(start_time=time(17, 30))  # en 3.5 h → más que compute_window (2 h)
    _seed_ping()
    provider = _StubProvider(duration_s=30 * 60)
    notifier = _CollectingNotifier()
    stats = await run_transport_for_user(
        user_id=1, provider=provider, notifier=notifier, cfg=_CFG, now=_NOW
    )
    assert stats.verdict == "on_time"
    assert stats.reason == "too_far"
    assert provider.calls == 0  # no se gastó una llamada a Maps
    assert notifier.sent == []


@pytest.mark.asyncio
async def test_event_without_coords_is_ignored() -> None:
    _seed_event(start_time=time(15, 0), lat=None, lng=None)  # sin geocodificar
    _seed_ping()
    provider = _StubProvider()
    notifier = _CollectingNotifier()
    stats = await run_transport_for_user(
        user_id=1, provider=provider, notifier=notifier, cfg=_CFG, now=_NOW
    )
    assert stats.checked == 0  # se omite: no hay destino que medir
    assert provider.calls == 0
