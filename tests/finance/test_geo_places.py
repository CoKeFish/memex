"""`resolve_transaction_places`: geo-prioridad al consolidar, seam DORMIDO (sin pings → no-op),
tránsito y filtro de precisión. Provider fake (sin red); pings + DB reales (ejercita el camino real
`resolve_place_at` → caché → escritura)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.geo.client import GeocodeResult, GeoNotFoundError, GeoPoint, PlaceResult
from memex.geo.store import PingInput, insert_pings
from memex.modules.finance import geo_places
from memex.modules.finance.geo_places import resolve_transaction_places

_AT = datetime(2026, 6, 8, 14, 0, tzinfo=UTC)
_PT = GeoPoint(4.65, -74.05)


class _FakeProvider:
    """Provider fake: `reverse_geocode` + `nearby_place` + `aclose`. Sin red."""

    def __init__(
        self, *, address: GeocodeResult | None = None, poi: PlaceResult | None = None
    ) -> None:
        self._address = address
        self._poi = poi

    async def reverse_geocode(self, point: GeoPoint) -> GeocodeResult:
        return self._address or GeocodeResult(point=point, formatted_address="addr")

    async def nearby_place(
        self,
        point: GeoPoint,
        *,
        radius_m: float = 50.0,
        included_types: tuple[str, ...] | None = None,
    ) -> PlaceResult:
        if self._poi is None:
            raise GeoNotFoundError(point.as_latlng())
        return self._poi

    async def aclose(self) -> None:
        return None


def _poi(name: str = "Juan Valdez Café") -> PlaceResult:
    return PlaceResult(
        name=name,
        formatted_address="Cra 7 #1-2",
        point=_PT,
        provider_place_id="P1",
        types=("cafe",),
    )


def _seed_tx(*, place: str = "", precision: str = "datetime", occurred_at: datetime = _AT) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_finance_transactions "
                    "(user_id, source_inbox_ids, direction, amount, currency, occurred_at, "
                    " occurred_at_precision, place) "
                    "VALUES (1, ARRAY[]::bigint[], 'egreso', 100, 'USD', :at, :prec, :place) "
                    "RETURNING id"
                ),
                {"at": occurred_at, "prec": precision, "place": place},
            ).scalar_one()
        )


def _seed_ping(*, speed_mps: float | None, captured_at: datetime = _AT) -> None:
    with connection() as c:
        insert_pings(
            c,
            user_id=1,
            pings=[
                PingInput(lat=_PT.lat, lng=_PT.lng, captured_at=captured_at, speed_mps=speed_mps)
            ],
        )


def _tx(tid: int) -> dict[str, Any]:
    with connection() as c:
        return dict(
            c.execute(
                text("SELECT place, metadata FROM mod_finance_transactions WHERE id = :id"),
                {"id": tid},
            )
            .mappings()
            .one()
        )


def _use_fake(monkeypatch: pytest.MonkeyPatch, provider: object) -> None:
    monkeypatch.setattr(geo_places, "build_provider_from_env", lambda *a, **k: provider)


@pytest.mark.asyncio
async def test_resolves_with_geo_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    tid = _seed_tx(place="rappi")
    _seed_ping(speed_mps=0.2)  # quieto → resuelve el lugar
    _use_fake(
        monkeypatch,
        _FakeProvider(address=GeocodeResult(point=_PT, formatted_address="Cra 7 #1-2"), poi=_poi()),
    )

    stats = await resolve_transaction_places(1)

    assert stats.resolved == 1
    row = _tx(tid)
    assert row["place"] == "Juan Valdez Café"  # geo gana sobre el texto extraído
    geo = row["metadata"]["geo"]
    assert geo["name"] == "Juan Valdez Café"
    assert (geo["lat"], geo["lng"]) == (_PT.lat, _PT.lng)
    assert geo["in_transit"] is False
    assert geo["matches_extracted"] is False  # "rappi" ≠ "juan valdez café"
    assert row["metadata"]["place_extracted"] == "rappi"  # original preservado, no se pierde


@pytest.mark.asyncio
async def test_no_ping_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    tid = _seed_tx(place="rappi")  # sin ping: seam dormido
    _use_fake(monkeypatch, _FakeProvider(poi=_poi()))

    stats = await resolve_transaction_places(1)

    assert (stats.scanned, stats.resolved, stats.no_fix) == (1, 0, 1)
    row = _tx(tid)
    assert row["place"] == "rappi"  # intacto
    assert "geo" not in row["metadata"]  # sigue candidata (reintenta si llegan pings)


@pytest.mark.asyncio
async def test_in_transit_marks_metadata_only(monkeypatch: pytest.MonkeyPatch) -> None:
    tid = _seed_tx(place="rappi")
    _seed_ping(speed_mps=8.0)  # en movimiento → el tránsito no es un lugar
    _use_fake(monkeypatch, _FakeProvider(poi=_poi()))

    stats = await resolve_transaction_places(1)

    assert stats.in_transit == 1
    row = _tx(tid)
    assert row["place"] == "rappi"  # no se toca el place
    assert row["metadata"]["geo"]["in_transit"] is True


@pytest.mark.asyncio
async def test_date_precision_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    tid = _seed_tx(place="rappi", precision="date")  # sin hora real
    _seed_ping(speed_mps=0.2)
    _use_fake(monkeypatch, _FakeProvider(poi=_poi()))

    stats = await resolve_transaction_places(1)

    assert stats.scanned == 0  # solo 'datetime' es candidata
    assert "geo" not in _tx(tid)["metadata"]
