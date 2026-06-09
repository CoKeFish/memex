"""Enriquecimiento geo de transacciones — dónde estuvo el usuario cuando ocurrió el cobro.

Worker DETERMINISTA on-demand (lo dispara la CLI `memex-finance geo`, NO el path de extracción):
para cada transacción con HORA precisa (`occurred_at_precision='datetime'`) busca el fix GPS más
cercano a `occurred_at` en `geo_location_pings` y resuelve el lugar (negocio/dirección) con caché,
vía `memex.geo.resolve_place_at`. Sin LLM (geo es determinista).

SEAM DORMIDO a propósito: hoy no hay pings (la app móvil no existe) → `resolve_place_at` devuelve
None y el worker no-opera limpio. Cuando lleguen pings, enriquece sin más cambios.

Consolidación geo-prioridad (decisión del dueño): si geo resuelve un lugar, ese pasa a ser el
`place` canónico (geo gana sobre el texto del comprobante), pero el texto extraído se preserva en
`metadata.place_extracted` y se compara (`metadata.geo.matches_extracted`). Nada se pierde. Lo ya
resuelto (`metadata.geo` presente) no se reprocesa; `no_fix` (sin ping cerca) sigue candidato.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.db import connection
from memex.geo import (
    GeoNotFoundError,
    GeoProviderError,
    ResolvedPlace,
    build_provider_from_env,
    resolve_place_at,
)
from memex.logging import get_logger

_log = get_logger("memex.modules.finance.geo")

#: Tolerancia por default: si el ping más cercano a `occurred_at` está más lejos que esto, no es
#: donde estabas cuando se hizo el cobro → no se ubica (evita pegar un fix lejano e irrelevante).
_DEFAULT_MAX_STALENESS = timedelta(minutes=15)


@dataclass
class GeoEnrichStats:
    """Resumen de una corrida del enriquecedor."""

    scanned: int = 0
    resolved: int = 0
    in_transit: int = 0
    no_fix: int = 0
    no_result: int = 0
    aborted: bool = False  # cortó por error de proveedor (cuota / key sin permiso)


@dataclass(frozen=True)
class _Candidate:
    id: int
    occurred_at: datetime
    place: str
    metadata: dict[str, Any]


def _candidates(conn: Connection, user_id: int, limit: int) -> list[_Candidate]:
    """Transacciones con hora precisa aún sin resolución geo (`metadata.geo` ausente). Las de
    precisión `date`/`inferred` se saltan: sin hora real no se puede ubicar el instante."""
    rows = (
        conn.execute(
            text(
                """
                SELECT id, occurred_at, place, metadata
                FROM mod_finance_transactions
                WHERE user_id = :uid AND occurred_at_precision = 'datetime'
                  AND (metadata->'geo') IS NULL
                ORDER BY occurred_at DESC
                LIMIT :limit
                """
            ),
            {"uid": user_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    return [
        _Candidate(
            id=int(r["id"]),
            occurred_at=r["occurred_at"],
            place=str(r["place"]),
            metadata=dict(r["metadata"]) if isinstance(r["metadata"], dict) else {},
        )
        for r in rows
    ]


def _matches_extracted(extracted: str, resolved: str) -> bool:
    """Comparación coarse y determinista texto-extraído vs lugar-geo (señal `matches_extracted`)."""
    a, b = extracted.strip().lower(), resolved.strip().lower()
    return bool(a) and bool(b) and (a in b or b in a)


def _apply_resolved(conn: Connection, c: _Candidate, place: ResolvedPlace) -> None:
    """Consolida con geo-prioridad: `place` ← lugar de geo; preserva el texto extraído y guarda el
    detalle geo + la señal de comparación en `metadata`."""
    resolved_name = place.name or place.formatted_address
    meta = dict(c.metadata)
    meta.setdefault("place_extracted", c.place)  # preserva el original (solo la 1ª vez)
    meta["geo"] = {
        "name": place.name,
        "formatted_address": place.formatted_address,
        "lat": place.point.lat,
        "lng": place.point.lng,
        "place_id": place.provider_place_id,
        "types": list(place.types),
        "in_transit": False,
        "matches_extracted": _matches_extracted(c.place, resolved_name),
        "resolved_at": datetime.now(UTC).isoformat(),
    }
    conn.execute(
        text(
            "UPDATE mod_finance_transactions SET place = :place, metadata = CAST(:meta AS JSONB) "
            "WHERE id = :id"
        ),
        {"id": c.id, "place": resolved_name, "meta": json.dumps(meta)},
    )


def _mark_geo(conn: Connection, c: _Candidate, geo: dict[str, Any]) -> None:
    """Escribe solo `metadata.geo` sin tocar `place` (casos in_transit / sin-resultado), para que la
    transacción no vuelva a ser candidata."""
    meta = dict(c.metadata)
    meta["geo"] = geo
    conn.execute(
        text("UPDATE mod_finance_transactions SET metadata = CAST(:meta AS JSONB) WHERE id = :id"),
        {"id": c.id, "meta": json.dumps(meta)},
    )


async def resolve_transaction_places(
    user_id: int,
    *,
    limit: int = 100,
    want_poi: bool = True,
    max_staleness: timedelta = _DEFAULT_MAX_STALENESS,
) -> GeoEnrichStats:
    """Resuelve el lugar GPS de hasta `limit` transacciones con hora precisa del user.

    No-op si no hay pings (seam dormido → `no_fix`). Best-effort por transacción; corta el lote ante
    error de proveedor (cuota / key sin permiso) preservando lo ya resuelto (la tx se commitea al
    salir limpio). `GeoConfigError` (falta key) se propaga al caller (la CLI la reporta)."""
    stats = GeoEnrichStats()
    provider = build_provider_from_env()
    try:
        with connection() as conn:
            candidates = _candidates(conn, user_id, limit)
            stats.scanned = len(candidates)
            for c in candidates:
                try:
                    place = await resolve_place_at(
                        conn,
                        provider,
                        user_id,
                        c.occurred_at,
                        max_staleness=max_staleness,
                        want_poi=want_poi,
                    )
                except GeoNotFoundError:
                    _mark_geo(
                        conn,
                        c,
                        {"resolved": False, "resolved_at": datetime.now(UTC).isoformat()},
                    )
                    stats.no_result += 1
                    continue
                except GeoProviderError as e:  # cuota / key sin permiso / proveedor caído → cortar
                    _log.warning(
                        "finance.geo.aborted",
                        user_id=user_id,
                        resolved=stats.resolved,
                        error=str(e),
                    )
                    stats.aborted = True
                    break
                if place is None:
                    stats.no_fix += 1  # sin fix cerca → no se escribe (sigue candidata)
                    continue
                if place.in_transit:
                    _mark_geo(
                        conn,
                        c,
                        {
                            "in_transit": True,
                            "lat": place.point.lat,
                            "lng": place.point.lng,
                            "resolved_at": datetime.now(UTC).isoformat(),
                        },
                    )
                    stats.in_transit += 1
                    continue
                _apply_resolved(conn, c, place)
                stats.resolved += 1
    finally:
        await provider.aclose()
    _log.info(
        "finance.geo.done",
        user_id=user_id,
        scanned=stats.scanned,
        resolved=stats.resolved,
        in_transit=stats.in_transit,
        no_fix=stats.no_fix,
        no_result=stats.no_result,
        aborted=stats.aborted,
    )
    return stats
