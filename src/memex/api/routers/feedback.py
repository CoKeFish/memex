"""Listado del feedback manual acumulado (inspección/calibración offline).

La captura por-mensaje vive en `POST /inbox/{id}/feedback`; acá solo se LISTA (sin UI todavía — la
vista de gestión irá en "Calidad y precisión"). Acotado al dueño.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query

from memex.api.auth import current_user_id
from memex.api.schemas import FeedbackList
from memex.core.feedback import list_feedback
from memex.db import connection

router = APIRouter(prefix="/feedback", tags=["feedback"])

UserID = Annotated[int, Depends(current_user_id)]


@router.get("", response_model=FeedbackList)
async def list_feedback_endpoint(
    user_id: UserID,
    status: Annotated[Literal["open", "reviewed", "dismissed", "all"], Query()] = "open",
) -> dict[str, Any]:
    with connection() as conn:
        items = list_feedback(conn, user_id=user_id, status=None if status == "all" else status)
    return {"items": items}
