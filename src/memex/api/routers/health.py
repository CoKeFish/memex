from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from memex.db import connection

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    try:
        with connection() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        raise HTTPException(status_code=503, detail={"db": "down", "error": str(e)}) from e
    return {"db": "ok"}
