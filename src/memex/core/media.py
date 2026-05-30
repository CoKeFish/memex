"""Persistencia de `media_assets` (la referencia a un blob en MinIO + estado de OCR).

El blob vive en object storage (lo sube el borde de ingest); acá se registra SOLO la referencia
(`object_key` + `bucket` + `sha256`) más la metadata y el estado inicial de OCR. Es el análogo
de `memex.core.inbox.insert_record`: SQL puro, sin red ni object storage.

Idempotente vía `ON CONFLICT (inbox_id, sha256) DO NOTHING`: re-ingestar el mismo mensaje (mismo
adjunto) no duplica la fila. El insert va en la MISMA tx que el inbox (atomicidad inbox↔media).
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import Connection, text

OcrStatus = Literal["pending", "ok", "error", "skipped"]

#: Tope de intentos de OCR por asset. El worker re-reclama filas en 'error' con menos de este
#: número de intentos (errores transitorios: red, 5xx, MinIO temporal); pasado el tope quedan
#: terminal-error. El gate de los worksets usa el MISMO umbral: un mensaje espera (no se resume/
#: extrae) mientras tenga media en estado NO-terminal — pending, o error aún reintentable.
MAX_OCR_ATTEMPTS = 3

#: Condición SQL "este media todavía NO está en estado terminal" (parametrizada por :ocrmax).
#: Terminal = ok | skipped | error con intentos agotados. Se usa en el gate de los worksets.
MEDIA_NOT_TERMINAL_SQL = (
    "(m.ocr_status = 'pending' OR (m.ocr_status = 'error' AND m.ocr_attempts < :ocrmax))"
)


def insert_media_asset(
    conn: Connection,
    *,
    user_id: int,
    inbox_id: int,
    sha256: str,
    object_key: str,
    bucket: str,
    content_type: str,
    size_bytes: int,
    filename: str | None,
    ocr_status: OcrStatus = "pending",
) -> None:
    """Inserta una fila en `media_assets` (idempotente por (inbox_id, sha256))."""
    conn.execute(
        text(
            """
            INSERT INTO media_assets
              (user_id, inbox_id, sha256, object_key, bucket,
               content_type, size_bytes, filename, ocr_status)
            VALUES
              (:uid, :iid, :sha, :key, :bucket,
               :ctype, :size, :filename, :status)
            ON CONFLICT (inbox_id, sha256) DO NOTHING
            """
        ),
        {
            "uid": user_id,
            "iid": inbox_id,
            "sha": sha256,
            "key": object_key,
            "bucket": bucket,
            "ctype": content_type,
            "size": size_bytes,
            "filename": filename,
            "status": ocr_status,
        },
    )
