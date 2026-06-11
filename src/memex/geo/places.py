"""Catálogo de LUGARES del usuario (`geo_places`) + resolución texto→lugar con caché.

Este módulo es el SINGLE-WRITER del catálogo. Los dominios (calendario y finanzas) referencian
lugares por FK — geo no conoce dominios por nombre (patrón identidades): el lugar es dato
canónico transversal y la correlación rica la teje el grafo de relaciones.

Dedupe en dos niveles (pedido del dueño):
- `geo_place_resolutions` cachea cada TEXTO normalizado ya resuelto (mismo texto = 0 llamadas a
  Maps). `place_id NULL` = ZERO_RESULTS cacheado: tampoco se reintenta.
- el UNIQUE parcial por `provider_place_id` colapsa grafías distintas del MISMO lugar en una
  sola fila del catálogo ("Gabriel Giraldo S.J. 3-507" y "gabriel giraldo 3-507" → un lugar).

OJO homónimos: `memex.geo.service.resolve_place` (coordenada→POI, para pings) es OTRA cosa — por
eso este módulo NO se exporta desde `memex.geo.__init__`; importarlo calificado
(`from memex.geo import places`). Ídem `geo_place_cache` (caché GLOBAL por celda del reverse
geocoding) vs `geo_place_resolutions` (caché POR USUARIO por texto, de acá).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.geo.client import GeoNotFoundError, GeoProvider
from memex.geo.service import geocode_address
from memex.logging import get_logger
from memex.modules.contract import normalize

_log = get_logger("memex.geo.places")


@dataclass(frozen=True)
class PlaceRecord:
    """Una fila del catálogo, tal como la consumen los dominios (calendario, CLI)."""

    id: int
    name: str
    formatted_address: str
    lat: float
    lng: float
    provider: str
    provider_place_id: str | None
    source: str
    created_at: datetime


def get_place(conn: Connection, user_id: int, place_id: int) -> PlaceRecord | None:
    row = (
        conn.execute(
            text(
                """
                SELECT id, name, formatted_address, lat, lng, provider, provider_place_id,
                       source, created_at
                FROM geo_places WHERE id = :pid AND user_id = :uid
                """
            ),
            {"pid": place_id, "uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return PlaceRecord(
        id=int(row["id"]),
        name=str(row["name"]),
        formatted_address=str(row["formatted_address"]),
        lat=float(row["lat"]),
        lng=float(row["lng"]),
        provider=str(row["provider"]),
        provider_place_id=(
            str(row["provider_place_id"]) if row["provider_place_id"] is not None else None
        ),
        source=str(row["source"]),
        created_at=row["created_at"],
    )


def _cached_resolution(conn: Connection, user_id: int, query_norm: str) -> tuple[bool, int | None]:
    """(hay_fila, place_id) — la fila puede existir con `place_id NULL` (ZERO_RESULTS cacheado)."""
    row = conn.execute(
        text("SELECT place_id FROM geo_place_resolutions WHERE user_id = :uid AND query_norm = :q"),
        {"uid": user_id, "q": query_norm},
    ).first()
    if row is None:
        return False, None
    return True, (int(row[0]) if row[0] is not None else None)


def _cache_resolution(
    conn: Connection, user_id: int, query_norm: str, place_id: int | None
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO geo_place_resolutions (user_id, query_norm, place_id)
            VALUES (:uid, :q, :pid)
            ON CONFLICT (user_id, query_norm) DO UPDATE
              SET place_id = EXCLUDED.place_id, resolved_at = NOW()
            """
        ),
        {"uid": user_id, "q": query_norm, "pid": place_id},
    )


def _upsert_place(
    conn: Connection,
    user_id: int,
    *,
    name: str,
    formatted_address: str,
    lat: float,
    lng: float,
    provider: str,
    provider_place_id: str | None,
    source: str,
) -> tuple[int, bool]:
    """Upsert de UNA fila del catálogo → `(place_id, created)`. Con `provider_place_id` colapsa
    por el UNIQUE parcial; sin él, fila suelta. Lo comparten `resolve_place` (texto geocodificado,
    source='geocode') y `upsert_place_from_poi` (POI de un ping GPS, source='gps')."""
    if provider_place_id is not None:
        # `DO UPDATE` no-op deliberado: garantiza RETURNING bajo concurrencia (scheduler y
        # «Sincronizar ahora» pueden consolidar a la vez). En colisión NO se pisa nada: ni el
        # name (el primer texto que lo resolvió gana) ni address/coords/source (estables).
        row = conn.execute(
            text(
                """
                INSERT INTO geo_places
                  (user_id, name, formatted_address, lat, lng, provider, provider_place_id,
                   source)
                VALUES (:uid, :name, :addr, :lat, :lng, :prov, :pid, :source)
                ON CONFLICT (user_id, provider, provider_place_id)
                  WHERE provider_place_id IS NOT NULL
                  DO UPDATE SET provider_place_id = EXCLUDED.provider_place_id
                RETURNING id, (xmax = 0) AS created
                """
            ),
            {
                "uid": user_id,
                "name": name,
                "addr": formatted_address,
                "lat": lat,
                "lng": lng,
                "prov": provider,
                "pid": provider_place_id,
                "source": source,
            },
        ).first()
        assert row is not None  # RETURNING siempre devuelve con el DO UPDATE no-op
        return int(row[0]), bool(row[1])
    # Proveedor sin id estable → fila suelta (no colapsa grafías; documentado).
    place_id = int(
        conn.execute(
            text(
                """
                INSERT INTO geo_places
                  (user_id, name, formatted_address, lat, lng, provider, source)
                VALUES (:uid, :name, :addr, :lat, :lng, :prov, :source)
                RETURNING id
                """
            ),
            {
                "uid": user_id,
                "name": name,
                "addr": formatted_address,
                "lat": lat,
                "lng": lng,
                "prov": provider,
                "source": source,
            },
        ).scalar_one()
    )
    return place_id, True


