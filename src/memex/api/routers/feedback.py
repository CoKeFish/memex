"""Listado y gestión del feedback manual acumulado (inspección/calibración offline).

La captura por-mensaje vive en `POST /inbox/{id}/feedback`; acá se LISTA (GET) y se mueve el estado
(POST `/{inbox_id}/status`) desde la vista "Calidad y precisión". Acotado al dueño.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from memex.api.auth import current_user_id
from memex.api.schemas import FeedbackInfo, FeedbackList, FeedbackStatusUpdate
from memex.core.feedback import InvalidFeedbackError, list_feedback, set_feedback_status
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


@router.post("/{inbox_id}/status", response_model=FeedbackInfo)
async def set_feedback_status_endpoint(
    inbox_id: int, user_id: UserID, body: FeedbackStatusUpdate
) -> dict[str, Any]:
    """Mueve el estado del feedback de un mensaje (open/reviewed/dismissed). 404 si no existe."""
    try:
        with connection() as conn:
            row = set_feedback_status(conn, user_id=user_id, inbox_id=inbox_id, status=body.status)
    except InvalidFeedbackError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="feedback no encontrado")
    return row
