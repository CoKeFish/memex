"""Capa de acceso del subsistema geo — cómo los consumidores leen la ubicación.

Dos piezas, ambas de solo lectura sobre `geo_location_pings`:

- `LocationDomain` (Protocol) + `LocationReader`: el handle tipado del sistema de ubicación, que
  calca el idiom `IdentidadesDomain`/`IdentidadesDomainReader`. Un módulo/daemon lo usa
  construyéndolo directo con su `(conn, user_id)` — p.ej. `LocationReader(ctx.conn, ctx.user_id)`.
  La ubicación es AMBIENTE (los pings ya están; no hay orden de extracción que la inyecte vía
  `ctx.deps`), por eso se construye directo y no pasa por el orquestador.
- `StoredLocationSource`: implementa el seam `LocationSource` de `client.py` leyendo el último ping
  → engancha el GPS real a `estimate_trip_from_source` sin cambiar su firma.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from sqlalchemy import Connection

from memex.db import connection
from memex.geo.client import GeoError, GeoPoint, GeoProvider, ResolvedPlace
from memex.geo.service import resolve_place
from memex.geo.store import (
    LocationFix,
    get_cached_place,
    latest_ping,
    ping_at,
    pings_in_range,
    put_cached_place,
)


class LocationUnavailableError(GeoError):
    """El usuario todavía no tiene ninguna ubicación registrada. `status_code=0`."""

    def __init__(self, user_id: int) -> None:
        super().__init__(0, f"no location yet for user {user_id}")
        self.user_id = user_id


@runtime_checkable
class LocationDomain(Protocol):
    """Handle tipado del sistema de ubicación: dónde estuvo el usuario, ahora e históricamente."""

    def latest(self) -> LocationFix | None:
        """El fix más reciente, o None si el usuario no tiene pings."""
        ...

    def at(self, when: datetime, *, max_staleness: timedelta | None = None) -> LocationFix | None:
        """El fix más cercano a `when` (None si no hay, o si excede `max_staleness`)."""
        ...

    def history(self, start: datetime, end: datetime, *, limit: int = 10000) -> list[LocationFix]:
        """Los fixes con `captured_at` en `[start, end)`, en orden cronológico."""
        ...


class LocationReader:
    """Implementación del handle ligada a `(conn, user_id)`. Lectura sobre `geo_location_pings`.

    Es la vía por la que otros módulos/daemons acceden a la ubicación: se construye con la
    conexión y el user del consumidor y delega en `memex.geo.store`.
    """

    def __init__(self, conn: Connection, user_id: int) -> None:
        self._conn = conn
        self._user_id = user_id

    def latest(self) -> LocationFix | None:
        return latest_ping(self._conn, user_id=self._user_id)

    def at(self, when: datetime, *, max_staleness: timedelta | None = None) -> LocationFix | None:
        return ping_at(self._conn, user_id=self._user_id, when=when, max_staleness=max_staleness)

    def history(self, start: datetime, end: datetime, *, limit: int = 10000) -> list[LocationFix]:
        return pings_in_range(self._conn, user_id=self._user_id, start=start, end=end, limit=limit)


class StoredLocationSource:
    """`LocationSource` respaldada por el GPS almacenado: el origen X es el último ping del usuario.

    Reemplaza a `ManualLocationSource` para los callers que quieren "desde donde estoy" en vez de un
    punto explícito, sin tocar `estimate_trip_from_source`. Abre su propia conexión breve (es I/O,
    por eso el Protocol es async). Levanta `LocationUnavailableError` si todavía no hay ningún ping.
    """

    def __init__(self, user_id: int) -> None:
        self._user_id = user_id

    async def current_location(self) -> GeoPoint:
        with connection() as conn:
            fix = latest_ping(conn, user_id=self._user_id)
        if fix is None:
            raise LocationUnavailableError(self._user_id)
        return fix.point


#: Velocidad (m/s) sobre la que un fix se considera EN MOVIMIENTO (≈ caminar): no es un lugar.
_DEFAULT_MOVING_SPEED_MPS = 1.5


async def resolve_place_cached(
    conn: Connection,
    provider: GeoProvider,
    point: GeoPoint,
    *,
    max_age: timedelta | None = None,
    want_poi: bool = True,
    radius_m: float = 50.0,
) -> ResolvedPlace:
    """Resuelve `point` reusando la caché (`geo_place_cache`): hit → no toca Maps; miss/expirado →
    `resolve_place` y guarda. `max_age` fuerza re-resolver si la entrada cacheada es vieja.
    """
    cached = get_cached_place(conn, point=point, max_age=max_age)
    if cached is not None:
        return cached
    place = await resolve_place(provider, point, want_poi=want_poi, radius_m=radius_m)
    put_cached_place(conn, point=point, place=place)
    return place


async def resolve_place_at(
    conn: Connection,
    provider: GeoProvider,
    user_id: int,
    when: datetime,
    *,
    max_staleness: timedelta | None = None,
    moving_speed_mps: float = _DEFAULT_MOVING_SPEED_MPS,
    max_age: timedelta | None = None,
    want_poi: bool = True,
) -> ResolvedPlace | None:
    """El lugar donde estaba el usuario en `when`, consciente del movimiento.

    Toma el fix más cercano a `when` (vía `LocationReader`). Si venía EN MOVIMIENTO (`speed_mps` por
    encima de `moving_speed_mps`), devuelve `ResolvedPlace(in_transit=True)` SIN llamar a Maps
    (el tránsito no es un lugar). Si estaba quieto, resuelve con caché. None si no hay fix cerca.
    """
    fix = LocationReader(conn, user_id).at(when, max_staleness=max_staleness)
    if fix is None:
        return None
    if fix.speed_mps is not None and fix.speed_mps > moving_speed_mps:
        return ResolvedPlace(formatted_address="", point=fix.point, in_transit=True)
    return await resolve_place_cached(conn, provider, fix.point, max_age=max_age, want_poi=want_poi)
