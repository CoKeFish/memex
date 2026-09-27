"""Sirve el blob original de un adjunto (`media_assets`) desde MinIO.

La DB guarda solo la REFERENCIA (object_key + bucket); el blob vive en MinIO. Este endpoint lo
baja y lo devuelve para previsualizar (imagen/PDF inline) o descargar (`?download=true`). Auth por
dueño: el asset tiene que ser del `user_id` del request (404 si no, sin filtrar existencia entre
usuarios). Reusa el `ObjectStore` memoizado del borde de ingest (`get_object_store`).
"""

from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.object_store import get_object_store
from memex.api.schemas import MediaList
from memex.db import connection
from memex.logging import get_logger
from memex.storage import StorageError

router = APIRouter(prefix="/media", tags=["media"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.media")

OcrStatusFilter = Literal["pending", "ok", "error", "skipped", "all"]


@router.get("", response_model=MediaList)
async def list_media(
    user_id: UserID,
    ocr_status: Annotated[OcrStatusFilter, Query()] = "all",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: Annotated[int | None, Query(description="id < cursor (más nuevos primero)")] = None,
) -> dict[str, Any]:
    """Lista los media_assets del usuario (más nuevos primero) con contexto del mensaje.

    Para el monitor /ocr: estado OCR + texto + referencia al adjunto y al mensaje de origen. El blob
    NO viaja acá (se sirve por `GET /media/{id}`). Owner-scoped por `user_id`. Paginación keyset por
    `id` descendente (`cursor` = trae los de id menor).
    """
    where = ["m.user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if ocr_status != "all":
        where.append("m.ocr_status = :st")
        params["st"] = ocr_status
    if cursor is not None:
        where.append("m.id < :cur")
        params["cur"] = cursor
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    f"""
                    SELECT m.id, m.sha256, m.content_type, m.filename, m.extension, m.size_bytes,
                           m.ocr_status, m.ocr_model, m.ocr_text, m.ocr_error, m.ocr_attempts,
                           m.ocr_done_at, m.inbox_id,
                           i.payload->>'subject' AS subject,
                           i.occurred_at AS occurred_at
                    FROM media_assets m
                    JOIN inbox i ON i.id = m.inbox_id
                    WHERE {" AND ".join(where)}
                    ORDER BY m.id DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
            .mappings()
            .all()
        )
    items = [dict(r) for r in rows]
    next_cursor = items[-1]["id"] if len(items) == limit else None
    return {"items": items, "next_cursor": next_cursor}


def _content_disposition(filename: str | None, *, download: bool) -> str:
    """Header `Content-Disposition`: `inline` para previsualizar, `attachment` para descargar.

    Codifica el nombre con RFC 5987 (`filename*`) para soportar acentos/no-ASCII sin romper.
    """
    disp = "attachment" if download else "inline"
    if not filename:
        return disp
    return f"{disp}; filename*=UTF-8''{quote(filename)}"


@router.get("/{media_id}")
async def get_media(
    media_id: int,
    user_id: UserID,
    download: Annotated[bool, Query()] = False,
) -> Response:
    with connection() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT object_key, content_type, filename
                    FROM media_assets
                    WHERE id = :id AND user_id = :uid
                    """
                ),
                {"id": media_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
    if not row:
        raise HTTPException(status_code=404, detail="not found")

    try:
        data = get_object_store().get(str(row["object_key"]))
    except StorageError as e:
        # El asset existe en la DB pero el blob no se pudo bajar (key faltante, MinIO caído).
        _log.error("media.fetch_failed", media_id=media_id, exc_msg=str(e))
        raise HTTPException(status_code=502, detail="no se pudo leer el adjunto") from e

    return Response(
        content=data,
        media_type=str(row["content_type"]),
        headers={"Content-Disposition": _content_disposition(row["filename"], download=download)},
    )
