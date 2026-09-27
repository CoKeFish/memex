from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from memex.db import connection
from memex.version import code_version

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/version")
async def version() -> dict[str, str | None]:
    """Versión del código que corre (`memex.version`, sello de las filas) + revisión del esquema.
    `code_version == "unknown"` = imagen construida sin el build-arg (ver scripts/build_api.ps1).
    """
    schema: str | None
    try:
        with connection() as conn:
            schema = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar()
    except Exception:
        schema = None
    return {"code_version": code_version(), "schema_revision": schema}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    try:
        with connection() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        raise HTTPException(status_code=503, detail={"db": "down", "error": str(e)}) from e
    return {"db": "ok"}
