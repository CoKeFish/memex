"""API de asuntos (storylines): lista, storyline, acciones, asignador, curador y settings."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.api.auth import current_user_id
from memex.api.routers.graph import _inbox_kinds
from memex.api.schemas import (
    AsuntoAssignRequest,
    AsuntoCloseRequest,
    AsuntoCreateRequest,
    AsuntoCurateRequest,
    AsuntoDatoAdd,
    AsuntoDatoRemove,
    AsuntoDueIn,
    AsuntoEvidenceAdd,
    AsuntoList,
    AsuntoMergeRequest,
    AsuntoNotaRequest,
    AsuntoNotaResponse,
    AsuntoPlanRequest,
    AsuntoPlanResponse,
    AsuntoPriorityIn,
    AsuntoRelateRequest,
    AsuntoRenameRequest,
    AsuntoRow,
    AsuntosSettingsOut,
    AsuntosSettingsPatch,
    AsuntoStateRequest,
    AsuntoStory,
)
from memex.db import connection
from memex.logging import get_logger
from memex.modules.asuntos import store
from memex.modules.asuntos.datos import add_dato, datos_of, remove_dato
from memex.modules.asuntos.notas import anotar, list_notas
from memex.modules.asuntos.settings import get_settings, upsert_settings
from memex.relations.edges import ASUNTO_SLUG, PRODUCER_ASUNTOS, Ref, list_edges
from memex.relations.vertices import list_vertices

router = APIRouter(prefix="/asuntos", tags=["asuntos"])
UserID = Annotated[int, Depends(current_user_id)]
_log = get_logger("memex.api.asuntos")


def _row(a: store.Asunto) -> dict[str, Any]:
    return {
        "id": a.id,
        "name": a.name,
        "aliases": list(a.aliases),
        "kind": a.kind,
        "description": a.description,
        "status": a.status,
        "state_text": a.state_text,
        "state_updated_at": a.state_updated_at,
        "seed": a.seed,
        "entities": list(a.entities),
        "opened_by_inbox_id": a.opened_by_inbox_id,
        "first_signal_at": a.first_signal_at,
        "last_signal_at": a.last_signal_at,
        "evidence_count": a.evidence_count,
        "closed_at": a.closed_at,
        "merged_into_id": a.merged_into_id,
        "created_at": a.created_at,
        "updated_at": a.updated_at,
        "priority": a.priority,
        "priority_reason": a.priority_reason,
        "priority_source": a.priority_source,
        "due_on": a.due_on,
        "silenced_at": a.silenced_at,
    }


def _tareas_summary(
    conn: Connection, user_id: int, ids: Sequence[int]
) -> dict[int, dict[str, Any]]:
    """«Qué sigue» por asunto (lista y story), con la MISMA regla que la agenda de la ventana
    (`agendaOf` en el frontend): `tareas_pendientes` = pendientes vivas (los proyectos son
    contenedores y no cuentan; sus pasos sí); `proxima_fecha` = la menor entre los vencimientos de
    las tareas sueltas pendientes y las ENTREGAS de los proyectos pendientes (la mayor fecha entre
    el proyecto y sus pasos no cancelados). Un paso viejo no adelanta la fecha del asunto. Una
    consulta para todos los ids."""
    if not ids:
        return {}
    rows = (
        conn.execute(
            text(
                "SELECT id, asunto_id, due_on, parent_id, status, "
                "COALESCE(metadata->>'is_parent', 'false') = 'true' AS is_parent "
                "FROM mod_tareas WHERE user_id = :u AND asunto_id = ANY(:ids) "
                "AND status <> 'cancelada'"
            ),
            {"u": user_id, "ids": list(ids)},
        )
        .mappings()
        .all()
    )
    by_parent: dict[int, list[Any]] = {}
    for r in rows:
        if r["parent_id"] is not None:
            by_parent.setdefault(int(r["parent_id"]), []).append(r)
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        aid = int(r["asunto_id"])
        cur = out.setdefault(aid, {"tareas_pendientes": 0, "proxima_fecha": None})
        if r["status"] != "pendiente":
            continue
        steps = by_parent.get(int(r["id"]), [])
        is_project = bool(r["is_parent"]) or bool(steps)
        if not is_project:
            cur["tareas_pendientes"] += 1
        if r["parent_id"] is not None and not is_project:
            continue  # un paso: su fecha entra por la entrega del proyecto
        dates = [r["due_on"]] + [st["due_on"] for st in steps] if is_project else [r["due_on"]]
        known = [d for d in dates if d is not None]
        if not known:
            continue
        candidate = max(known) if is_project else known[0]
        if cur["proxima_fecha"] is None or candidate < cur["proxima_fecha"]:
            cur["proxima_fecha"] = candidate
    return out


def _counts(conn: Connection, user_id: int) -> dict[str, int]:
    rows = conn.execute(
        text(
            "SELECT status, COUNT(*) FROM mod_asuntos "
            "WHERE user_id = :u AND merged_into_id IS NULL GROUP BY status"
        ),
        {"u": user_id},
    ).all()
    return {str(r[0]): int(r[1]) for r in rows}


@router.get("", response_model=AsuntoList)
async def list_asuntos(
    user_id: UserID,
    status: str | None = None,
    kind: str | None = None,
    priority: str | None = None,
    q: str | None = None,
    limit: int = 300,
    offset: int = 0,
) -> dict[str, Any]:
    statuses = [s for s in (status or "").split(",") if s] or None
    kinds = [k for k in (kind or "").split(",") if k] or None
    priorities = [p for p in (priority or "").split(",") if p] or None
    with connection() as conn:
        rows = store.list_asuntos(
            conn,
            user_id,
            statuses=statuses,
            kinds=kinds,
            priorities=priorities,
            q=q,
            limit=limit,
            offset=offset,
        )
        counts = _counts(conn, user_id)
        que_sigue = _tareas_summary(conn, user_id, [a.id for a in rows])
    return {"items": [{**_row(a), **que_sigue.get(a.id, {})} for a in rows], "counts": counts}


def _elenco(conn: Connection, user_id: int, asunto_id: int) -> list[dict[str, Any]]:
    """Elenco del asunto = destinos de sus aristas «involucra» (con etiqueta y tipo del vértice)."""
    me = Ref(ASUNTO_SLUG, asunto_id)
    edges = [
        e
        for e in list_edges(conn, user_id, producer=PRODUCER_ASUNTOS)
        if e.src == me and e.dst.slug != ASUNTO_SLUG
    ]
    if not edges:
        return []
    labels = {(v.slug, v.id): (v.label, v.kind) for v in list_vertices(conn, user_id)}
    out: list[dict[str, Any]] = []
    for e in edges:
        label, kind = labels.get((e.dst.slug, e.dst.id), (f"{e.dst.slug}#{e.dst.id}", e.dst.slug))
        n = 0
        if e.evidence.startswith("mensajes="):
            try:
                n = int(e.evidence.split("=", 1)[1])
            except ValueError:
                n = 0
        out.append(
            {
                "slug": e.dst.slug,
                "id": e.dst.id,
                "label": label,
                "kind": kind,
                "messages": n,
                "edge_id": e.id,
            }
        )
    out.sort(key=lambda x: (-int(x["messages"]), str(x["kind"]), str(x["label"])))
    return out


@router.get("/settings", response_model=AsuntosSettingsOut)
async def get_asuntos_settings(user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        s = get_settings(conn, user_id)
    return s.__dict__


@router.patch("/settings", response_model=AsuntosSettingsOut)
async def patch_asuntos_settings(user_id: UserID, body: AsuntosSettingsPatch) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    try:
        with connection() as conn:
            s = upsert_settings(conn, user_id, **fields)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    _log.info("asuntos.settings.patch", user_id=user_id, **fields)
    return s.__dict__


@router.post("/assign")
async def assign(user_id: UserID, body: AsuntoAssignRequest) -> dict[str, Any]:
    from memex.modules.asuntos.assign import run_assign, run_pending

    if body.inbox_ids:
        res = await run_assign(user_id, body.inbox_ids, force=body.force)
    else:
        res = await run_pending(user_id, limit=body.limit)
    return res.as_dict()


@router.post("/curate")
async def curate(user_id: UserID, body: AsuntoCurateRequest) -> dict[str, Any]:
    from memex.modules.asuntos.curator import run_curator

    st = await run_curator(
        user_id,
        scope=body.scope,
        limit=body.limit,
        dry_run=body.dry_run,
        web_search=body.web_search,
    )
    return st.__dict__


@router.post("/maintain")
async def maintain(user_id: UserID) -> dict[str, Any]:
    from memex.modules.asuntos.curator import run_maintenance

    st = await run_maintenance(user_id)
    return st.__dict__


@router.post("/merge", response_model=AsuntoRow)
async def merge(user_id: UserID, body: AsuntoMergeRequest) -> dict[str, Any]:
    with connection() as conn:
        ok = store.merge_asuntos(
            conn, user_id, survivor_id=body.survivor_id, absorbed_id=body.absorbed_id
        )
        if not ok:
            raise HTTPException(status_code=422, detail="fusión no aplicable")
        a = store.get_asunto(conn, user_id, store.resolve_merged(conn, user_id, body.survivor_id))
    assert a is not None
    return _row(a)


@router.post("/relate")
async def relate(user_id: UserID, body: AsuntoRelateRequest) -> dict[str, Any]:
    with connection() as conn:
        try:
            ok = store.add_relation(
                conn,
                user_id,
                body.src_id,
                body.dst_id,
                body.relation_type,
                confidence=1.0,
                source="manual",
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    return {"created": ok}


@router.post("/relations/{relation_id:int}/reject")
async def reject_relation(relation_id: int, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        ok = store.reject_relation(conn, user_id, relation_id)
    if not ok:
        raise HTTPException(status_code=404, detail="relación no encontrada")
    return {"rejected": True}


@router.post("/evidence/{evidence_id:int}/reject")
async def reject_evidence(evidence_id: int, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        aid = store.reject_evidence(conn, user_id, evidence_id)
    if aid is None:
        raise HTTPException(status_code=404, detail="evidencia no encontrada")
    return {"asunto_id": aid}


@router.post("/{asunto_id:int}/evidence")
async def add_evidence(asunto_id: int, user_id: UserID, body: AsuntoEvidenceAdd) -> dict[str, Any]:
    with connection() as conn:
        if store.get_asunto(conn, user_id, asunto_id) is None:
            raise HTTPException(status_code=404, detail="asunto no encontrado")
        created = store.add_evidence(
            conn,
            user_id,
            asunto_id,
            body.inbox_id,
            method="manual",
            hito_date=body.hito_date,
            hito_text=body.hito_text,
            reactivate=True,
        )
    return {"created": bool(created)}


@router.get("/{asunto_id:int}", response_model=AsuntoStory)
async def story(asunto_id: int, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        a = store.get_asunto(conn, user_id, asunto_id)
        if a is None:
            raise HTTPException(status_code=404, detail="asunto no encontrado")
        if a.merged_into_id is not None:
            a = store.get_asunto(conn, user_id, store.resolve_merged(conn, user_id, a.id)) or a
        ev = store.evidence_of(conn, user_id, a.id)
        kinds = _inbox_kinds(conn, user_id, {e.inbox_id for e in ev})
        states = store.states_of(conn, user_id, a.id)
        rels = store.relations_of(conn, user_id, a.id)
        elenco = _elenco(conn, user_id, a.id)
        datos = datos_of(conn, user_id, a.id)
        que_sigue = _tareas_summary(conn, user_id, [a.id]).get(a.id, {})
        notas = list_notas(conn, user_id, a.id)
    hitos = sorted(
        (
            {
                "date": (e.hito_date or (e.occurred_at.date() if e.occurred_at else None)),
                "date_is_exact": e.hito_date is not None,
                "text": e.hito_text,
                "inbox_id": e.inbox_id,
                "evidence_id": e.id,
            }
            for e in ev
            if e.hito_text
        ),
        key=lambda h: (h["date"] is None, h["date"] or a.created_at.date()),
    )
    # Lo que contó el dueño también es un hito: va a la cronología con su día (local).
    hitos.extend(
        {
            "date": n["fecha_local"],
            "date_is_exact": True,
            "text": n["texto"],
            "inbox_id": None,
            "evidence_id": None,
            "nota_id": n["id"],
        }
        for n in notas
    )
    hitos.sort(key=lambda h: (h["date"] is None, h["date"] or a.created_at.date()))
    return {
        "asunto": {**_row(a), **que_sigue},
        "datos": datos,
        "hitos": hitos,
        "evidence": [
            {
                "id": e.id,
                "inbox_id": e.inbox_id,
                "kind": kinds.get(e.inbox_id),
                "method": e.method,
                "confidence": e.confidence,
                "occurred_at": e.occurred_at,
                "subject": e.subject,
                "sender": e.sender,
                "hito_text": e.hito_text,
                "hito_date": e.hito_date,
                "filename": e.filename,
                "media_id": e.media_id,
                "size_bytes": e.size_bytes,
            }
            for e in ev
        ],
        "states": [s.__dict__ for s in states],
        "notas": notas,
        "relations": [r.__dict__ for r in rels],
        "elenco": elenco,
    }


@router.post("/{asunto_id:int}/datos")
async def add_dato_endpoint(asunto_id: int, user_id: UserID, body: AsuntoDatoAdd) -> dict[str, Any]:
    """Un dato clave del asunto (código de clase, enlace de acceso, salón…), a mano."""
    with connection() as conn:
        if store.get_asunto(conn, user_id, asunto_id) is None:
            raise HTTPException(status_code=404, detail="asunto no encontrado")
        try:
            created = add_dato(
                conn, user_id, asunto_id, label=body.label, value=body.value, origin="manual"
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return {"created": created, "datos": datos_of(conn, user_id, asunto_id)}


@router.post("/{asunto_id:int}/datos/remove")
async def remove_dato_endpoint(
    asunto_id: int, user_id: UserID, body: AsuntoDatoRemove
) -> dict[str, Any]:
    with connection() as conn:
        if store.get_asunto(conn, user_id, asunto_id) is None:
            raise HTTPException(status_code=404, detail="asunto no encontrado")
        removed = remove_dato(conn, user_id, asunto_id, body.value)
        return {"removed": removed, "datos": datos_of(conn, user_id, asunto_id)}


@router.post("/{asunto_id:int}/close", response_model=AsuntoRow)
async def close(asunto_id: int, user_id: UserID, body: AsuntoCloseRequest) -> dict[str, Any]:
    """Cierra el asunto y aplica a sus pendientes lo que el dueño haya decidido.

    Solo acá y en el CLI: el curador y las reglas de vencimiento cierran asuntos sin tocar tareas,
    porque un cierre automático que te cancele pendientes sería peor que el problema.
    """
    try:
        with connection() as conn:
            if store.get_asunto(conn, user_id, asunto_id) is None:
                raise HTTPException(status_code=404, detail="asunto no encontrado")
            store.resolve_pending_tareas(conn, user_id, asunto_id, body.tareas)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return _state_change(asunto_id, user_id, body.state_text, closed=True)


@router.post("/{asunto_id:int}/due", response_model=AsuntoRow)
async def set_due(user_id: UserID, asunto_id: int, body: AsuntoDueIn) -> dict[str, Any]:
    """La palabra del usuario sobre la fecha límite del próximo paso (source `user`)."""
    with connection() as conn:
        store.set_due(conn, user_id, asunto_id, body.due_on, source="user")
        a = store.get_asunto(conn, user_id, asunto_id)
    if a is None:
        raise HTTPException(status_code=404, detail="asunto no encontrado")
    return _row(a)


@router.post("/{asunto_id:int}/priority", response_model=AsuntoRow)
async def set_priority(user_id: UserID, asunto_id: int, body: AsuntoPriorityIn) -> dict[str, Any]:
    """La palabra del usuario sobre la prioridad (source `user`: el asignador no la pisa)."""
    with connection() as conn:
        store.set_priority(
            conn, user_id, asunto_id, body.priority, reason=body.reason, source="user"
        )
        a = store.get_asunto(conn, user_id, asunto_id)
    if a is None:
        raise HTTPException(status_code=404, detail="asunto no encontrado")
    return _row(a)


@router.post("", response_model=AsuntoRow, status_code=201)
async def create(body: AsuntoCreateRequest, user_id: UserID) -> dict[str, Any]:
    """Declara un asunto a mano: el único que no nace de un mensaje.

    Es la puerta de entrada del planificador: primero decís «sacar el pasaporte es un asunto mío»
    y después le pedís el plan.
    """
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="el nombre no puede estar vacío")
    try:
        with connection() as conn:
            aid = store.create_asunto(
                conn,
                user_id,
                name=name,
                kind=body.kind,
                description=body.description.strip(),
                seed="manual",
                status="abierto",
                priority=body.priority or "normal",
                priority_source="user" if body.priority else None,
            )
            a = store.get_asunto(conn, user_id, aid)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    assert a is not None
    _log.info("asuntos.api.create", asunto_id=aid, kind=body.kind)
    return _row(a)


@router.post("/{asunto_id:int}/plan", response_model=AsuntoPlanResponse)
async def plan(asunto_id: int, body: AsuntoPlanRequest, user_id: UserID) -> dict[str, Any]:
    """Arma el plan del asunto con codex y búsqueda web en fuentes OFICIALES.

    Tarda: son varios minutos de búsqueda. Deja un proyecto con sus pasos colgando del asunto, así
    que lo que sale de acá aparece igual en «Qué sigue», en la agenda y en el resumen de Telegram.
    """
    from memex.modules.asuntos.planner import plan_asunto

    try:
        res = await plan_asunto(
            user_id,
            asunto_id,
            contexto=body.context,
            ciudad=body.city,
            dry_run=body.dry_run,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:  # codex ausente: accionable, no un 500 mudo
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {
        "asunto_id": res.asunto_id,
        "resumen": res.plan.resumen,
        "pasos": [
            {
                "titulo": p.titulo,
                "que_hacer": p.que_hacer,
                "donde": p.donde,
                "llevar": list(p.llevar),
                "costo": p.costo,
                "fecha": p.fecha,
                "fuente": p.fuente,
                "por_que": p.por_que,
            }
            for p in res.plan.pasos
        ],
        "fuentes": list(res.plan.fuentes),
        "sin_confirmar": list(res.plan.sin_confirmar),
        "proyecto_id": res.proyecto_id,
        "creadas": res.creadas,
        "repetidas": res.repetidas,
        "dry_run": res.dry_run,
    }


@router.post("/{asunto_id:int}/nota", response_model=AsuntoNotaResponse)
async def nota(asunto_id: int, body: AsuntoNotaRequest, user_id: UserID) -> dict[str, Any]:
    """«Contar algo»: la nota queda como hito del asunto y, si dice cuándo va a hacer un paso
    o que algo ya está hecho, se aplica (fecha al paso, paso hecho, plazo, estado). Devuelve
    qué entendió y qué cambió, para que la pantalla lo diga tal cual.
    """
    try:
        res = await anotar(user_id, asunto_id, body.texto, interpretar=body.interpretar)
    except ValueError as e:
        code = 404 if "no existe" in str(e) else 422
        raise HTTPException(status_code=code, detail=str(e)) from e
    return {
        "nota_id": res.nota_id,
        "asunto_id": res.asunto_id,
        "texto": res.texto,
        "entendido": res.entendido,
        "cambios": res.cambios,
        "interpretada": res.interpretada,
    }


@router.post("/{asunto_id:int}/silence", response_model=AsuntoRow)
async def silence(asunto_id: int, user_id: UserID) -> dict[str, Any]:
    """Silencia el asunto: lo cierra y lo deja cerrado aunque siga llegando señal.

    Para el hilo que el dueño no puede ni quiere atender (un aparato que repite la misma alarma).
    La evidencia nueva sigue entrando y el asunto sigue en el catálogo del asignador —si saliera,
    el aviso siguiente abriría un duplicado—; lo que no pasa es que vuelva solo. Las tareas
    pendientes se cancelan: silenciar es «no voy a hacer nada con esto».
    """
    with connection() as conn:
        ok, _ = store.silence(conn, user_id, asunto_id)
        if not ok:
            raise HTTPException(status_code=404, detail="asunto no encontrado")
        a = store.get_asunto(conn, user_id, asunto_id)
    assert a is not None
    _log.info("asuntos.api.silence", asunto_id=asunto_id)
    return _row(a)


@router.post("/{asunto_id:int}/unsilence", response_model=AsuntoRow)
async def unsilence(asunto_id: int, user_id: UserID) -> dict[str, Any]:
    """Levanta el silencio sin reabrir: el asunto queda cerrado pero vuelve a poder revivir."""
    with connection() as conn:
        if store.get_asunto(conn, user_id, asunto_id) is None:
            raise HTTPException(status_code=404, detail="asunto no encontrado")
        store.unsilence(conn, user_id, asunto_id)
        a = store.get_asunto(conn, user_id, asunto_id)
    assert a is not None
    _log.info("asuntos.api.unsilence", asunto_id=asunto_id)
    return _row(a)


@router.post("/{asunto_id:int}/reopen", response_model=AsuntoRow)
async def reopen(asunto_id: int, user_id: UserID, body: AsuntoStateRequest) -> dict[str, Any]:
    with connection() as conn:
        if store.get_asunto(conn, user_id, asunto_id) is None:
            raise HTTPException(status_code=404, detail="asunto no encontrado")
        store.set_status(conn, user_id, asunto_id, "abierto", source="manual")
        if body.state_text:
            store.record_state(
                conn, user_id, asunto_id, body.state_text, closed=False, source="manual"
            )
        a = store.get_asunto(conn, user_id, asunto_id)
    assert a is not None
    return _row(a)


@router.post("/{asunto_id:int}/state", response_model=AsuntoRow)
async def set_state(asunto_id: int, user_id: UserID, body: AsuntoStateRequest) -> dict[str, Any]:
    return _state_change(asunto_id, user_id, body.state_text, closed=False)


def _state_change(asunto_id: int, user_id: int, state_text: str, *, closed: bool) -> dict[str, Any]:
    with connection() as conn:
        if store.get_asunto(conn, user_id, asunto_id) is None:
            raise HTTPException(status_code=404, detail="asunto no encontrado")
        store.record_state(conn, user_id, asunto_id, state_text, closed=closed, source="manual")
        a = store.get_asunto(conn, user_id, asunto_id)
    assert a is not None
    return _row(a)


@router.post("/{asunto_id:int}/rename", response_model=AsuntoRow)
async def rename(asunto_id: int, user_id: UserID, body: AsuntoRenameRequest) -> dict[str, Any]:
    with connection() as conn:
        if store.get_asunto(conn, user_id, asunto_id) is None:
            raise HTTPException(status_code=404, detail="asunto no encontrado")
        try:
            store.rename(conn, user_id, asunto_id, body.name)
            if body.kind:
                store.update_fields(conn, user_id, asunto_id, kind=body.kind)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        a = store.get_asunto(conn, user_id, asunto_id)
    assert a is not None
    return _row(a)


@router.delete("/{asunto_id:int}")
async def delete(asunto_id: int, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        ok = store.delete_asunto(conn, user_id, asunto_id)
    if not ok:
        raise HTTPException(status_code=404, detail="asunto no encontrado")
    return {"deleted": True}
