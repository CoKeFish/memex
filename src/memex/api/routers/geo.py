"""Gateway de ubicación — entrada de pings GPS del subsistema geo.

Dos endpoints bajo `/gateway/location`:

- POST /pings   — recibe los pings de la app móvil. Mismo surface gateway que el cliente local, pero
                  FUERA del pipeline de inbox: escribe directo en `geo_location_pings` vía el store.
                  Append-only, SIN dedup. Es el contrato que implementará la app.
- GET  /latest  — última ubicación conocida del usuario (read-back / verificación); 404 si no hay.

`user_id` sale de `current_user_id` (la app autentica como cualquier cliente externo). Loggea
`geo.location.received` (lo que llegó) y `geo.location.committed` (lo que se guardó).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from memex.api.auth import current_user_id
from memex.api.schemas import LocationFixRow, LocationIngestStats, LocationPingBatch
from memex.db import connection
from memex.geo.store import PingInput, insert_pings, latest_ping
from memex.logging import get_logger

router = APIRouter(prefix="/gateway/location", tags=["geo"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.geo.location")


@router.post("/pings", response_model=LocationIngestStats)
async def ingest_pings(body: LocationPingBatch, user_id: UserID) -> dict[str, Any]:
    _log.info("geo.location.received", user_id=user_id, count=len(body.pings))
    pings = [
        PingInput(
            lat=p.lat,
            lng=p.lng,
            captured_at=p.captured_at,
            accuracy_m=p.accuracy_m,
            altitude_m=p.altitude_m,
            heading=p.heading,
            speed_mps=p.speed_mps,
            source=p.source,
            metadata=p.metadata,
        )
        for p in body.pings
    ]
    with connection() as conn:
        inserted = insert_pings(conn, user_id=user_id, pings=pings)
    _log.info("geo.location.committed", user_id=user_id, inserted=inserted)
    return {"inserted": inserted}


@router.get("/latest", response_model=LocationFixRow)
async def latest_location(user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        fix = latest_ping(conn, user_id=user_id)
    if fix is None:
        raise HTTPException(status_code=404, detail="no location yet")
    return {
        "id": fix.id,
        "lat": fix.point.lat,
        "lng": fix.point.lng,
        "accuracy_m": fix.accuracy_m,
        "altitude_m": fix.altitude_m,
        "heading": fix.heading,
        "speed_mps": fix.speed_mps,
        "captured_at": fix.captured_at,
        "received_at": fix.received_at,
        "source": fix.source,
        "metadata": fix.metadata,
    }
