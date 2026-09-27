"""Subsistema geo — plano de ubicación: gateway de pings + storage + capa de acceso.

Sin API externa (no toca geocoding/rutas): solo DB. El `client`/`auth_client` van por HTTP (cada
request commitea su tx); los tests de store/reader usan el `conn` de larga vida (leen su propia tx);
los de `StoredLocationSource` insertan en una conexión COMMITEADA aparte porque la fuente abre su
propia conexión y no vería datos sin commitear.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.geo import LocationDomain, LocationReader, LocationSource, StoredLocationSource
from memex.geo.domain import LocationUnavailableError
from memex.geo.store import PingInput, insert_pings, latest_ping, ping_at, pings_in_range


def _ping(captured_at: str, lat: float, lng: float, **extra: Any) -> dict[str, Any]:
    return {"captured_at": captured_at, "lat": lat, "lng": lng, **extra}


def _count(user_id: int) -> int:
    with connection() as c:
        n = c.execute(
            text("SELECT COUNT(*) FROM geo_location_pings WHERE user_id = :u"),
            {"u": user_id},
        ).scalar()
    return int(n or 0)


# ---------------- ingesta (POST /gateway/location/pings) ----------------


def test_pings_ingest_persists_and_counts(client: Any) -> None:
    r = client.post(
        "/gateway/location/pings",
        json={
            "pings": [
                _ping("2026-06-08T10:00:00-05:00", 4.65, -74.05),
                _ping("2026-06-08T10:01:00-05:00", 4.66, -74.06, accuracy_m=8.0, speed_mps=1.2),
            ]
        },
    )
    assert r.status_code == 200
    assert r.json() == {"inserted": 2}
    assert _count(1) == 2


def test_pings_no_dedup_same_batch_twice(client: Any) -> None:
    batch = {"pings": [_ping("2026-06-08T10:00:00-05:00", 4.65, -74.05)]}
    assert client.post("/gateway/location/pings", json=batch).json() == {"inserted": 1}
    assert client.post("/gateway/location/pings", json=batch).json() == {"inserted": 1}
    assert _count(1) == 2  # SIN dedup: el mismo ping entra dos veces


def test_pings_empty_batch_ok(client: Any) -> None:
    r = client.post("/gateway/location/pings", json={"pings": []})
    assert r.status_code == 200
    assert r.json() == {"inserted": 0}


def test_ping_validation_rejects_bad_coords(client: Any) -> None:
    r = client.post(
        "/gateway/location/pings",
        json={"pings": [_ping("2026-06-08T10:00:00-05:00", 999.0, -74.0)]},
    )
    assert r.status_code == 422


def test_ping_validation_rejects_bad_heading(client: Any) -> None:
    r = client.post(
        "/gateway/location/pings",
        json={"pings": [_ping("2026-06-08T10:00:00-05:00", 4.65, -74.05, heading=400.0)]},
    )
    assert r.status_code == 422


def test_ping_validation_rejects_naive_captured_at(client: Any) -> None:
    r = client.post(
        "/gateway/location/pings",
        json={"pings": [_ping("2026-06-08T10:00:00", 4.65, -74.05)]},  # sin zona horaria
    )
    assert r.status_code == 422


def test_pings_require_auth_when_enforced(auth_client: Any) -> None:
    assert auth_client.post("/gateway/location/pings", json={"pings": []}).status_code == 401
    r = auth_client.post(
        "/gateway/location/pings",
        json={"pings": []},
        headers={"Authorization": "Bearer secret-test"},
    )
    assert r.status_code == 200


# ---------------- última ubicación (GET /gateway/location/latest) ----------------


def test_latest_returns_most_recent_by_captured_at(client: Any) -> None:
    client.post(
        "/gateway/location/pings",
        json={
            "pings": [
                _ping("2026-06-08T09:00:00-05:00", 1.0, 1.0),
                _ping("2026-06-08T11:00:00-05:00", 3.0, 3.0),  # el más reciente
                _ping("2026-06-08T10:00:00-05:00", 2.0, 2.0),
            ]
        },
    )
    body = client.get("/gateway/location/latest").json()
    assert (body["lat"], body["lng"]) == (3.0, 3.0)


def test_latest_round_trips_optional_fields(client: Any) -> None:
    client.post(
        "/gateway/location/pings",
        json={
            "pings": [
                _ping(
                    "2026-06-08T10:00:00-05:00",
                    4.65,
                    -74.05,
                    accuracy_m=7.5,
                    altitude_m=2600.0,
                    heading=90.0,
                    speed_mps=1.4,
                    source="device",
                    metadata={"battery": 88},
                )
            ]
        },
    )
    body = client.get("/gateway/location/latest").json()
    assert body["accuracy_m"] == 7.5
    assert body["altitude_m"] == 2600.0
    assert body["heading"] == 90.0
    assert body["speed_mps"] == 1.4
    assert body["source"] == "device"
    assert body["metadata"] == {"battery": 88}


def test_latest_empty_is_404(client: Any) -> None:
    assert client.get("/gateway/location/latest").status_code == 404


def test_pings_isolated_per_user(client: Any, seed_user2: int) -> None:
    with connection() as c:
        insert_pings(
            c,
            user_id=seed_user2,
            pings=[PingInput(lat=9.0, lng=9.0, captured_at=datetime(2026, 6, 8, 12, tzinfo=UTC))],
        )
    # user1 (client, auth no enforced → id=1) no ve el ping de user2.
    assert client.get("/gateway/location/latest").status_code == 404
    assert _count(seed_user2) == 1


# ---------------- store + reader (acceso server-side) ----------------


def _mk(captured: datetime, lat: float, lng: float) -> PingInput:
    return PingInput(lat=lat, lng=lng, captured_at=captured)


def test_store_latest_and_history(conn: Any) -> None:
    base = datetime(2026, 6, 8, 12, tzinfo=UTC)
    insert_pings(
        conn,
        user_id=1,
        pings=[
            _mk(base, 1.0, 1.0),
            _mk(base + timedelta(minutes=10), 2.0, 2.0),
            _mk(base + timedelta(minutes=20), 3.0, 3.0),
        ],
    )
    latest = latest_ping(conn, user_id=1)
    assert latest is not None and latest.point.lat == 3.0
    hist = pings_in_range(conn, user_id=1, start=base, end=base + timedelta(minutes=15))
    assert [p.point.lat for p in hist] == [1.0, 2.0]  # [start, end): excluye el de +20


def test_store_ping_at_nearest_and_staleness(conn: Any) -> None:
    base = datetime(2026, 6, 8, 12, tzinfo=UTC)
    insert_pings(
        conn,
        user_id=1,
        pings=[_mk(base, 1.0, 1.0), _mk(base + timedelta(minutes=30), 2.0, 2.0)],
    )
    near1 = ping_at(conn, user_id=1, when=base + timedelta(minutes=10))
    assert near1 is not None and near1.point.lat == 1.0
    near2 = ping_at(conn, user_id=1, when=base + timedelta(minutes=25))
    assert near2 is not None and near2.point.lat == 2.0
    stale = ping_at(
        conn, user_id=1, when=base + timedelta(minutes=15), max_staleness=timedelta(minutes=5)
    )
    assert stale is None


def test_store_ping_at_empty(conn: Any) -> None:
    assert ping_at(conn, user_id=1, when=datetime(2026, 6, 8, tzinfo=UTC)) is None


def test_reader_delegates_to_store(conn: Any) -> None:
    base = datetime(2026, 6, 8, 12, tzinfo=UTC)
    insert_pings(conn, user_id=1, pings=[_mk(base, 5.0, 5.0)])
    reader = LocationReader(conn, 1)
    latest = reader.latest()
    assert latest is not None and latest.point.lat == 5.0
    assert reader.at(base).point.lat == 5.0  # type: ignore[union-attr]
    assert len(reader.history(base, base + timedelta(hours=1))) == 1


# ---------------- StoredLocationSource (seam GPS → estimate_trip_from_source) ----------------


def test_stored_location_source_returns_latest_point() -> None:
    with connection() as c:  # commiteado: la fuente abre su propia conexión
        insert_pings(
            c,
            user_id=1,
            pings=[PingInput(lat=4.6, lng=-74.0, captured_at=datetime(2026, 6, 8, 12, tzinfo=UTC))],
        )
    point = asyncio.run(StoredLocationSource(1).current_location())
    assert (round(point.lat, 4), round(point.lng, 4)) == (4.6, -74.0)


def test_stored_location_source_raises_when_empty() -> None:
    with pytest.raises(LocationUnavailableError):
        asyncio.run(StoredLocationSource(1).current_location())


# ---------------- conformidad de Protocols ----------------


def test_reader_satisfies_location_domain(conn: Any) -> None:
    assert isinstance(LocationReader(conn, 1), LocationDomain)


def test_stored_source_satisfies_location_source() -> None:
    assert isinstance(StoredLocationSource(1), LocationSource)
