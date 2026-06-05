"""Descarga de bytes de media de Telegram → `MediaBlob` (para MinIO + OCR).

El parser (`parser.py`) solo DETECTA media (`media_kind` + `media_caption`); acá
bajamos los bytes con Telethon (`tc.download_media`) y los empaquetamos como
`MediaBlob` para `SourceRecord.media`. Espejo funcional de
`social/_common.download_social_media`, pero Telegram NO tiene URLs: los bytes
salen del objeto `msg` vivo vía MTProto, así que la descarga ocurre donde `msg` +
cliente están en scope (el collect loop de polling/catchup y el handler live).

Decisión de alcance (dueño): bajamos fotos + video + documentos cuyo mime sea
imagen o PDF. Stickers / audio / voz se ignoran.

Best-effort y defensivo: NUNCA lanza. Un media que falla (descarga, tipo no
whitelisteado, supera el tope) se loggea y se saltea — no tumba el record, el
chat ni (en streaming) la conexión. Eventos structlog como literales estáticos
(ADR-007), espejo de los `social.media.*`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from memex.core.media_types import (
    IMAGE_CONTENT_TYPES,
    VIDEO_CONTENT_TYPES,
    make_media_blob,
    normalize_content_type,
)
from memex.core.source import MediaBlob

if TYPE_CHECKING:
    from memex.ingestors.telegram.client import TelegramClientWrapper

#: Documentos que bajamos: imágenes (sin comprimir / re-enviadas como archivo) + PDF
#: (recibos, facturas). NO ZIP ni otros — decisión explícita "imagen|PDF".
_DOCUMENT_CONTENT_TYPES = IMAGE_CONTENT_TYPES | {"application/pdf"}


def _accept_content_type(msg: Any, file: Any) -> str | None:
    """Content-type normalizado a bajar para este mensaje, o `None` si se ignora.

    El ORDEN importa: un sticker es un `Document` con mime `image/webp` —que está
    en `IMAGE_CONTENT_TYPES`— así que matchearía la rama `document`. Lo cortamos
    ANTES de evaluar document para no bajar stickers como si fueran imágenes.

    - sticker presente            → None (ignorado)
    - photo presente              → `image/jpeg` (Telegram re-encodea fotos a JPEG)
    - video presente              → mime si ∈ VIDEO_CONTENT_TYPES, else None
    - document presente           → mime si ∈ (imágenes | application/pdf), else None
    - sin mime / otro tipo        → None
    """
    if _get_attr(msg, "sticker", None) is not None:
        return None
    if _get_attr(msg, "photo", None) is not None:
        return "image/jpeg"
    if _get_attr(msg, "video", None) is not None:
        mime = normalize_content_type(_get_attr(file, "mime_type", None))
        return mime if mime in VIDEO_CONTENT_TYPES else None
    if _get_attr(msg, "document", None) is not None:
        mime = normalize_content_type(_get_attr(file, "mime_type", None))
        return mime if mime in _DOCUMENT_CONTENT_TYPES else None
    return None


async def download_message_media(
    msg: Any,
    *,
    tc: TelegramClientWrapper,
    max_image_bytes: int,
    max_video_bytes: int,
    log: Any,
) -> list[MediaBlob]:
    """Baja los bytes de la media de `msg` → 0 o 1 `MediaBlob`. NUNCA lanza.

    Un mensaje de Telegram lleva como mucho un media (los álbumes son mensajes
    separados, cada uno con el suyo), así que retornamos lista de 0 o 1 elemento
    para encajar con `SourceRecord.media`.

    Mejora sobre social (que baja-y-mide): Telethon expone `msg.file.size` ANTES
    de descargar, así que chequeamos el tope primero y evitamos traer videos
    enormes a memoria. El post-check con `len(data)` queda como red de seguridad
    (el `size` de fotos es aproximado — es el thumb más pesado).
    """
    file = _get_attr(msg, "file", None)
    if file is None:
        # Mensaje sin media descargable (texto, encuesta, geo, contacto). Sin log.
        return []

    content_type = _accept_content_type(msg, file)
    if content_type is None:
        log.info(
            "telegram.media.skipped",
            content_type=normalize_content_type(_get_attr(file, "mime_type", None)) or None,
        )
        return []

    is_video = _get_attr(msg, "video", None) is not None
    max_bytes = max_video_bytes if is_video else max_image_bytes

    size = _get_attr(file, "size", None)
    if isinstance(size, int) and size > max_bytes:
        log.warning(
            "telegram.media.too_large",
            content_type=content_type,
            size=size,
            max_bytes=max_bytes,
        )
        return []

    try:
        data = await tc.download_media(msg)
    except Exception as e:
        log.warning(
            "telegram.media.fetch_error",
            content_type=content_type,
            exc_type=type(e).__name__,
            exc_msg=str(e),
        )
        return []

    if not data:
        # `None` (media Empty/expirada) o `b""`.
        return []

    if len(data) > max_bytes:
        log.warning(
            "telegram.media.too_large",
            content_type=content_type,
            size=len(data),
            max_bytes=max_bytes,
        )
        return []

    blob = make_media_blob(
        data,
        content_type=content_type,
        filename=_get_attr(file, "name", None),
    )
    log.info(
        "telegram.media.downloaded",
        count=1,
        bytes=blob.size,
        content_type=content_type,
    )
    return [blob]


def _get_attr(obj: Any, name: str, default: Any) -> Any:
    """Defensive getattr — los objetos de Telethon a veces lanzan en campos ausentes."""
    try:
        return getattr(obj, name, default)
    except Exception:
        return default
