import json
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import Connection, text

from memex.core.source import SourceRecord

InsertReason = Literal["duplicate"]
"""Why an insert was rejected. Extend as new rejection cases appear."""


@dataclass(frozen=True)
class InsertResult:
    inserted: bool
    id: int | None
    reason: InsertReason | None = None


def insert_record(
    conn: Connection,
    user_id: int,
    source_id: int,
    record: SourceRecord,
) -> InsertResult:
    """Insert one record. Idempotent via ON CONFLICT on (source_id, external_id).
    Validates source_id belongs to user_id; raises ValueError otherwise.
    Writes dedupe_keys after a successful insert.
    """
    owner = conn.execute(
        text("SELECT user_id FROM sources WHERE id = :sid"),
        {"sid": source_id},
    ).scalar()
    if owner is None:
        raise ValueError(f"source_id {source_id} does not exist")
    if owner != user_id:
        raise ValueError(f"source_id {source_id} does not belong to user {user_id}")

    # Dedup por CONTENIDO (antes de insertar): si una clave de alta confianza (`msgid:`) ya
    # existe para este usuario, es el MISMO correo llegando por otra carpeta/cuenta/fuente →
    # se rechaza como duplicado sin crear una segunda fila. Solo claves `msgid:` para acotar el
    # riesgo de falso positivo (telegram/social usan el external_id como clave y ya quedan
    # cubiertos por UNIQUE(source_id, external_id)).
    content_keys = [k for k in record.dedupe_keys if k.startswith("msgid:")]
    if content_keys:
        seen = conn.execute(
            text(
                "SELECT 1 FROM inbox_dedupe_keys WHERE user_id = :uid AND key = ANY(:keys) LIMIT 1"
            ),
            {"uid": user_id, "keys": content_keys},
        ).first()
        if seen is not None:
            return InsertResult(inserted=False, id=None, reason="duplicate")

    # Un payload no serializable a JSON (ingestor que no respetó model_dump(mode="json")) sería un
    # TypeError NO-ValueError que aborta el batch y atasca el cursor (poison-wedge). Convertirlo a
    # ValueError lo cuenta como record fallido (el cursor avanza), sin colgar la fuente.
    try:
        payload_json = json.dumps(record.payload)
    except (TypeError, ValueError) as e:
        raise ValueError(f"payload not JSON-serializable: {e}") from e

    row = conn.execute(
        text(
            """
            INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
            VALUES (:uid, :sid, :eid, :occ, CAST(:payload AS JSONB))
            ON CONFLICT (source_id, external_id) DO NOTHING
            RETURNING id
            """
        ),
        {
            "uid": user_id,
            "sid": source_id,
            "eid": record.external_id,
            "occ": record.occurred_at,
            "payload": payload_json,
        },
    ).first()

    if row is None:
        return InsertResult(inserted=False, id=None, reason="duplicate")

    inbox_id = int(row[0])

    if record.dedupe_keys:
        conn.execute(
            text(
                """
                INSERT INTO inbox_dedupe_keys (user_id, key, inbox_id, source_id)
                VALUES (:uid, :key, :iid, :sid)
                ON CONFLICT (user_id, key) DO NOTHING
                """
            ),
            [
                {"uid": user_id, "key": k, "iid": inbox_id, "sid": source_id}
                for k in record.dedupe_keys
            ],
        )

    return InsertResult(inserted=True, id=inbox_id)


def claim_batch(
    conn: Connection,
    user_id: int,
    source_id: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Lock + return up to `limit` pending rows for the user.
    Uses FOR UPDATE SKIP LOCKED so multiple consumers can run concurrently.
    """
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    source_filter = ""
    if source_id is not None:
        source_filter = "AND source_id = :sid"
        params["sid"] = source_id

    rows = (
        conn.execute(
            text(
                f"""
                SELECT id, source_id, external_id, occurred_at, payload, attempts
                FROM inbox
                WHERE user_id = :uid
                  AND processed_at IS NULL
                  {source_filter}
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def mark_processed(
    conn: Connection,
    user_id: int,
    inbox_id: int,
    error: str | None = None,
) -> None:
    """Mark a row processed (success or failure). Bumps attempts."""
    conn.execute(
        text(
            """
            UPDATE inbox
            SET processed_at = NOW(),
                process_error = :err,
                attempts = attempts + 1
            WHERE id = :id AND user_id = :uid
            """
        ),
        {"id": inbox_id, "uid": user_id, "err": error},
    )
