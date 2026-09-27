"""Prompts: ver, editar (con confirmación en la UI), restablecer al código y volver a una versión.

`GET /prompts` lista todos (sin historial); `GET /prompts/{key}` trae default, override, texto
vigente, avisos y versiones; `PUT` guarda (crea versión y aplica en el acto); `DELETE` restablece
al código; `POST /{key}/revert/{version_id}` vuelve a una versión. Ver `memex.prompts`."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from memex.api.auth import current_user_id
from memex.db import connection
from memex.prompts import overrides
from memex.prompts.registry import PROMPTS

router = APIRouter(prefix="/prompts", tags=["prompts"])

UserID = Annotated[int, Depends(current_user_id)]


class PromptVersionOut(BaseModel):
    id: int
    note: str
    created_at: datetime
    chars: int
    text: str


class PromptSummary(BaseModel):
    key: str
    title: str
    group: str
    description: str
    consumer: str
    editable: bool
    has_override: bool
    note: str
    updated_at: datetime | None
    default_chars: int
    effective_chars: int


class PromptDetail(PromptSummary):
    default: str
    override: str | None
    effective: str
    warnings: list[str]
    versions: list[PromptVersionOut]


class PromptList(BaseModel):
    groups: list[str]
    items: list[PromptSummary]


class PromptSave(BaseModel):
    text: str = Field(min_length=1)
    note: str = ""


def _summary(v: overrides.PromptView) -> PromptSummary:
    return PromptSummary(
        key=v.key,
        title=v.title,
        group=v.group,
        description=v.description,
        consumer=v.consumer,
        editable=v.editable,
        has_override=v.override is not None,
        note=v.note,
        updated_at=v.updated_at,
        default_chars=len(v.default),
        effective_chars=len(v.effective),
    )


def _detail(v: overrides.PromptView) -> PromptDetail:
    base = _summary(v).model_dump()
    return PromptDetail(
        **base,
        default=v.default,
        override=v.override,
        effective=v.effective,
        warnings=list(v.warnings),
        versions=[
            PromptVersionOut(
                id=x.id, note=x.note, created_at=x.created_at, chars=len(x.text), text=x.text
            )
            for x in v.versions
        ],
    )


@router.get("", response_model=PromptList)
def list_prompts(user_id: UserID) -> PromptList:
    overrides.refresh_if_stale()
    with connection() as conn:
        views = overrides.list_views(conn)
    groups: list[str] = []
    for p in PROMPTS:
        if p.group not in groups:
            groups.append(p.group)
    return PromptList(groups=groups, items=[_summary(v) for v in views])


@router.get("/{key}", response_model=PromptDetail)
def get_prompt(key: str, user_id: UserID) -> PromptDetail:
    overrides.refresh_if_stale()
    try:
        with connection() as conn:
            v = overrides.view(conn, key)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _detail(v)


@router.put("/{key}", response_model=PromptDetail)
def save_prompt(key: str, body: PromptSave, user_id: UserID) -> PromptDetail:
    try:
        with connection() as conn:
            v = overrides.set_override(conn, key, body.text, note=body.note)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return _detail(v)


@router.delete("/{key}", response_model=PromptDetail)
def reset_prompt(key: str, user_id: UserID) -> PromptDetail:
    try:
        with connection() as conn:
            v = overrides.reset_override(conn, key)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _detail(v)


@router.post("/{key}/revert/{version_id}", response_model=PromptDetail)
def revert_prompt(key: str, version_id: int, user_id: UserID) -> PromptDetail:
    try:
        with connection() as conn:
            v = overrides.revert_to_version(conn, key, version_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return _detail(v)
