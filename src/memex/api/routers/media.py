"""Sirve el blob original de un adjunto (`media_assets`) desde MinIO.

La DB guarda solo la REFERENCIA (object_key + bucket); el blob vive en MinIO. Este endpoint lo
baja y lo devuelve para previsualizar (imagen/PDF inline) o descargar (`?download=true`). Auth por
dueño: el asset tiene que ser del `user_id` del request (404 si no, sin filtrar existencia entre
usuarios). Reusa el `ObjectStore` memoizado del borde de ingest (`get_object_store`).
"""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.object_store import get_object_store
from memex.db import connection
from memex.logging import get_logger
from memex.storage import StorageError

router = APIRouter(prefix="/media", tags=["media"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.media")


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
