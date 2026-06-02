"""Whitelists de content-type de media + helper para construir `MediaBlob`.

Lado-INGESTOR (ADR-001): módulo puro — solo stdlib y `MediaBlob` de `core.source`,
SIN DB ni object storage. Lo comparten los ingestors que extraen bytes de adjuntos/
media para que el borde de ingest los suba a MinIO + OCR:

- IMAP: imágenes + PDF + ZIP (`MEDIA_CONTENT_TYPES`).
- Social (Apify): imágenes + video crudo (`SOCIAL_MEDIA_CONTENT_TYPES`).

Es DISTINTO de `core.media`, que es lado-servidor (persiste `media_assets`, toca DB).
La whitelist vive acá, en un solo lugar, para que IMAP y social no diverjan.

El worker `memex-ocr` solo procesa imágenes (y PDF por capa de texto); el video se
guarda como `media_asset` pero no se OCR-ea — viaja por el mismo canal `SourceRecord.media`.
"""

from __future__ import annotations

import base64
import hashlib

from memex.core.source import MediaBlob

#: Imágenes que extraemos para OCR. `image/jpg` es no-estándar pero algunos clientes lo emiten.
IMAGE_CONTENT_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
    }
)

#: PDF + ZIP: el worker `memex-ocr` los procesa (PDF por capa de texto + visión; ZIP
#: descomprimiendo y ruteando sus entradas).
DOCUMENT_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "application/zip",
        "application/x-zip-compressed",
        "application/zip-compressed",
        "multipart/x-zip",
    }
)

#: Video crudo (se guarda en MinIO; NO se OCR-ea). Lo bajan las sources sociales.
VIDEO_CONTENT_TYPES = frozenset(
    {
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "video/x-msvideo",
        "video/x-matroska",
        "video/mpeg",
        "video/3gpp",
    }
)

#: Adjuntos de email que extraemos: imágenes + PDF + ZIP (sin video).
MEDIA_CONTENT_TYPES = IMAGE_CONTENT_TYPES | DOCUMENT_CONTENT_TYPES

#: Media de posts sociales que bajamos: imágenes + video crudo.
SOCIAL_MEDIA_CONTENT_TYPES = IMAGE_CONTENT_TYPES | VIDEO_CONTENT_TYPES

#: Tope de bytes por adjunto/media (default 10 MiB). Arriba de esto se saltea + loguea.
DEFAULT_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def normalize_content_type(value: str | None) -> str:
    """Content-type en minúsculas y sin parámetros (`image/jpeg; charset=x` → `image/jpeg`)."""
    if not value:
        return ""
    return value.split(";", 1)[0].strip().lower()


def make_media_blob(data: bytes, *, content_type: str, filename: str | None) -> MediaBlob:
    """Construye un `MediaBlob` desde bytes: sha256 (content-address) + base64 (wire JSON)."""
    return MediaBlob(
        sha256=hashlib.sha256(data).hexdigest(),
        content_type=content_type,
        filename=filename,
        size=len(data),
        data_b64=base64.b64encode(data).decode("ascii"),
    )