async def resolve_place(
    conn: Connection, user_id: int, query_text: str, provider: GeoProvider
) -> int | None:
    """Resuelve un texto de lugar contra el catálogo (resolve-or-create, como identidades).

    Caché primero (incluido el NULL de ZERO_RESULTS: no se reintenta); miss → geocodifica el
    texto CRUDO (el normalizado es solo clave de caché) → upsert del lugar por
    `provider_place_id` (colisión = conservar TODO estable: el `name` del primer texto gana) →
    cachea la resolución. `GeoQuotaError`/`GeoProviderError`/`GeoConfigError` PROPAGAN (el
    caller decide cortar el lote); solo `GeoNotFoundError` se absorbe como NULL cacheado."""
    query_norm = normalize(query_text)
    if not query_norm:
        return None

    hit, cached = _cached_resolution(conn, user_id, query_norm)
    if hit:
        _log.info("geo.places.resolution_hit", user_id=user_id, place_id=cached)
        return cached

    try:
        result = await geocode_address(provider, query_text)
    except GeoNotFoundError:
        _cache_resolution(conn, user_id, query_norm, None)
        _log.info("geo.places.resolution_cached", user_id=user_id, place_id=None)
        return None

    name = query_text.strip()
    place_id, created = _upsert_place(
        conn,
        user_id,
        name=name,
        formatted_address=result.formatted_address,
        lat=result.point.lat,
        lng=result.point.lng,
        provider=provider.name,
        provider_place_id=result.provider_place_id,
        source="geocode",
    )
    if created:
        _log.info("geo.places.created", user_id=user_id, place_id=place_id, name=name)
    _cache_resolution(conn, user_id, query_norm, place_id)
    _log.info("geo.places.resolution_cached", user_id=user_id, place_id=place_id)
    return place_id


def upsert_place_from_poi(
    conn: Connection,
    user_id: int,
    *,
    name: str,
    formatted_address: str,
    lat: float,
    lng: float,
    provider: str,
    provider_place_id: str | None,
    source: str = "gps",
) -> int:
    """Da de alta (o colapsa) en el catálogo un lugar que YA viene resuelto a coordenadas/POI —
    el reverse geocoding de un ping GPS (seam de finanzas). Sin red y sin tocar
    `geo_place_resolutions` (no hay texto de búsqueda: la identidad es el `provider_place_id`
    cuando el POI lo trae; sin él, fila suelta). `source` distingue cómo nació la fila
    ('gps' vs 'geocode'); la columna no tiene CHECK a propósito."""
    place_id, created = _upsert_place(
        conn,
        user_id,
        name=name,
        formatted_address=formatted_address,
        lat=lat,
        lng=lng,
        provider=provider,
        provider_place_id=provider_place_id,
        source=source,
    )
    if created:
        _log.info(
            "geo.places.created", user_id=user_id, place_id=place_id, name=name, source=source
        )
    return place_id


def list_places(conn: Connection, user_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
    """Inventario del catálogo con cuántas referencias entrantes tiene cada lugar por dominio:
    eventos de calendario (`event_count`) y pagos consolidados (`payment_count`).

    La lectura cross-tabla es deliberada (contar referencias entrantes es inventario, no
    acoplamiento): geo sigue sin escribir ni conocer la semántica del dominio. `COUNT(DISTINCT)`
    porque los dos LEFT JOIN multiplican filas entre sí (eventos x pagos del mismo lugar)."""
    rows = (
        conn.execute(
            text(
                """
                SELECT p.id, p.name, p.formatted_address, p.lat, p.lng, p.provider,
                       p.provider_place_id, p.source, p.created_at,
                       COUNT(DISTINCT c.id) AS event_count,
                       COUNT(DISTINCT f.id) AS payment_count
                FROM geo_places p
                LEFT JOIN mod_calendar_consolidated c
                       ON c.place_id = p.id AND c.user_id = p.user_id AND NOT c.deleted
                LEFT JOIN mod_finance_consolidated f
                       ON f.place_id = p.id AND f.user_id = p.user_id AND NOT f.deleted
                WHERE p.user_id = :uid
                GROUP BY p.id
                ORDER BY COUNT(DISTINCT c.id) + COUNT(DISTINCT f.id) DESC, p.id
                LIMIT :limit
                """
            ),
            {"uid": user_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    return [
        {
            "id": int(r["id"]),
            "name": r["name"],
            "formatted_address": r["formatted_address"],
            "lat": float(r["lat"]),
            "lng": float(r["lng"]),
            "provider": r["provider"],
            "provider_place_id": r["provider_place_id"],
            "source": r["source"],
            "created_at": r["created_at"],
            "event_count": int(r["event_count"]),
            "payment_count": int(r["payment_count"]),
        }
        for r in rows
    ]
