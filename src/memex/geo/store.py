"""Almacenamiento del subsistema geo — pings de ubicación en `geo_location_pings`.

Capa DB pura: raw SQL vía `text()`, recibe un `Connection` (no abre ni gestiona la conexión), calca
`memex.core.checkpoint`. Es la capa de storage que el servicio geo (orquestación HTTP) no tenía. La
leen los consumidores a través de `memex.geo.domain.LocationReader`; la escribe el endpoint del
gateway de ubicación.

SIN dedup a propósito: cada ping se inserta tal cual — no puede haber dos posiciones del usuario en
el mismo instante. Los timestamps son tz-aware (la columna es TIMESTAMPTZ); los `datetime` que se
pasan a las lecturas (`when`, `start`, `end`) DEBEN ser aware o la resta de timedeltas falla.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, RowMapping, text

from memex.geo.client import GeoPoint, ResolvedPlace


@dataclass(frozen=True)
class PingInput:
    """Un ping GPS entrante a persistir. Solo `lat`/`lng`/`captured_at` son obligatorios."""

    lat: float
    lng: float
    captured_at: datetime
    accuracy_m: float | None = None
    altitude_m: float | None = None
    heading: float | None = None
    speed_mps: float | None = None
    source: str = "device"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LocationFix:
    """Un ping ya almacenado, proyectado para los consumidores. `point` SIEMPRE (lat, lng)."""

    id: int
    point: GeoPoint
    captured_at: datetime
    received_at: datetime
    accuracy_m: float | None = None
    altitude_m: float | None = None
    heading: float | None = None
    speed_mps: float | None = None
    source: str = "device"
    metadata: dict[str, Any] = field(default_factory=dict)


#: Columnas que pueblan un `LocationFix` (orden estable para `_fix_from_row`).
_SELECT_COLS = (
    "id, lat, lng, accuracy_m, altitude_m, heading, speed_mps, "
    "captured_at, received_at, source, metadata"
)


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _fix_from_row(m: RowMapping) -> LocationFix:
    meta = m["metadata"]
    return LocationFix(
        id=int(m["id"]),
        point=GeoPoint(float(m["lat"]), float(m["lng"])),
        captured_at=m["captured_at"],
        received_at=m["received_at"],
        accuracy_m=_opt_float(m["accuracy_m"]),
        altitude_m=_opt_float(m["altitude_m"]),
        heading=_opt_float(m["heading"]),
        speed_mps=_opt_float(m["speed_mps"]),
        source=str(m["source"]),
        metadata=dict(meta) if isinstance(meta, dict) else {},
    )


def insert_pings(conn: Connection, *, user_id: int, pings: Sequence[PingInput]) -> int:
    """Inserta los pings tal cual (append-only, sin dedup). Devuelve cuántos se insertaron."""
    if not pings:
        return 0
    params = [
        {
            "uid": user_id,
            "lat": p.lat,
            "lng": p.lng,
            "accuracy_m": p.accuracy_m,
            "altitude_m": p.altitude_m,
            "heading": p.heading,
            "speed_mps": p.speed_mps,
            "captured_at": p.captured_at,
            "source": p.source,
            "metadata": json.dumps(p.metadata),
        }
        for p in pings
    ]
    conn.execute(
        text(
            """
            INSERT INTO geo_location_pings
                (user_id, lat, lng, accuracy_m, altitude_m, heading, speed_mps,
                 captured_at, source, metadata)
            VALUES
                (:uid, :lat, :lng, :accuracy_m, :altitude_m, :heading, :speed_mps,
                 :captured_at, :source, CAST(:metadata AS JSONB))
            """
        ),
        params,
    )
    return len(params)


def latest_ping(conn: Connection, *, user_id: int) -> LocationFix | None:
    """El ping más reciente del usuario (mayor `captured_at`), o None si no hay ninguno."""
    row = (
        conn.execute(
            text(
                f"SELECT {_SELECT_COLS} FROM geo_location_pings "
                "WHERE user_id = :uid ORDER BY captured_at DESC, id DESC LIMIT 1"
            ),
            {"uid": user_id},
        )
        .mappings()
        .first()
    )
    return _fix_from_row(row) if row is not None else None


def ping_at(
    conn: Connection,
    *,
    user_id: int,
    when: datetime,
    max_staleness: timedelta | None = None,
) -> LocationFix | None:
    """El fix más cercano a `when` (vecino antes o después, el que esté más próximo).

    Usa el índice `(user_id, captured_at DESC)` con dos LIMIT 1 (≤ when y > when). Con
    `max_staleness`, devuelve None si el más cercano está más lejos que esa tolerancia. None
    también si el usuario no tiene pings. `when` debe ser tz-aware.
    """
    before = (
        conn.execute(
            text(
                f"SELECT {_SELECT_COLS} FROM geo_location_pings "
                "WHERE user_id = :uid AND captured_at <= :when "
                "ORDER BY captured_at DESC, id DESC LIMIT 1"
            ),
            {"uid": user_id, "when": when},
        )
        .mappings()
        .first()
    )
    after = (
        conn.execute(
            text(
                f"SELECT {_SELECT_COLS} FROM geo_location_pings "
                "WHERE user_id = :uid AND captured_at > :when "
                "ORDER BY captured_at ASC, id ASC LIMIT 1"
            ),
            {"uid": user_id, "when": when},
        )
        .mappings()
        .first()
    )
    candidates = [m for m in (before, after) if m is not None]
    if not candidates:
        return None
    nearest = min(candidates, key=lambda m: abs(m["captured_at"] - when))
    if max_staleness is not None and abs(nearest["captured_at"] - when) > max_staleness:
        return None
    return _fix_from_row(nearest)


def pings_in_range(
    conn: Connection,
    *,
    user_id: int,
    start: datetime,
    end: datetime,
    limit: int = 10000,
) -> list[LocationFix]:
    """Pings con `captured_at` en `[start, end)`, en orden cronológico. Base del clustering."""
    rows = (
        conn.execute(
            text(
                f"SELECT {_SELECT_COLS} FROM geo_location_pings "
                "WHERE user_id = :uid AND captured_at >= :start AND captured_at < :end "
                "ORDER BY captured_at ASC, id ASC LIMIT :limit"
            ),
            {"uid": user_id, "start": start, "end": end, "limit": limit},
        )
        .mappings()
        .all()
    )
    return [_fix_from_row(m) for m in rows]


# --- Caché de lugares (geo_place_cache) — resoluciones coordenada → dirección/POI ----------------
# Global (sin user_id): una resolución es dato de referencia, igual para todos. Clave = la CELDA de
# la coordenada (lat/lng redondeados), para que puntos casi iguales reusen la misma resolución.

_CELL_PRECISION = 4  # decimales de la celda de caché ≈ 11 m


def _cell(point: GeoPoint) -> tuple[float, float]:
    return (round(point.lat, _CELL_PRECISION), round(point.lng, _CELL_PRECISION))


def get_cached_place(
    conn: Connection, *, point: GeoPoint, max_age: timedelta | None = None
) -> ResolvedPlace | None:
    """Resolución cacheada de la celda de `point`, o None si no hay (o expiró según `max_age`)."""
    clat, clng = _cell(point)
    sql = (
        "SELECT name, formatted_address, lat, lng, place_id, types "
        "FROM geo_place_cache WHERE cell_lat = :clat AND cell_lng = :clng"
    )
    params: dict[str, Any] = {"clat": clat, "clng": clng}
    if max_age is not None:
        sql += " AND resolved_at > :cutoff"
        params["cutoff"] = datetime.now(UTC) - max_age
    row = conn.execute(text(sql), params).mappings().first()
    if row is None:
        return None
    types = row["types"]
    return ResolvedPlace(
        formatted_address=str(row["formatted_address"]),
        point=GeoPoint(float(row["lat"]), float(row["lng"])),
        name=row["name"],
        provider_place_id=row["place_id"],
        types=tuple(t for t in types if isinstance(t, str)) if isinstance(types, list) else (),
    )


def put_cached_place(conn: Connection, *, point: GeoPoint, place: ResolvedPlace) -> None:
    """Upsert de la resolución de la celda de `point` (idempotente por celda)."""
    clat, clng = _cell(point)
    conn.execute(
        text(
            """
            INSERT INTO geo_place_cache (
                cell_lat, cell_lng, lat, lng, name, formatted_address, place_id, types, resolved_at
            )
            VALUES (
                :clat, :clng, :lat, :lng, :name, :addr, :pid, CAST(:types AS JSONB), NOW()
            )
            ON CONFLICT (cell_lat, cell_lng) DO UPDATE SET
                lat = EXCLUDED.lat,
                lng = EXCLUDED.lng,
                name = EXCLUDED.name,
                formatted_address = EXCLUDED.formatted_address,
                place_id = EXCLUDED.place_id,
                types = EXCLUDED.types,
                resolved_at = NOW()
            """
        ),
        {
            "clat": clat,
            "clng": clng,
            "lat": place.point.lat,
            "lng": place.point.lng,
            "name": place.name,
            "addr": place.formatted_address,
            "pid": place.provider_place_id,
            "types": json.dumps(list(place.types)),
        },
    )
