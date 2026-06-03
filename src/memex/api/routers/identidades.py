"""Router del módulo `identidades` para el dashboard (sección /directorio).

Expone el directorio de personas (sync de Google Contacts), la lista de organizaciones/productos/
agentes de interés (con CRUD — a diferencia del router read-only de calendar, la lista la edita el
usuario), las menciones extraídas con su resolución, y la observabilidad del sync (cuentas +
corridas). Calca el patrón de `finance.py`/`calendar.py`: `connection()` + SQL crudo +
`.mappings()`, paginación por cursor, scoping por `user_id`.

`POST /sync` dispara una corrida de sync server-side (fetch-server-side); NO expone el token (las
cuentas solo cruzan `account_id` del vault + `sync_token_present`).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import (
    IdentityMentionList,
    IdentityOrgCreate,
    IdentityOrgDetail,
    IdentityOrgList,
    IdentityOrgRow,
    IdentityOrgUpdate,
    IdentityPersonDetail,
    IdentityPersonList,
    IdentityPersonOrgCreate,
    IdentityPersonRow,
    IdentityPersonUpdate,
    IdentityProviderAccountList,
    IdentitySyncRequest,
    IdentitySyncResult,
    IdentitySyncRunList,
)
from memex.db import connection
from memex.logging import get_logger
from memex.modules.identidades.sync import run_sync

router = APIRouter(prefix="/identidades", tags=["identidades"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.identidades")

_ORG_KINDS = frozenset({"organizacion", "producto", "agente"})


def _person_row(r: Any) -> dict[str, Any]:
    meta = r["metadata"] if isinstance(r["metadata"], dict) else {}
    return {
        "id": int(r["id"]),
        "display_name": r["display_name"],
        "given_name": r["given_name"],
        "family_name": r["family_name"],
        "emails": list(r["emails"] or []),
        "phones": list(r["phones"] or []),
        "org_name": r["org_name"],
        "role": r["role"],
        "source": r["source"],
        "interest": bool(r["interest"]),
        "provider": r["provider"],
        "photo_url": r["photo_url"],
        "deleted": bool(meta.get("deleted")),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def _org_row(r: Any) -> dict[str, Any]:
    return {
        "id": int(r["id"]),
        "name": r["name"],
        "kind": r["kind"],
        "aliases": list(r["aliases"] or []),
        "domains": list(r["domains"] or []),
        "interest": bool(r["interest"]),
        "description": r["description"],
        "source": r["source"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def _mention_row(r: Any) -> dict[str, Any]:
    conf = r["confidence"]
    return {
        "id": int(r["id"]),
        "source_inbox_ids": list(r["source_inbox_ids"] or []),
        "evidence": r["evidence"],
        "mentioned_name": r["mentioned_name"],
        "mentioned_kind": r["mentioned_kind"],
        "email": r["email"],
        "handle": r["handle"],
        "org_hint": r["org_hint"],
        "role_hint": r["role_hint"],
        "confidence": float(conf) if conf is not None else None,
        "resolved_kind": r["resolved_kind"],
        "resolved_person_id": r["resolved_person_id"],
        "resolved_org_id": r["resolved_org_id"],
        "resolution_method": r["resolution_method"],
        "created_at": r["created_at"],
    }


_PERSON_COLS = (
    "id, display_name, given_name, family_name, emails, phones, org_name, role, source, "
    "interest, provider, photo_url, metadata, created_at, updated_at"
)
_ORG_COLS = (
    "id, name, kind, aliases, domains, interest, description, source, created_at, updated_at"
)
_MENTION_COLS = (
    "id, source_inbox_ids, evidence, mentioned_name, mentioned_kind, email, handle, org_hint, "
    "role_hint, confidence, resolved_kind, resolved_person_id, resolved_org_id, resolution_method, "
    "created_at"
)


def _prefixed(cols: str, alias: str) -> str:
    """Prefija cada columna con `alias.` para los SELECT con JOIN (donde `id`/`user_id` etc. son
    ambiguos entre tablas). El label del resultado sigue siendo la columna sin prefijo."""
    return ", ".join(f"{alias}.{c.strip()}" for c in cols.split(","))


# ---- Personas -------------------------------------------------------------------------------- #


@router.get("/persons", response_model=IdentityPersonList)
async def list_persons(
    user_id: UserID,
    q: str | None = Query(default=None, description="Busca en display_name / emails."),
    org_id: int | None = Query(default=None, description="Filtra por org asociada."),
    interest: bool | None = Query(default=None, description="true=interés, false=Detectadas."),
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    cursor: int | None = Query(default=None, description="id > cursor for pagination"),
) -> dict[str, Any]:
    """Directorio de personas del usuario, paginado por cursor ascendente."""
    where = ["p.user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if cursor is not None:
        where.append("p.id > :cur")
        params["cur"] = cursor
    if q:
        where.append("(p.display_name ILIKE :q OR array_to_string(p.emails, ' ') ILIKE :q)")
        params["q"] = f"%{q}%"
    if interest is not None:
        where.append("p.interest = :interest")
        params["interest"] = interest
    join = ""
    if org_id is not None:
        join = "JOIN mod_identidades_person_orgs po ON po.person_id = p.id AND po.org_id = :oid"
        params["oid"] = org_id
    sql = f"""
        SELECT {_prefixed(_PERSON_COLS, "p")} FROM mod_identidades_persons p
        {join}
        WHERE {" AND ".join(where)}
        ORDER BY p.id
        LIMIT :limit
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    items = [_person_row(r) for r in rows]
    next_cursor = items[-1]["id"] if len(items) == limit else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/persons/{person_id}", response_model=IdentityPersonDetail)
