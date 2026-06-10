"""Router del módulo `identidades` para el dashboard (sección /directorio), modelo unificado.

Expone el directorio UNIFICADO de identidades (`mod_identidades`: personas u organizaciones) con
sus identificadores por-fuente, sedes, afiliaciones y menciones; el CRUD que edita el usuario; la
cola de candidatos de merge (zona gris del difuso) con confirmación/rechazo + un merge manual; y la
observabilidad del sync. Calca el patrón de `finance.py`/`calendar.py`: `connection()` + SQL crudo +
`.mappings()`, paginación por cursor, scoping por `user_id`.

`POST /sync` dispara una corrida server-side (fetch-server-side); NO expone el token.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import (
    IdentityAffiliateCreate,
    IdentityCreate,
    IdentityDetail,
    IdentityIdentifierCreate,
    IdentityIdentifierRow,
    IdentityList,
    IdentityMentionList,
    IdentityMergeCandidateList,
    IdentityMergeRequest,
    IdentityProviderAccountList,
    IdentityRow,
    IdentitySiteCreate,
    IdentitySiteRow,
    IdentitySyncRequest,
    IdentitySyncResult,
    IdentitySyncRunList,
    IdentityUpdate,
)
from memex.db import connection
from memex.llm.client import LLMQuotaError
from memex.logging import get_logger
from memex.modules.identidades.hierarchy import run_organize, would_create_cycle
from memex.modules.identidades.merge import merge_identities
from memex.modules.identidades.normalize import norm_identifier
from memex.modules.identidades.sync import run_sync

router = APIRouter(prefix="/identidades", tags=["identidades"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.identidades")

_KINDS = frozenset({"persona", "organizacion", "producto"})
_IDENTIFIER_KINDS = frozenset({"email", "phone", "handle", "domain", "url"})

_IDENTITY_COLS = (
    "id, kind, display_name, aliases, interest, source, notes, given_name, family_name, birthday, "
    "photo_url, parent_identity_id, metadata, created_at, updated_at"
)
_MENTION_COLS = (
    "id, source_inbox_ids, evidence, mentioned_name, mentioned_kind, email, handle, org_hint, "
    "role_hint, confidence, resolved_kind, resolved_identity_id, resolution_method, created_at"
)


def _identity_row(r: Any) -> dict[str, Any]:
    meta = r["metadata"] if isinstance(r["metadata"], dict) else {}
    return {
        "id": int(r["id"]),
        "kind": r["kind"],
        "display_name": r["display_name"],
        "aliases": list(r["aliases"] or []),
        "interest": bool(r["interest"]),
        "source": r["source"],
        "notes": r["notes"],
        "given_name": r["given_name"],
        "family_name": r["family_name"],
        "birthday": r["birthday"],
        "photo_url": r["photo_url"],
        "deleted": bool(meta.get("deleted")),
        "parent_id": r["parent_identity_id"],
        # parent_name / mention_count solo vienen en la lista (JOIN) y el detalle (enriquecido);
        # en los SELECT de una sola tabla quedan None/0.
        "parent_name": r.get("parent_name"),
        "mention_count": int(r["mention_count"]) if r.get("mention_count") is not None else 0,
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
        "resolved_identity_id": r["resolved_identity_id"],
        "resolution_method": r["resolution_method"],
        "created_at": r["created_at"],
    }


# ---- Directorio (lista + detalle + CRUD) ----------------------------------------------------- #


@router.get("", response_model=IdentityList)
async def list_identities(
    user_id: UserID,
    q: str | None = Query(default=None, description="Busca en display_name / aliases."),
    kind: str | None = Query(default=None, description="persona | organizacion | producto."),
    interest: bool | None = Query(default=None, description="true=interés, false=Detectadas."),
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    cursor: int | None = Query(default=None, description="id > cursor for pagination"),
) -> dict[str, Any]:
    """Directorio del usuario, paginado por cursor ascendente. Cada fila trae el padre
    (`parent_name`) y el `mention_count` por LEFT JOIN (no subquery por-fila)."""
    where = ["i.user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if cursor is not None:
        where.append("i.id > :cur")
        params["cur"] = cursor
    if kind in _KINDS:
        where.append("i.kind = :kind")
        params["kind"] = kind
    if interest is not None:
        where.append("i.interest = :interest")
        params["interest"] = interest
    if q:
        where.append("(i.display_name ILIKE :q OR array_to_string(i.aliases, ' ') ILIKE :q)")
        params["q"] = f"%{q}%"
    sql = (
        "SELECT i.id, i.kind, i.display_name, i.aliases, i.interest, i.source, i.notes, "
        "i.given_name, i.family_name, i.birthday, i.photo_url, i.parent_identity_id, i.metadata, "
        "i.created_at, i.updated_at, p.display_name AS parent_name, "
        "COALESCE(mc.n, 0) AS mention_count "
        "FROM mod_identidades i "
        "LEFT JOIN mod_identidades p ON p.id = i.parent_identity_id "
        "LEFT JOIN (SELECT resolved_identity_id, count(*) AS n FROM mod_identidades_mentions "
        "           WHERE user_id = :uid GROUP BY resolved_identity_id) mc "
        "       ON mc.resolved_identity_id = i.id "
        f"WHERE {' AND '.join(where)} ORDER BY i.id LIMIT :limit"
    )
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    items = [_identity_row(r) for r in rows]
    next_cursor = items[-1]["id"] if len(items) == limit else None
    return {"items": items, "next_cursor": next_cursor}


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
        where.append("resolved_identity_id IS NOT NULL")
    elif resolved is False:
        where.append("resolved_identity_id IS NULL")
    sql = (
        f"SELECT {_MENTION_COLS} FROM mod_identidades_mentions "
        f"WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT :limit"
    )
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    items = [_mention_row(r) for r in rows]
    next_cursor = items[-1]["id"] if len(items) == limit else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/merge-candidates", response_model=IdentityMergeCandidateList)
async def list_merge_candidates(user_id: UserID) -> dict[str, Any]:
    """Pares candidatos a fusionar (zona gris del difuso) pendientes de decisión."""
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT c.id, c.identity_a_id, c.identity_b_id, c.reason, c.score, c.status,
                           a.display_name AS a_name, a.kind AS kind, b.display_name AS b_name
                    FROM mod_identidades_merge_candidates c
                    JOIN mod_identidades a ON a.id = c.identity_a_id
                    JOIN mod_identidades b ON b.id = c.identity_b_id
                    WHERE c.user_id = :uid AND c.status = 'candidate'
                    ORDER BY c.score DESC NULLS LAST, c.id
                    """
                ),
                {"uid": user_id},
            )
            .mappings()
            .all()
        )
    items = [
        {
            "id": int(r["id"]),
            "identity_a_id": int(r["identity_a_id"]),
            "identity_b_id": int(r["identity_b_id"]),
            "a_name": r["a_name"],
            "b_name": r["b_name"],
            "kind": r["kind"],
            "reason": r["reason"],
            "score": float(r["score"]) if r["score"] is not None else None,
            "status": r["status"],
        }
        for r in rows
    ]
    return {"items": items}


