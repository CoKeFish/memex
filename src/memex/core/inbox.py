import json
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import Connection, text

from memex.core.source import SourceRecord
from memex.version import code_version

InsertReason = Literal["duplicate", "updated"]
"""Why an insert did not create a row: `duplicate` (same record again) or `updated` (same
external_id with NEW content and `on_conflict="replace"`: the payload was refreshed)."""

OnConflict = Literal["ignore", "replace"]


@dataclass(frozen=True)
class InsertResult:
    inserted: bool
    id: int | None
    reason: InsertReason | None = None


#: Prefijos de clave que habilitan el dedup por CONTENIDO ENTRE FUENTES (ver `insert_record`).
#: Solo identidades exactas: el Message-ID de un correo, el sha256 de un archivo y el id de un
#: mensaje de WhatsApp (`whatsapp:<chat>:<id>`: el mismo mensaje llega igual por dos cuentas del
#: usuario o por una cuenta re-creada; sin esto, re-vincular duplicaba todo el historial).
HIGH_CONFIDENCE_DEDUPE_PREFIXES = ("msgid:", "sha256:", "whatsapp:")


def insert_record(
    conn: Connection,
    user_id: int,
    source_id: int,
    record: SourceRecord,
    *,
    on_conflict: OnConflict = "ignore",
) -> InsertResult:
    """Insert one record. Idempotent via ON CONFLICT on (source_id, external_id).
    Validates source_id belongs to user_id; raises ValueError otherwise.
    Writes dedupe_keys after a successful insert.

    `on_conflict="replace"` (fuentes cuyo registro es la versión VIGENTE de algo editable,
    p. ej. notas): si el external_id ya existe y el payload cambió, se reemplazan payload y
    occurred_at y se devuelve `reason="updated"` con el id; el caller invalida las etapas
    (`invalidate_for_reprocess`). Payload idéntico → `duplicate` sin tocar nada.
    """
    owner = conn.execute(
        text("SELECT user_id FROM sources WHERE id = :sid"),
        {"sid": source_id},
    ).scalar()
    if owner is None:
        raise ValueError(f"source_id {source_id} does not exist")
    if owner != user_id:
        raise ValueError(f"source_id {source_id} does not belong to user {user_id}")

    # Dedup por CONTENIDO (antes de insertar): si una clave de ALTA CONFIANZA ya existe para
    # este usuario, es la MISMA cosa llegando por otra carpeta/cuenta/fuente → se rechaza como
    # duplicado sin crear una segunda fila.
    #   `msgid:`  — Message-ID de un correo: el mismo mensaje por dos cuentas o carpetas.
    #   `sha256:` — hash del ARCHIVO: el mismo documento entregado por dos vías distintas (se lo
    #               mandaste al bot y además lo subiste por la web). Es una igualdad exacta de
    #               bytes, así que no hay falso positivo posible.
    # El resto de claves NO abre este gate, para acotar el riesgo (telegram/social usan el
    # external_id como clave y ya quedan cubiertos por UNIQUE(source_id, external_id)).
    content_keys = [k for k in record.dedupe_keys if k.startswith(HIGH_CONFIDENCE_DEDUPE_PREFIXES)]
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

    if on_conflict == "replace":
        return _insert_or_replace(conn, user_id, source_id, record, payload_json)

    row = conn.execute(
        text(
            """
            INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload, code_version)
            VALUES (:uid, :sid, :eid, :occ, CAST(:payload AS JSONB), :ver)
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
            "ver": code_version(),  # con qué parser/ingestor entró (sello, ver memex.version)
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


def _insert_or_replace(
    conn: Connection, user_id: int, source_id: int, record: SourceRecord, payload_json: str
) -> InsertResult:
    row = conn.execute(
        text(
            """
            INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload, code_version)
            VALUES (:uid, :sid, :eid, :occ, CAST(:payload AS JSONB), :ver)
            ON CONFLICT (source_id, external_id) DO UPDATE SET
                payload = EXCLUDED.payload,
                occurred_at = EXCLUDED.occurred_at,
                code_version = EXCLUDED.code_version
            WHERE inbox.payload IS DISTINCT FROM EXCLUDED.payload
            RETURNING id, (xmax = 0) AS inserted
            """
        ),
        {
            "uid": user_id,
            "sid": source_id,
            "eid": record.external_id,
            "occ": record.occurred_at,
            "payload": payload_json,
            "ver": code_version(),
        },
    ).first()
    if row is None:
        return InsertResult(inserted=False, id=None, reason="duplicate")
    inbox_id, inserted = int(row[0]), bool(row[1])
    if not inserted:
        return InsertResult(inserted=False, id=inbox_id, reason="updated")
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


def invalidate_for_reprocess(conn: Connection, user_id: int, inbox_ids: list[int]) -> int:
    """Un registro cuyo contenido CAMBIÓ vuelve a la cola de extracción: se borran sus filas de
    `module_extractions` (el work-set toma «pendiente» = sin fila por módulo) y se resetea el estado
    de procesamiento. Lo ya consolidado (eventos, identidades, evidencia de asuntos) no se toca acá:
    la re-extracción dedupea contra lo existente y asuntos olvida su decisión por su lado."""
    if not inbox_ids:
        return 0
    conn.execute(
        text("DELETE FROM module_extractions WHERE inbox_id = ANY(:ids)"),
        {"ids": inbox_ids},
    )
    n = conn.execute(
        text(
            "UPDATE inbox SET processed_at = NULL, process_error = NULL, attempts = 0 "
            "WHERE user_id = :uid AND id = ANY(:ids)"
        ),
        {"uid": user_id, "ids": inbox_ids},
    ).rowcount
    return int(n)


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
