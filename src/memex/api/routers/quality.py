"""Sistema de calidad: relevancia por remitente (lectura determinista, read-only).

`GET /quality/senders` — remitentes rankeados por relevancia para domar ruido (ruido primero). Sin
LLM, sin mutación, acotado al dueño. La marca manual, la acción "no procesar"/descartar y la cola de
candidatos llegan en fases posteriores; esta es la vista que las habilita.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from memex.api.auth import current_user_id
from memex.api.schemas import SenderRelevanceList
from memex.db import connection
from memex.logging import get_logger
from memex.quality.relevance import senders_by_relevance

router = APIRouter(prefix="/quality", tags=["quality"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.quality")


@router.get("/senders", response_model=SenderRelevanceList)
async def list_sender_relevance(
    user_id: UserID,
    source_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict[str, Any]:
    """Remitentes rankeados por relevancia (ruido primero). `source_id` acota a una fuente."""
    with connection() as conn:
        items = senders_by_relevance(conn, user_id=user_id, limit=limit, source_id=source_id)
    _log.info("quality.senders", user_id=user_id, rows=len(items), source_id=source_id)
    return {"items": items}