async def get_person(person_id: int, user_id: UserID) -> dict[str, Any]:
    """Una persona + sus orgs asociadas + sus menciones recientes."""
    with connection() as conn:
        prow = (
            conn.execute(
                text(
                    f"SELECT {_PERSON_COLS} FROM mod_identidades_persons "
                    "WHERE id = :id AND user_id = :uid"
                ),
                {"id": person_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
        if prow is None:
            raise HTTPException(status_code=404, detail="person not found")
        orgs = (
            conn.execute(
                text(
                    f"""
                    SELECT {_prefixed(_ORG_COLS, "o")} FROM mod_identidades_orgs o
                    JOIN mod_identidades_person_orgs po ON po.org_id = o.id
                    WHERE po.person_id = :id AND o.user_id = :uid ORDER BY o.name
                    """
                ),
                {"id": person_id, "uid": user_id},
            )
            .mappings()
            .all()
        )
        mentions = (
            conn.execute(
                text(
                    f"SELECT {_MENTION_COLS} FROM mod_identidades_mentions "
                    "WHERE resolved_person_id = :id AND user_id = :uid ORDER BY id DESC LIMIT 50"
                ),
                {"id": person_id, "uid": user_id},
            )
            .mappings()
            .all()
        )
    return {
        "person": _person_row(prow),
        "orgs": [_org_row(o) for o in orgs],
        "mentions": [_mention_row(m) for m in mentions],
    }


@router.patch("/persons/{person_id}", response_model=IdentityPersonRow)
async def update_person(
    person_id: int, user_id: UserID, body: IdentityPersonUpdate
) -> dict[str, Any]:
    """Actualiza una persona (p. ej. 'promover' = `interest=true`)."""
    sets: list[str] = []
    params: dict[str, Any] = {"id": person_id, "uid": user_id}
    if body.interest is not None:
        sets.append("interest = :interest")
        params["interest"] = body.interest
    if body.display_name is not None:
        sets.append("display_name = :dn")
        params["dn"] = body.display_name
    if body.role is not None:
        sets.append("role = :role")
        params["role"] = body.role
    if body.notes is not None:
        sets.append("notes = :notes")
        params["notes"] = body.notes
    if not sets:
        raise HTTPException(status_code=422, detail="sin campos para actualizar")
    sets.append("updated_at = NOW()")
    with connection() as conn:
        row = (
            conn.execute(
                text(
                    f"UPDATE mod_identidades_persons SET {', '.join(sets)} "
                    f"WHERE id = :id AND user_id = :uid RETURNING {_PERSON_COLS}"
                ),
                params,
            )
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(status_code=404, detail="person not found")
    return _person_row(row)


# ---- Organizaciones / lista de interés (CRUD) ------------------------------------------------ #


@router.get("/orgs", response_model=IdentityOrgList)
async def list_orgs(
    user_id: UserID,
    q: str | None = Query(default=None, description="Busca en name / aliases."),
    interest: bool | None = Query(default=None, description="Filtra por lista de interés."),
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    cursor: int | None = Query(default=None),
) -> dict[str, Any]:
    where = ["user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if cursor is not None:
        where.append("id > :cur")
        params["cur"] = cursor
    if q:
        where.append("(name ILIKE :q OR array_to_string(aliases, ' ') ILIKE :q)")
        params["q"] = f"%{q}%"
    if interest is not None:
        where.append("interest = :interest")
        params["interest"] = interest
    sql = f"""
        SELECT {_ORG_COLS} FROM mod_identidades_orgs
        WHERE {" AND ".join(where)} ORDER BY id LIMIT :limit
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    items = [_org_row(r) for r in rows]
    next_cursor = items[-1]["id"] if len(items) == limit else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/orgs/{org_id}", response_model=IdentityOrgDetail)
async def get_org(org_id: int, user_id: UserID) -> dict[str, Any]:
    """Una org + sus personas miembro + sus menciones recientes."""
    with connection() as conn:
        orow = (
            conn.execute(
                text(f"SELECT {_ORG_COLS} FROM mod_identidades_orgs WHERE id=:id AND user_id=:uid"),
                {"id": org_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
        if orow is None:
            raise HTTPException(status_code=404, detail="org not found")
        members = (
            conn.execute(
                text(
                    f"""
                    SELECT {_prefixed(_PERSON_COLS, "p")} FROM mod_identidades_persons p
                    JOIN mod_identidades_person_orgs po ON po.person_id = p.id
                    WHERE po.org_id = :id AND p.user_id = :uid ORDER BY p.display_name
                    """
                ),
                {"id": org_id, "uid": user_id},
            )
            .mappings()
            .all()
        )
        mentions = (
            conn.execute(
                text(
                    f"SELECT {_MENTION_COLS} FROM mod_identidades_mentions "
                    "WHERE resolved_org_id = :id AND user_id = :uid ORDER BY id DESC LIMIT 50"
                ),
                {"id": org_id, "uid": user_id},
            )
            .mappings()
            .all()
        )
    return {
        "org": _org_row(orow),
        "members": [_person_row(p) for p in members],
        "mentions": [_mention_row(m) for m in mentions],
    }


@router.post("/orgs", response_model=IdentityOrgRow)
async def create_org(user_id: UserID, body: IdentityOrgCreate) -> dict[str, Any]:
    if body.kind not in _ORG_KINDS:
        raise HTTPException(status_code=422, detail=f"kind inválido: {body.kind!r}")
    with connection() as conn:
        row = (
            conn.execute(
                text(
                    f"""
                    INSERT INTO mod_identidades_orgs
                      (user_id, name, kind, aliases, domains, description, interest, source)
                    VALUES (:uid, :name, :kind, :aliases, :domains, :desc, :interest, 'manual')
                    ON CONFLICT (user_id, name) DO UPDATE SET
                      kind = EXCLUDED.kind, aliases = EXCLUDED.aliases, domains = EXCLUDED.domains,
                      description = EXCLUDED.description, interest = EXCLUDED.interest,
                      updated_at = NOW()
                    RETURNING {_ORG_COLS}
                    """
                ),
                {
                    "uid": user_id,
                    "name": body.name,
                    "kind": body.kind,
                    "aliases": [a.strip() for a in body.aliases if a.strip()],
                    "domains": [d.strip().lower() for d in body.domains if d.strip()],
                    "desc": body.description,
                    "interest": body.interest,
                },
            )
            .mappings()
            .one()
        )
    return _org_row(row)


@router.patch("/orgs/{org_id}", response_model=IdentityOrgRow)
async def update_org(org_id: int, user_id: UserID, body: IdentityOrgUpdate) -> dict[str, Any]:
    if body.kind is not None and body.kind not in _ORG_KINDS:
        raise HTTPException(status_code=422, detail=f"kind inválido: {body.kind!r}")
    sets: list[str] = []
    params: dict[str, Any] = {"id": org_id, "uid": user_id}
    if body.name is not None:
        sets.append("name = :name")
        params["name"] = body.name
    if body.kind is not None:
        sets.append("kind = :kind")
        params["kind"] = body.kind
    if body.aliases is not None:
        sets.append("aliases = :aliases")
        params["aliases"] = [a.strip() for a in body.aliases if a.strip()]
    if body.domains is not None:
        sets.append("domains = :domains")
        params["domains"] = [d.strip().lower() for d in body.domains if d.strip()]
    if body.description is not None:
        sets.append("description = :desc")
        params["desc"] = body.description
    if body.interest is not None:
        sets.append("interest = :interest")
        params["interest"] = body.interest
    if not sets:
        raise HTTPException(status_code=422, detail="sin campos para actualizar")
    sets.append("updated_at = NOW()")
    with connection() as conn:
        row = (
            conn.execute(
                text(
                    f"UPDATE mod_identidades_orgs SET {', '.join(sets)} "
                    f"WHERE id = :id AND user_id = :uid RETURNING {_ORG_COLS}"
                ),
                params,
            )
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(status_code=404, detail="org not found")
    return _org_row(row)


@router.delete("/orgs/{org_id}")
async def delete_org(org_id: int, user_id: UserID) -> dict[str, bool]:
    with connection() as conn:
        res = conn.execute(
            text("DELETE FROM mod_identidades_orgs WHERE id = :id AND user_id = :uid"),
            {"id": org_id, "uid": user_id},
        )
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="org not found")
    return {"deleted": True}


@router.post("/persons/{person_id}/orgs", response_model=IdentityOrgDetail)
async def associate_person_org(
    person_id: int, user_id: UserID, body: IdentityPersonOrgCreate
) -> dict[str, Any]:
    """Asocia una persona con una org (idempotente)."""
    with connection() as conn:
        owns_person = conn.execute(
            text("SELECT 1 FROM mod_identidades_persons WHERE id=:p AND user_id=:uid"),
            {"p": person_id, "uid": user_id},
        ).first()
        owns_org = conn.execute(
            text("SELECT 1 FROM mod_identidades_orgs WHERE id=:o AND user_id=:uid"),
            {"o": body.org_id, "uid": user_id},
        ).first()
        if owns_person is None or owns_org is None:
            raise HTTPException(status_code=404, detail="person or org not found")
        conn.execute(
            text(
                """
                INSERT INTO mod_identidades_person_orgs (user_id, person_id, org_id, role, source)
                VALUES (:uid, :p, :o, :role, 'manual')
                ON CONFLICT (person_id, org_id) DO UPDATE SET role = EXCLUDED.role
                """
            ),
            {"uid": user_id, "p": person_id, "o": body.org_id, "role": body.role},
        )
    return await get_org(body.org_id, user_id)


# ---- Menciones ------------------------------------------------------------------------------- #


@router.get("/mentions", response_model=IdentityMentionList)
async def list_mentions(
    user_id: UserID,
    resolved: bool | None = Query(default=None, description="Filtra resueltas/sin resolver."),
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    cursor: int | None = Query(default=None, description="id < cursor (más recientes primero)"),
) -> dict[str, Any]:
    where = ["user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if cursor is not None:
        where.append("id < :cur")
        params["cur"] = cursor
    if resolved is True:
        where.append("resolved_kind IS NOT NULL")
    elif resolved is False:
        where.append("resolved_kind IS NULL")
    sql = f"""
        SELECT {_MENTION_COLS} FROM mod_identidades_mentions
        WHERE {" AND ".join(where)} ORDER BY id DESC LIMIT :limit
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    items = [_mention_row(r) for r in rows]
    next_cursor = items[-1]["id"] if len(items) == limit else None
    return {"items": items, "next_cursor": next_cursor}


# ---- Sync (cuentas + corridas + trigger) ----------------------------------------------------- #


@router.get("/provider-accounts", response_model=IdentityProviderAccountList)
async def list_provider_accounts(user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT id, provider, account_label, account_id, enabled, last_sync_at,
                           sync_token IS NOT NULL AS sync_token_present
                    FROM mod_identidades_provider_accounts
                    WHERE user_id = :uid ORDER BY id
                    """
                ),
                {"uid": user_id},
            )
            .mappings()
            .all()
        )
    return {"items": [dict(r) for r in rows]}


@router.get("/sync-runs", response_model=IdentitySyncRunList)
async def list_sync_runs(
    user_id: UserID,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: int | None = Query(default=None, description="id < cursor (más recientes primero)"),
) -> dict[str, Any]:
    where = ["user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if cursor is not None:
        where.append("id < :cur")
        params["cur"] = cursor
    sql = f"""
        SELECT id, provider_account_id, pulled, created, modified, deleted, unchanged, errors,
               status, started_at, finished_at
        FROM mod_identidades_sync_runs
        WHERE {" AND ".join(where)} ORDER BY id DESC LIMIT :limit
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    items = [dict(r) for r in rows]
    next_cursor = int(items[-1]["id"]) if len(items) == limit else None
    return {"items": items, "next_cursor": next_cursor}


@router.post("/sync", response_model=IdentitySyncResult)
async def trigger_sync(user_id: UserID, body: IdentitySyncRequest) -> dict[str, Any]:
    """Dispara una corrida de sync (ingress) server-side para una cuenta de proveedor."""
    _log.info("identidades.api.sync", user_id=user_id, account_id=body.account_id, full=body.full)
    stats = await run_sync(user_id, body.account_id, full=body.full)
    return {
        "pulled": stats.pulled,
        "created": stats.created,
        "modified": stats.modified,
        "deleted": stats.deleted,
        "unchanged": stats.unchanged,
        "errors": stats.errors,
    }
