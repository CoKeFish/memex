"""Entregar un archivo a memex: lo guarda, le saca el texto y deja que los módulos lo lean.

Server-side (habla con la DB y con MinIO), así que vive acá y no bajo `ingestors/`. Reusa el MISMO
borde de ingesta que el resto (`ingest_one_record`): el archivo entra como un registro con su blob,
y de ahí en adelante es indistinguible de un adjunto que llegó por correo.

La fuente se crea sola la primera vez (una por canal de entrega: Telegram, consola, carpeta), para
que el usuario no tenga que configurar nada antes de mandar su primer documento.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.api.ingest_service import ingest_one_record
from memex.api.schemas import IngestRequest, MediaItem
from memex.core.media_types import DOCUMENT_CONTENT_TYPES, IMAGE_CONTENT_TYPES
from memex.core.payloads import DocumentPayload
from memex.logging import get_logger

_log = get_logger("memex.documentos.ingest")

#: Extensión → content-type. Solo lo que memex sabe leer; el resto se rechaza con un motivo claro
#: en vez de entrar y morir después en el OCR.
BY_EXTENSION: dict[str, str] = {
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
#: Tope por archivo (MiB). Configurable (`MEMEX_DOCUMENTOS_MAX_MB`): los extractos pesan < 1 MB,
#: pero
#: las diapositivas de un curso exportadas a PDF pesan 10-70 MB cada una (2026-09-13). El archivo
#: se lee entero en memoria (API y OCR): el tope es el límite de lo razonable para un solo usuario.
_DEFAULT_MAX_MB = 50


def _max_mb_from_env() -> int:
    raw = os.environ.get("MEMEX_DOCUMENTOS_MAX_MB", "").strip()
    if not raw:
        return _DEFAULT_MAX_MB
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_MB
    return value if value > 0 else _DEFAULT_MAX_MB


MAX_MB = _max_mb_from_env()


class DocumentoError(Exception):
    """No se pudo entregar el documento (tipo no soportado, vacío, demasiado grande…)."""


@dataclass(frozen=True)
class Entrega:
    """Resultado de entregar UN documento."""

    inbox_id: int | None
    filename: str
    sha256: str
    #: True si ya estaba (mismo contenido entregado antes): no se duplica.
    duplicado: bool


def content_type_of(filename: str) -> str | None:
    ct = BY_EXTENSION.get(Path(filename).suffix.lower())
    if ct is None or ct not in (DOCUMENT_CONTENT_TYPES | IMAGE_CONTENT_TYPES):
        return None
    return ct


def ensure_source(conn: Connection, user_id: int, *, canal: str) -> int:
    """Fuente de documentos de un canal de entrega (`telegram`, `consola`, …). Se crea al vuelo.

    Una por canal: así `/carga` muestra por dónde entró cada documento y se puede apagar un canal
    sin tocar los otros.
    """
    nombre = f"documentos-{canal}"
    row = conn.execute(
        text("SELECT id FROM sources WHERE user_id = :uid AND name = :n"),
        {"uid": user_id, "n": nombre},
    ).first()
    if row is not None:
        return int(row[0])
    created = conn.execute(
        text(
            "INSERT INTO sources (user_id, name, type, config) "
            "VALUES (:uid, :n, 'documentos', '{}') RETURNING id"
        ),
        {"uid": user_id, "n": nombre},
    ).scalar_one()
    _log.info("documentos.source.created", user_id=user_id, source_id=int(created), canal=canal)
    return int(created)


def entregar_bytes(
    conn: Connection,
    user_id: int,
    *,
    data: bytes,
    filename: str,
    canal: str,
    nota: str = "",
    occurred_at: datetime | None = None,
) -> Entrega:
    """Entrega UN archivo ya leído. Idempotente por contenido: el `external_id` es su sha256, así
    que mandar dos veces el mismo documento (o renombrado) no lo duplica."""
    if not data:
        raise DocumentoError(f"{filename}: el archivo está vacío.")
    if len(data) > MAX_MB * 1024 * 1024:
        raise DocumentoError(f"{filename}: pesa más de {MAX_MB} MB.")
    content_type = content_type_of(filename)
    if content_type is None:
        soportadas = ", ".join(sorted(BY_EXTENSION))
        raise DocumentoError(f"{filename}: tipo no soportado (acepta {soportadas}).")

    sha = hashlib.sha256(data).hexdigest()
    payload = DocumentPayload(
        filename=filename,
        content_type=content_type,
        size=len(data),
        sha256=sha,
        captured_from=canal,
        document_date=occurred_at,
        body_text=nota,
    )
    req = IngestRequest(
        source_id=ensure_source(conn, user_id, canal=canal),
        external_id=f"documento:{sha}",
        occurred_at=occurred_at or datetime.now(UTC),
        payload=payload.model_dump(mode="json", by_alias=True),
        # Clave de contenido: el MISMO archivo entregado por otra vía (bot, web, consola, carpeta)
        # cae en fuentes distintas, así que el UNIQUE(source_id, external_id) no lo detectaría.
        dedupe_keys=[f"sha256:{sha}"],
        media=[
            MediaItem(
                sha256=sha,
                content_type=content_type,
                filename=filename,
                size=len(data),
                data_b64=base64.b64encode(data).decode("ascii"),
            )
        ],
    )
    outcome = ingest_one_record(conn, user_id, req)
    _log.info(
        "documentos.entregado",
        user_id=user_id,
        filename=filename,
        canal=canal,
        inbox_id=outcome.id,
        inserted=outcome.inserted,
        reason=outcome.reason,
    )
    return Entrega(
        inbox_id=outcome.id,
        filename=filename,
        sha256=sha,
        duplicado=not outcome.inserted and outcome.reason == "duplicate",
    )


def entregar_archivo(
    conn: Connection,
    user_id: int,
    path: Path,
    *,
    canal: str = "consola",
    nota: str = "",
) -> Entrega:
    """Entrega un archivo del disco. La fecha del documento sale de su fecha de modificación."""
    if not path.is_file():
        raise DocumentoError(f"{path}: no existe.")
    stat = path.stat()
    return entregar_bytes(
        conn,
        user_id,
        data=path.read_bytes(),
        filename=path.name,
        canal=canal,
        nota=nota,
        occurred_at=datetime.fromtimestamp(stat.st_mtime, UTC),
    )