@router.post("/merge", response_model=IdentityRow)
async def merge(user_id: UserID, body: IdentityMergeRequest) -> dict[str, Any]:
    """Fusiona dos identidades a mano (la absorbida desaparece). Devuelve la superviviente."""
    with connection() as conn:
        if not merge_identities(conn, user_id, body.survivor_id, body.absorbed_id):
            raise HTTPException(status_code=404, detail="no encontradas o de distinto tipo")
        row = (
            conn.execute(
                text(f"SELECT {_IDENTITY_COLS} FROM mod_identidades WHERE id=:id AND user_id=:uid"),
                {"id": body.survivor_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(status_code=404, detail="superviviente no encontrada")
    return _identity_row(row)


@router.post("/merge-candidates/{cand_id}/confirm", response_model=IdentityRow)
async def confirm_merge_candidate(cand_id: int, user_id: UserID) -> dict[str, Any]:
    """Confirma un candidato: fusiona el par (superviviente = id menor). El candidato cae por FK."""
    with connection() as conn:
        cand = conn.execute(
            text(
                "SELECT identity_a_id, identity_b_id FROM mod_identidades_merge_candidates "
                "WHERE id = :id AND user_id = :uid AND status = 'candidate'"
            ),
            {"id": cand_id, "uid": user_id},
        ).first()
        if cand is None:
            raise HTTPException(status_code=404, detail="candidato no encontrado")
        survivor, absorbed = sorted((int(cand[0]), int(cand[1])))
        merge_identities(conn, user_id, survivor, absorbed)
        row = (
            conn.execute(
                text(f"SELECT {_IDENTITY_COLS} FROM mod_identidades WHERE id=:id AND user_id=:uid"),
                {"id": survivor, "uid": user_id},
            )
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(status_code=404, detail="superviviente no encontrada")
    return _identity_row(row)


@router.post("/merge-candidates/{cand_id}/reject")
async def reject_merge_candidate(cand_id: int, user_id: UserID) -> dict[str, bool]:
    """Rechaza un candidato (coexisten): pasa a `rejected`."""
    with connection() as conn:
        res = conn.execute(
            text(
                "UPDATE mod_identidades_merge_candidates "
                "SET status = 'rejected', decided_by = 'human', decided_at = NOW() "
                "WHERE id = :id AND user_id = :uid AND status = 'candidate'"
            ),
            {"id": cand_id, "uid": user_id},
        )
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="candidato no encontrado")
    return {"rejected": True}


@router.post("/organize")
async def organize_hierarchy(user_id: UserID) -> dict[str, int]:
    """Organiza la jerarquía de pertenencia («sub») con el LLM y la aplica directo (sin cola de
    confirmación). Una sola llamada holística sobre todas las organizaciones del directorio."""
    try:
        stats = await run_organize(user_id)
    except LLMQuotaError:
        raise HTTPException(status_code=503, detail="sin cuota de LLM disponible") from None
    return {
        "orgs": stats.orgs,
        "linked": stats.linked,
        "created": stats.created,
        "cleaned": stats.cleaned,
        "skipped": stats.skipped,
    }


@router.post("", response_model=IdentityRow)
async def create_identity(user_id: UserID, body: IdentityCreate) -> dict[str, Any]:
    if body.kind not in _KINDS:
        raise HTTPException(status_code=422, detail=f"kind inválido: {body.kind!r}")
    with connection() as conn:
        row = (
            conn.execute(
                text(
                    f"""
                    INSERT INTO mod_identidades
                      (user_id, kind, display_name, aliases, interest, source, notes,
                       given_name, family_name, birthday)
                    VALUES (:uid, :kind, :dn, :aliases, :interest, 'manual', :notes,
                            :given, :family, :bday)
                    RETURNING {_IDENTITY_COLS}
                    """
                ),
                {
                    "uid": user_id,
                    "kind": body.kind,
                    "dn": body.display_name,
                    "aliases": [a.strip() for a in body.aliases if a.strip()],
                    "interest": body.interest,
                    "notes": body.notes,
                    "given": body.given_name,
                    "family": body.family_name,
                    "bday": body.birthday,
                },
            )
            .mappings()
            .one()
        )
    return _identity_row(row)


@router.get("/{identity_id:int}", response_model=IdentityDetail)
async def get_identity(identity_id: int, user_id: UserID) -> dict[str, Any]:
    """Una identidad + sus identificadores, sedes, afiliaciones y menciones recientes."""
    with connection() as conn:
        irow = (
            conn.execute(
                text(f"SELECT {_IDENTITY_COLS} FROM mod_identidades WHERE id=:id AND user_id=:uid"),
                {"id": identity_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
        if irow is None:
            raise HTTPException(status_code=404, detail="identidad no encontrada")
        identifiers = (
            conn.execute(
                text(
                    "SELECT id, platform, kind, value, is_primary, source "
                    "FROM mod_identidades_identifiers WHERE identity_id = :id ORDER BY id"
                ),
                {"id": identity_id},
            )
            .mappings()
            .all()
        )
        sites = (
            conn.execute(
                text(
                    "SELECT id, label, address, country FROM mod_identidades_sites "
                    "WHERE identity_id = :id ORDER BY id"
                ),
                {"id": identity_id},
            )
            .mappings()
            .all()
        )
        affiliations = (
            conn.execute(
                text(
                    """
                    SELECT po.role AS role, o.id AS id, o.kind AS kind,
                           o.display_name AS display_name
                    FROM mod_identidades_person_orgs po
                    JOIN mod_identidades o
                      ON o.id = CASE WHEN po.person_id = :id THEN po.org_id ELSE po.person_id END
                    WHERE (po.person_id = :id OR po.org_id = :id) AND po.user_id = :uid
                    ORDER BY o.display_name
                    """
                ),
                {"id": identity_id, "uid": user_id},
            )
            .mappings()
            .all()
        )
        mentions = (
            conn.execute(
                text(
                    f"SELECT {_MENTION_COLS} FROM mod_identidades_mentions "
                    "WHERE resolved_identity_id = :id AND user_id = :uid ORDER BY id DESC LIMIT 50"
                ),
                {"id": identity_id, "uid": user_id},
            )
            .mappings()
            .all()
        )
        # Sub-identidades que cuelgan de esta (sus «partes»: programas, productos, filiales, …).
        children = (
            conn.execute(
                text(
                    "SELECT id, kind, display_name FROM mod_identidades "
                    "WHERE parent_identity_id = :id AND user_id = :uid ORDER BY display_name"
                ),
                {"id": identity_id, "uid": user_id},
            )
            .mappings()
            .all()
        )
        identity = _identity_row(irow)
        mc = conn.execute(
            text(
                "SELECT count(*) FROM mod_identidades_mentions "
                "WHERE resolved_identity_id = :id AND user_id = :uid"
            ),
            {"id": identity_id, "uid": user_id},
        ).scalar()
        identity["mention_count"] = int(mc or 0)
        if identity["parent_id"] is not None:
            identity["parent_name"] = conn.execute(
                text("SELECT display_name FROM mod_identidades WHERE id = :p AND user_id = :uid"),
                {"p": identity["parent_id"], "uid": user_id},
            ).scalar()
    return {
        "identity": identity,
        "identifiers": [dict(r) for r in identifiers],
        "sites": [dict(r) for r in sites],
        "affiliations": [dict(r) for r in affiliations],
        "mentions": [_mention_row(m) for m in mentions],
        "children": [dict(r) for r in children],
    }


@router.patch("/{identity_id:int}", response_model=IdentityRow)
async def update_identity(
    identity_id: int, user_id: UserID, body: IdentityUpdate
) -> dict[str, Any]:
    """Actualiza una identidad ('promover' = `interest=true`; editar notas/cumpleaños/nombre/alias;
    setear o quitar el padre de pertenencia)."""
    if body.kind is not None and body.kind not in _KINDS:
        raise HTTPException(status_code=422, detail=f"kind inválido: {body.kind!r}")
    sets: list[str] = []
    params: dict[str, Any] = {"id": identity_id, "uid": user_id}
    cols: dict[str, Any] = {
        "display_name": body.display_name,
        "kind": body.kind,
        "interest": body.interest,
        "notes": body.notes,
        "given_name": body.given_name,
        "family_name": body.family_name,
        "birthday": body.birthday,
    }
    for col, val in cols.items():
        if val is not None:
            sets.append(f"{col} = :{col}")
            params[col] = val
    if body.aliases is not None:
        sets.append("aliases = :aliases")
        params["aliases"] = [a.strip() for a in body.aliases if a.strip()]
    # `parent_id` distingue "no enviado" (no tocar) de `null` (quitar el padre) vía exclude_unset.
    set_parent = "parent_id" in body.model_dump(exclude_unset=True)
    with connection() as conn:
        if set_parent:
            pval = body.parent_id
            if pval is None:
                sets.append("parent_identity_id = NULL")
            else:
                if pval == identity_id:
                    raise HTTPException(422, detail="una identidad no puede ser su propio padre")
                owns = conn.execute(
                    text("SELECT 1 FROM mod_identidades WHERE id = :p AND user_id = :uid"),
                    {"p": pval, "uid": user_id},
                ).first()
                if owns is None:
                    raise HTTPException(422, detail="padre no encontrado")
                if would_create_cycle(conn, user_id, identity_id, pval):
                    raise HTTPException(422, detail="el padre crearía un ciclo de pertenencia")
                sets.append("parent_identity_id = :parent_id")
                params["parent_id"] = pval
                sets.append(
                    "metadata = jsonb_set(metadata, '{parent_source}', "
                    "to_jsonb(CAST('manual' AS TEXT)))"
                )
        if not sets:
            raise HTTPException(status_code=422, detail="sin campos para actualizar")
        sets.append("updated_at = NOW()")
        row = (
            conn.execute(
                text(
                    f"UPDATE mod_identidades SET {', '.join(sets)} "
                    f"WHERE id = :id AND user_id = :uid RETURNING {_IDENTITY_COLS}"
                ),
                params,
            )
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(status_code=404, detail="identidad no encontrada")
    return _identity_row(row)


@router.delete("/{identity_id:int}")
async def delete_identity(identity_id: int, user_id: UserID) -> dict[str, bool]:
    with connection() as conn:
        res = conn.execute(
            text("DELETE FROM mod_identidades WHERE id = :id AND user_id = :uid"),
            {"id": identity_id, "uid": user_id},
        )
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="identidad no encontrada")
    return {"deleted": True}


# ---- Identificadores por-fuente -------------------------------------------------------------- #


@router.post("/{identity_id}/identifiers", response_model=IdentityIdentifierRow)
async def add_identifier(
    identity_id: int, user_id: UserID, body: IdentityIdentifierCreate
) -> dict[str, Any]:
    if body.kind not in _IDENTIFIER_KINDS:
        raise HTTPException(status_code=422, detail=f"kind inválido: {body.kind!r}")
    vn = norm_identifier(body.kind, body.value)
    with connection() as conn:
        owns = conn.execute(
            text("SELECT 1 FROM mod_identidades WHERE id=:id AND user_id=:uid"),
            {"id": identity_id, "uid": user_id},
        ).first()
        if owns is None:
            raise HTTPException(status_code=404, detail="identidad no encontrada")
        row = (
            conn.execute(
                text(
                    """
                    INSERT INTO mod_identidades_identifiers
                      (user_id, identity_id, platform, kind, value, value_norm, is_primary, source)
                    VALUES (:uid, :id, :p, :k, :v, :vn, :prim, 'manual')
                    ON CONFLICT (identity_id, platform, kind, value_norm)
                      DO UPDATE SET value = EXCLUDED.value, is_primary = EXCLUDED.is_primary
                    RETURNING id, platform, kind, value, is_primary, source
                    """
                ),
                {
                    "uid": user_id,
                    "id": identity_id,
                    "p": body.platform,
                    "k": body.kind,
                    "v": body.value,
                    "vn": vn,
                    "prim": body.is_primary,
                },
            )
            .mappings()
            .one()
        )
    return dict(row)


@router.delete("/{identity_id}/identifiers/{identifier_id}")
async def delete_identifier(
    identity_id: int, identifier_id: int, user_id: UserID
) -> dict[str, bool]:
    with connection() as conn:
        res = conn.execute(
            text(
                "DELETE FROM mod_identidades_identifiers "
                "WHERE id = :iid AND identity_id = :id AND user_id = :uid"
            ),
            {"iid": identifier_id, "id": identity_id, "uid": user_id},
        )
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="identificador no encontrado")
    return {"deleted": True}


# ---- Sedes (organizaciones) ------------------------------------------------------------------ #


@router.post("/{identity_id}/sites", response_model=IdentitySiteRow)
async def add_site(identity_id: int, user_id: UserID, body: IdentitySiteCreate) -> dict[str, Any]:
    with connection() as conn:
        kind = conn.execute(
            text("SELECT kind FROM mod_identidades WHERE id=:id AND user_id=:uid"),
            {"id": identity_id, "uid": user_id},
        ).scalar()
        if kind is None:
            raise HTTPException(status_code=404, detail="identidad no encontrada")
        if kind != "organizacion":
            raise HTTPException(status_code=422, detail="las sedes son solo de organizaciones")
        row = (
            conn.execute(
                text(
                    """
                    INSERT INTO mod_identidades_sites
                      (user_id, identity_id, label, address, country)
                    VALUES (:uid, :id, :label, :address, :country)
                    RETURNING id, label, address, country
                    """
                ),
                {
                    "uid": user_id,
                    "id": identity_id,
                    "label": body.label,
                    "address": body.address,
                    "country": body.country,
                },
            )
            .mappings()
            .one()
        )
    return dict(row)


@router.delete("/{identity_id}/sites/{site_id}")
async def delete_site(identity_id: int, site_id: int, user_id: UserID) -> dict[str, bool]:
    with connection() as conn:
        res = conn.execute(
            text(
                "DELETE FROM mod_identidades_sites "
                "WHERE id = :sid AND identity_id = :id AND user_id = :uid"
            ),
            {"sid": site_id, "id": identity_id, "uid": user_id},
        )
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="sede no encontrada")
    return {"deleted": True}


# ---- Afiliación persona↔org ------------------------------------------------------------------ #


@router.post("/{identity_id}/orgs", response_model=IdentityDetail)
async def affiliate(
    identity_id: int, user_id: UserID, body: IdentityAffiliateCreate
) -> dict[str, Any]:
    """Asocia una persona (`identity_id`) con una organización (`org_id`). Idempotente."""
    with connection() as conn:
        person_kind = conn.execute(
            text("SELECT kind FROM mod_identidades WHERE id=:id AND user_id=:uid"),
            {"id": identity_id, "uid": user_id},
        ).scalar()
        org_kind = conn.execute(
            text("SELECT kind FROM mod_identidades WHERE id=:id AND user_id=:uid"),
            {"id": body.org_id, "uid": user_id},
        ).scalar()
        if person_kind is None or org_kind is None:
            raise HTTPException(status_code=404, detail="identidad no encontrada")
        if person_kind != "persona" or org_kind != "organizacion":
            raise HTTPException(status_code=422, detail="la afiliación es persona → organización")
        conn.execute(
            text(
                """
                INSERT INTO mod_identidades_person_orgs (user_id, person_id, org_id, role, source)
                VALUES (:uid, :p, :o, :role, 'manual')
                ON CONFLICT (person_id, org_id) DO UPDATE SET role = EXCLUDED.role
                """
            ),
            {"uid": user_id, "p": identity_id, "o": body.org_id, "role": body.role},
        )
    return await get_identity(identity_id, user_id)


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
