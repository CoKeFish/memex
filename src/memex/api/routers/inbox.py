import json
from datetime import date, datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.coverage_helpers import clip_date_spans, merge_date_spans, merge_day_buckets
from memex.api.schemas import (
    ClassificationInfo,
    ClassifyRequest,
    CoverageOut,
    ExtractResponse,
    FeedbackInfo,
    FeedbackRequest,
    InboxList,
    InboxRow,
    InboxStats,
    InboxWindow,
    ProcessResponse,
    RelevanceMarkInfo,
    RelevanceMarkRequest,
    ReprocessRequest,
    ReprocessResponse,
    StatsBySource,
    SummarizeResponse,
)
from memex.classifier.rules import classify
from memex.core.cursors import summarize_cursor
from memex.core.feedback import InvalidFeedbackError, get_feedback, record_feedback
from memex.core.relevance_marks import clear_mark, get_mark, set_mark
from memex.db import connection
from memex.logging import bind_request_context, get_logger
from memex.sources import kind_for_type

router = APIRouter(prefix="/inbox", tags=["inbox"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.inbox")

#: SELECT de "fila de lista" (inbox + tier + avance real del pipeline). Compartido por la lista
#: y por los miembros del lote (/window) para que ambas devuelvan el MISMO shape de fila.
#: `{where}`/`{order}` se interpolan con cláusulas armadas localmente (nunca input del usuario).
_LIST_ROW_SQL = """
    SELECT i.id, i.source_id, i.external_id, i.occurred_at, i.received_at,
           i.payload, i.processed_at, i.process_error, i.attempts,
           c.tier AS _tier, c.metadata AS _cmeta,
           EXISTS (SELECT 1 FROM summary_inbox_links sl
                   WHERE sl.inbox_id = i.id) AS _summarized,
           EXISTS (SELECT 1 FROM module_extractions me
                   WHERE me.inbox_id = i.id) AS _extracted
    FROM inbox i
    LEFT JOIN classifications c ON c.inbox_id = i.id
    WHERE {where}
    ORDER BY {order}
    LIMIT :limit
"""


def _map_list_row(r: Any) -> dict[str, Any]:
    """Fila cruda del `_LIST_ROW_SQL` → dict con `classification`/`summarized`/`extracted`."""
    d = dict(r)
    tier = d.pop("_tier", None)
    cmeta = d.pop("_cmeta", None)
    d["classification"] = {"tier": tier, "metadata": cmeta} if tier else None
    d["summarized"] = bool(d.pop("_summarized", False))
    d["extracted"] = bool(d.pop("_extracted", False))
    return d


#: TZ por defecto del bucket de cobertura (cuando el cliente no manda `tz`). El front pasa su TZ
#: activa para que los días del timeline coincidan con su reloj de pared (patrón de metrics.py).
_BUCKET_TZ = "America/Bogota"


def _resolve_tz(tz: str | None) -> str:
    """Valida/resuelve la TZ del bucket. None → `_BUCKET_TZ`; nombre IANA inválido → 422.

    El valor va al SQL como bind param (`:tz` en `AT TIME ZONE`), no es injection; pero un nombre
    inválido reventaría en Postgres, así que se valida acá contra el catálogo IANA (helper copiado
    inline a propósito, como en logs.py/metrics.py).
    """
    if tz is None:
        return _BUCKET_TZ
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"timezone inválida: {tz}") from exc
    return tz


@router.get("", response_model=InboxList)
async def list_inbox(
    user_id: UserID,
    source_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    processed: Literal["true", "false", "all"] = "all",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: int | None = Query(default=None, description="id > cursor for pagination"),
) -> dict[str, Any]:
    where: list[str] = ["i.user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}

    if source_id is not None:
        where.append("i.source_id = :sid")
        params["sid"] = source_id
    if since is not None:
        where.append("i.occurred_at >= :since")
        params["since"] = since
    if until is not None:
        where.append("i.occurred_at < :until")
        params["until"] = until
    if processed == "true":
        where.append("i.processed_at IS NOT NULL")
    elif processed == "false":
        where.append("i.processed_at IS NULL")
    if cursor is not None:
        where.append("i.id > :cur")
        params["cur"] = cursor

    # Estado para la lista (índices por inbox_id en las 3 tablas):
    #  - classifications: el tier (blacklist/batch/individual) = "en qué filtro entró".
    #  - summary/extraction (EXISTS): avance real del pipeline. `inbox.processed_at` quedó en desuso
    #    (casi nunca se setea), así que el estado se deriva de clasificación + resumen/extracción.
    sql = _LIST_ROW_SQL.format(where=" AND ".join(where), order="i.id")
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    items = [_map_list_row(r) for r in rows]
    next_cursor = items[-1]["id"] if len(items) == limit else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/stats", response_model=InboxStats)
async def stats(user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT source_id,
                           COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE processed_at IS NULL) AS pending,
                           COUNT(*) FILTER (WHERE process_error IS NOT NULL) AS errored
                    FROM inbox
                    WHERE user_id = :uid
                    GROUP BY source_id
                    ORDER BY source_id
                    """
                ),
                {"uid": user_id},
            )
            .mappings()
            .all()
        )
    sources = {
        r["source_id"]: StatsBySource(total=r["total"], pending=r["pending"], errored=r["errored"])
        for r in rows
    }
    return {"sources": sources}


# NOTA: declarado ANTES de `GET /{inbox_id}` — si quedara después, FastAPI intentaría parsear
# "coverage" como int y la ruta moriría en 422.
@router.get("/coverage", response_model=CoverageOut)
async def inbox_coverage(
    user_id: UserID,
    tz: str | None = None,
    gap_days: Annotated[int, Query(ge=0, le=365)] = 2,
    source_id: int | None = None,
    kind: Literal["email", "chat", "social", "other"] | None = None,
    since: date | None = None,
    until: date | None = None,
) -> dict[str, Any]:
    """Rangos de FECHAS DE ORIGEN (`occurred_at`) ya ingeridos, por fuente.

    Para el timeline de cobertura del historial: un día (en la tz pedida) está cubierto si tiene
    >= 1 item; días separados por <= `gap_days` se funden en un rango. Las fuentes sin items
    salen como lane vacía (eso también es información: "de acá no hay nada ingerido").

    Además cada lane trae sus tramos BARRIDOS (`swept`): rangos de fechas que un fetch de rango
    o incremental ya recorrió aunque no haya dejado mensajes — distingue "barrí y estaba vacío"
    de "nunca lo intenté". Fuentes: `ingest_swept_ranges` (bitácora durable) + el avance del
    backfill_job vigente (`[range_start, frontier)`, que cubre lo barrido antes de la bitácora).

    `since`/`until` (inclusivos) acotan la ventana del eje: filtran buckets, recortan barridos
    y fijan el dominio EXACTO a la ventana pedida. `cursor` por lane = posición del checkpoint
    incremental ("al día hasta acá"), omitido si cae fuera de la ventana.
    """
    resolved_tz = _resolve_tz(tz)
    if since is not None and until is not None and until < since:
        raise HTTPException(status_code=422, detail="until no puede ser anterior a since")

    src_where = ["user_id = :uid"]
    bucket_where = ["i.user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id}
    if source_id is not None:
        src_where.append("id = :sid")
        bucket_where.append("i.source_id = :sid")
        params["sid"] = source_id
    bucket_params: dict[str, Any] = {**params, "tz": resolved_tz}
    if since is not None:
        bucket_where.append("(i.occurred_at AT TIME ZONE :tz)::date >= :since")
        bucket_params["since"] = since
    if until is not None:
        bucket_where.append("(i.occurred_at AT TIME ZONE :tz)::date <= :until")
        bucket_params["until"] = until

    with connection() as conn:
        src_rows = (
            conn.execute(
                text(
                    "SELECT id, name, type, enabled FROM sources "
                    f"WHERE {' AND '.join(src_where)} ORDER BY id"
                ),
                params,
            )
            .mappings()
            .all()
        )
        # Buckets diarios por fuente; el índice inbox_user_occurred cubre las tres columnas.
        bucket_rows = conn.execute(
            text(
                f"""
                SELECT i.source_id,
                       (i.occurred_at AT TIME ZONE :tz)::date AS day,
                       COUNT(*) AS n
                FROM inbox i
                WHERE {" AND ".join(bucket_where)}
                GROUP BY i.source_id, day
                ORDER BY i.source_id, day
                """
            ),
            bucket_params,
        ).all()
        # Tramos barridos: la bitácora durable + el avance del job vigente (frontera ya recorrida).
        sweep_where = ["user_id = :uid"] + (["source_id = :sid"] if source_id is not None else [])
        swept_rows = conn.execute(
            text(
                "SELECT source_id, range_start, range_end FROM ingest_swept_ranges "
                f"WHERE {' AND '.join(sweep_where)}"
            ),
            params,
        ).all()
        job_rows = conn.execute(
            text(
                "SELECT source_id, range_start, frontier FROM backfill_jobs "
                f"WHERE {' AND '.join(sweep_where)} AND frontier > range_start"
            ),
            params,
        ).all()
        # Cursores incrementales (checkpoints): no tienen user_id propio → JOIN a sources.
        cursor_where = ["s.user_id = :uid"] + (
            ["sc.source_id = :sid"] if source_id is not None else []
        )
        cursor_rows = conn.execute(
            text(
                "SELECT sc.source_id, sc.cursor, sc.updated_at, s.type "
                "FROM source_checkpoints sc JOIN sources s ON s.id = sc.source_id "
                f"WHERE {' AND '.join(cursor_where)}"
            ),
            params,
        ).all()

    buckets_by_source: dict[int, list[tuple[date, int]]] = {}
    for sid, day, n in bucket_rows:
        buckets_by_source.setdefault(sid, []).append((day, n))
    spans_by_source: dict[int, list[tuple[date, date]]] = {}
    for sid, start, end in [*swept_rows, *job_rows]:
        spans_by_source.setdefault(sid, []).append((start, end))
    cursors_by_source: dict[int, dict[str, Any]] = {}
    for sid, cur, updated_at, src_type in cursor_rows:
        cur_day = updated_at.astimezone(ZoneInfo(resolved_tz)).date()
        if (since is not None and cur_day < since) or (until is not None and cur_day > until):
            continue  # marcador fuera de la ventana pedida: se omite
        cursors_by_source[sid] = {
            "at": updated_at,
            "day": cur_day,
            "summary": summarize_cursor(str(src_type), dict(cur)),
        }

    lanes: list[dict[str, Any]] = []
    domain_lo: list[date] = []
    domain_hi: list[date] = []
    for src in src_rows:
        try:
            src_kind = kind_for_type(src["type"]).value
        except KeyError:
            src_kind = "other"  # tipos sin SourceKind registrada (p.ej. seeds viejos)
        if kind is not None and src_kind != kind:
            continue
        ranges = merge_day_buckets(buckets_by_source.get(src["id"], []), gap_days)
        swept = merge_date_spans(clip_date_spans(spans_by_source.get(src["id"], []), since, until))
        cursor_info = cursors_by_source.get(src["id"])
        lanes.append(
            {
                "id": src["id"],
                "label": src["name"],
                "kind": src_kind,
                "enabled": src["enabled"],
                "total": sum(r["count"] for r in ranges),
                "first_day": ranges[0]["start"] if ranges else None,
                "last_day": ranges[-1]["end"] if ranges else None,
                "ranges": ranges,
                "swept": swept,
                "cursor": cursor_info,
            }
        )
        # El dominio del eje abarca items, barridos y cursor (todo es cobertura/estado).
        domain_lo += [r["start"] for r in (ranges[:1] + swept[:1])]
        domain_hi += [r["end"] for r in (ranges[-1:] + swept[-1:])]
        if cursor_info is not None:
            domain_lo.append(cursor_info["day"])
            domain_hi.append(cursor_info["day"])

    return {
        "lanes": lanes,
        # Con ventana pedida el eje ES la ventana (aunque esté vacía); sin ella, los extremos
        # de los datos. Lado pedido a medias: el faltante sale de los datos (o queda None).
        "domain_min": since if since is not None else (min(domain_lo) if domain_lo else None),
        "domain_max": until if until is not None else (max(domain_hi) if domain_hi else None),
        "tz": resolved_tz,
        "gap_days": gap_days,
    }


@router.get("/{inbox_id}", response_model=InboxRow)
async def get_inbox(inbox_id: int, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT i.id, i.source_id, i.external_id, i.occurred_at, i.received_at,
                           i.payload, i.processed_at, i.process_error, i.attempts,
                           c.tier AS _tier, c.metadata AS _cmeta
                    FROM inbox i
                    LEFT JOIN classifications c ON c.inbox_id = i.id
                    WHERE i.id = :id AND i.user_id = :uid
                    """
                ),
                {"id": inbox_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    data = dict(row)
    tier = data.pop("_tier", None)
    cmeta = data.pop("_cmeta", None)
    data["classification"] = {"tier": tier, "metadata": cmeta} if tier else None

    # Resultados de fases posteriores (resumen + extracciones), para el detalle.
    with connection() as conn:
        # `metadata.n` = tamaño real del lote al persistir (el front avisa "resumen del lote · n").
        summary = (
            conn.execute(
                text(
                    """
                    SELECT s.id, s.tier, s.content, s.created_at, s.metadata
                    FROM summaries s
                    JOIN summary_inbox_links sl ON sl.summary_id = s.id
                    WHERE sl.inbox_id = :id AND s.user_id = :uid
                    """
                ),
                {"id": inbox_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
        # Traza de llamadas LLM atribuidas a este mensaje (auditoría/costo por correo).
        llm_calls = (
            conn.execute(
                text(
                    """
                    SELECT request_id, purpose, model, prompt_tokens, completion_tokens,
                           cost_usd, latency_ms, status, error_message, created_at, metadata
                    FROM llm_calls
                    WHERE user_id = :uid AND inbox_id = :id
                    ORDER BY created_at
                    """
                ),
                {"uid": user_id, "id": inbox_id},
            )
            .mappings()
            .all()
        )
        # Adjuntos (media_assets): referencia + estado/texto de OCR. El blob va por /media/{id}.
        media = (
            conn.execute(
                text(
                    """
                    SELECT id, sha256, content_type, filename, extension, size_bytes,
                           ocr_status, ocr_model, ocr_text, ocr_error, ocr_attempts, ocr_done_at
                    FROM media_assets
                    WHERE user_id = :uid AND inbox_id = :id
                    ORDER BY id
                    """
                ),
                {"uid": user_id, "id": inbox_id},
            )
            .mappings()
            .all()
        )
        feedback = get_feedback(conn, inbox_id)
        mark = get_mark(conn, inbox_id)
    # Extracciones: única fuente = read_extractions (de-hardcodeado, itera el registry). Antes este
    # router duplicaba el SQL por módulo y ya había divergido (le faltaba identidades).
    from memex.modules.orchestrator import read_extractions, read_extractions_debug

    data["summary"] = dict(summary) if summary else None
    data["extraction"] = read_extractions(user_id, inbox_id)
    # Estado interno por-módulo (dedup, seam contraparte→identidad, consolidación) para la vista de
    # DEBUG; de-hardcodeado (itera el registry por CAP_DEBUG_INBOX). Mapa slug→filas; {} si ninguno.
    data["extraction_debug"] = read_extractions_debug(user_id, inbox_id)
    # Árbol de traza jerárquica (vista en stack); None ⇒ sin árbol persistido → el front usa el
    # fallback (LlmTrace + extraction_debug). Cuelga del root las llm_calls de ruteo/extracción/OCR.
    from memex.core.trace import read_trace

    data["trace"] = read_trace(user_id, inbox_id)
    calls = [dict(c) for c in llm_calls]
    data["llm"] = {
        "calls": len(calls),
        "cost_usd": float(sum(float(c["cost_usd"]) for c in calls)),
        "prompt_tokens": sum(int(c["prompt_tokens"]) for c in calls),
        "completion_tokens": sum(int(c["completion_tokens"]) for c in calls),
        "items": [{**c, "cost_usd": float(c["cost_usd"])} for c in calls],
    }
    data["media"] = [dict(m) for m in media]
    data["feedback"] = feedback
    data["relevance"] = mark
    data["summarized"] = summary is not None
    data["extracted"] = bool(data["extraction"]["done"])
    return data


@router.get("/{inbox_id}/window", response_model=InboxWindow)
async def get_inbox_window(inbox_id: int, user_id: UserID) -> dict[str, Any]:
    """Lote de procesamiento del mensaje: con quiénes se resumió (o se resumiría) junto.

    Solo lectura, sin LLM. La semántica de `mode` vive en `relations.summary.inbox_window`;
    acá solo se hidratan los miembros con el shape de fila de la lista (orden conversacional).
    """
    from memex.relations.summary import inbox_window

    try:
        win = inbox_window(user_id, inbox_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="not found") from None

    member_ids: list[int] = win["member_ids"]
    members: list[dict[str, Any]] = []
    if member_ids:
        sql = _LIST_ROW_SQL.format(
            where="i.user_id = :uid AND i.id = ANY(:iids)", order="i.occurred_at, i.id"
        )
        with connection() as conn:
            rows = (
                conn.execute(
                    text(sql), {"uid": user_id, "iids": member_ids, "limit": len(member_ids)}
                )
                .mappings()
                .all()
            )
        members = [_map_list_row(r) for r in rows]
    return {"mode": win["mode"], "summary_id": win["summary_id"], "members": members}


def _coerce_payload(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


@router.post("/{inbox_id}/process", response_model=ProcessResponse)
async def process_inbox(inbox_id: int, user_id: UserID) -> dict[str, Any]:
    """Procesa (clasifica) un mensaje puntual de forma determinista — sin LLM.

    Asigna el `tier` (blacklist/batch) según las reglas de `classify()` y lo persiste en
    `classifications` (idempotente por `UNIQUE(inbox_id)`). Si ya estaba clasificado, devuelve
    el tier existente sin re-escribir. Summarize/extract (LLM, por lotes) son otro paso.
    """
    with connection() as conn:
        row = (
            conn.execute(
                text("SELECT payload FROM inbox WHERE id = :id AND user_id = :uid"),
                {"id": inbox_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        existing = (
            conn.execute(
                text("SELECT tier, metadata FROM classifications WHERE inbox_id = :id"),
                {"id": inbox_id},
            )
            .mappings()
            .first()
        )
        if existing:
            meta = existing["metadata"] or {}
            reason = str(meta.get("rule", "")) if isinstance(meta, dict) else ""
            return {
                "inbox_id": inbox_id,
                "tier": existing["tier"],
                "reason": reason,
                "classified": False,
                "already": True,
            }
        result = classify(_coerce_payload(row["payload"]))
        conn.execute(
            text(
                """
                INSERT INTO classifications (user_id, inbox_id, tier, metadata)
                VALUES (:uid, :iid, :tier, CAST(:metadata AS JSONB))
                ON CONFLICT (inbox_id) DO NOTHING
                """
            ),
            {
                "uid": user_id,
                "iid": inbox_id,
                "tier": result.tier,
                "metadata": json.dumps(result.metadata),
            },
        )
    _log.info("inbox.processed", user_id=user_id, inbox_id=inbox_id, tier=result.tier)
    return {
        "inbox_id": inbox_id,
        "tier": result.tier,
        "reason": result.reason,
        "classified": True,
        "already": False,
    }


Scope = Annotated[Literal["individual", "window"], Query()]


@router.post("/{inbox_id}/summarize", response_model=SummarizeResponse)
async def summarize_inbox_endpoint(
    inbox_id: int,
    user_id: UserID,
    scope: Scope = "individual",
    force: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Resume (LLM) un mensaje o su ventana. Requiere clasificación previa."""
    from memex.llm import LLMConfigError, LLMError, LLMQuotaError
    from memex.relations.summary import InboxNotClassifiedError, summarize_inbox

    bind_request_context(inbox_id=inbox_id)  # atribuye el costo LLM a este mensaje
    try:
        return await summarize_inbox(user_id, inbox_id, scope=scope, force=force)
    except LookupError as e:
        raise HTTPException(status_code=404, detail="not found") from e
    except InboxNotClassifiedError as e:
        raise HTTPException(status_code=409, detail="clasificá el mensaje primero") from e
    except LLMConfigError as e:
        raise HTTPException(status_code=422, detail="LLM no configurado (DEEPSEEK_API_KEY)") from e
    except LLMQuotaError as e:
        raise HTTPException(status_code=402, detail="saldo LLM agotado") from e
    except LLMError as e:
        raise HTTPException(status_code=502, detail=f"error de LLM: {e}") from e


@router.post("/{inbox_id}/extract", response_model=ExtractResponse)
async def extract_inbox_endpoint(
    inbox_id: int,
    user_id: UserID,
    scope: Scope = "individual",
    force: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Extrae (módulos finance/calendar, LLM) sobre un mensaje o su ventana. Requiere clasificar."""
    from memex.llm import LLMConfigError, LLMError, LLMQuotaError
    from memex.modules.orchestrator import InboxNotClassifiedError, extract_inbox

    bind_request_context(inbox_id=inbox_id)  # atribuye el costo LLM a este mensaje
    try:
        return await extract_inbox(user_id, inbox_id, scope=scope, force=force)
    except LookupError as e:
        raise HTTPException(status_code=404, detail="not found") from e
    except InboxNotClassifiedError as e:
        raise HTTPException(status_code=409, detail="clasificá el mensaje primero") from e
    except LLMConfigError as e:
        raise HTTPException(status_code=422, detail="LLM no configurado (DEEPSEEK_API_KEY)") from e
    except LLMQuotaError as e:
        raise HTTPException(status_code=402, detail="saldo LLM agotado") from e
    except LLMError as e:
        raise HTTPException(status_code=502, detail=f"error de LLM: {e}") from e


@router.post("/{inbox_id}/reprocess", response_model=ReprocessResponse)
async def reprocess_inbox_endpoint(
    inbox_id: int, user_id: UserID, body: ReprocessRequest
) -> dict[str, Any]:
    """Re-aplica etapas (media/ocr/classify/summarize/extract) a UN mensaje.

    Síncrono y best-effort por etapa: cada una se corre en orden de dependencia y su resultado (o
    error) viaja en `results[<stage>]`. Los lotes van por el CLI `memex-reprocess`.
    """
    from memex.reprocess import reprocess

    with connection() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM inbox WHERE id = :id AND user_id = :uid"),
            {"id": inbox_id, "uid": user_id},
        ).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail="not found")

    bind_request_context(inbox_id=inbox_id)  # atribuye el costo LLM/OCR a este mensaje
    try:
        return await reprocess(user_id, stages=body.stages, targets=[inbox_id], force=body.force)
    except ValueError as e:  # stages inválidas
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/{inbox_id}/feedback", response_model=FeedbackInfo)
async def feedback_inbox_endpoint(
    inbox_id: int, user_id: UserID, body: FeedbackRequest
) -> dict[str, Any]:
    """Registra feedback rápido del usuario sobre un mensaje (SOLO captura — no corrige nada).

    Guarda las categorías + nota y un snapshot de lo observado (tier/remitente/asunto/adjuntos) para
    que el feedback sea autocontenido al evaluar/calibrar después. Upsert: re-reportar reemplaza.
    """
    try:
        with connection() as conn:
            row = (
                conn.execute(
                    text(
                        """
                        SELECT i.payload->>'subject' AS subject,
                               i.payload->'from'->>'email' AS from_email,
                               c.tier AS tier,
                               EXISTS (SELECT 1 FROM media_assets m WHERE m.inbox_id = i.id)
                                   AS has_media
                        FROM inbox i
                        LEFT JOIN classifications c ON c.inbox_id = i.id
                        WHERE i.id = :id AND i.user_id = :uid
                        """
                    ),
                    {"id": inbox_id, "uid": user_id},
                )
                .mappings()
                .first()
            )
            if not row:
                raise HTTPException(status_code=404, detail="not found")
            snapshot = {
                "tier": row["tier"],
                "from_email": row["from_email"],
                "subject": row["subject"],
                "has_media": bool(row["has_media"]),
            }
            return record_feedback(
                conn,
                user_id=user_id,
                inbox_id=inbox_id,
                kinds=body.kinds,
                note=body.note,
                metadata=snapshot,
            )
    except InvalidFeedbackError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/{inbox_id}/relevance", response_model=RelevanceMarkInfo)
async def set_relevance_endpoint(
    inbox_id: int, user_id: UserID, body: RelevanceMarkRequest
) -> dict[str, Any]:
    """Marca manual de relevancia de un mensaje (override por-mensaje; `is_relevant=False` = ruido).

    Captura, no acción: alimenta la métrica (gana sobre la heurística para ESE mensaje),
    NO toca filtros ni clasificación. Marcar uno NO condena a todo el remitente. Upsert por mensaje.
    """
    with connection() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM inbox WHERE id = :id AND user_id = :uid"),
            {"id": inbox_id, "uid": user_id},
        ).scalar()
        if not exists:
            raise HTTPException(status_code=404, detail="not found")
        row = set_mark(
            conn,
            user_id=user_id,
            inbox_id=inbox_id,
            is_relevant=body.is_relevant,
            reason=body.reason,
        )
    _log.info(
        "inbox.relevance.mark", user_id=user_id, inbox_id=inbox_id, is_relevant=body.is_relevant
    )
    return row


@router.delete("/{inbox_id}/relevance", status_code=204)
async def clear_relevance_endpoint(inbox_id: int, user_id: UserID) -> None:
    """Borra la marca manual de relevancia (vuelve a la heurística). 404 si no existía."""
    with connection() as conn:
        ok = clear_mark(conn, user_id=user_id, inbox_id=inbox_id)
    if not ok:
        raise HTTPException(status_code=404, detail="sin marca")


@router.post("/{inbox_id}/classification", response_model=ClassificationInfo)
async def set_classification_endpoint(
    inbox_id: int, user_id: UserID, body: ClassifyRequest
) -> dict[str, Any]:
    """Override MANUAL del tier de un mensaje, aplicado ya (blacklist/batch/individual).

    Marca la clasificación como `manual` (guarda el tier previo en `metadata.prev_tier`). El worker
    determinista no la pisa (inserta solo si falta); un re-clasificar con `force` sí la recalcula.
    """
    meta: dict[str, Any] = {"rule": "manual", "manual": True, "by": "user"}
    with connection() as conn:
        prev = (
            conn.execute(
                text(
                    "SELECT c.tier FROM inbox i LEFT JOIN classifications c ON c.inbox_id = i.id "
                    "WHERE i.id = :id AND i.user_id = :uid"
                ),
                {"id": inbox_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
        if prev is None:
            raise HTTPException(status_code=404, detail="not found")
        if prev["tier"] is not None:
            meta["prev_tier"] = prev["tier"]
        conn.execute(
            text(
                """
                INSERT INTO classifications (user_id, inbox_id, tier, metadata)
                VALUES (:uid, :iid, :tier, CAST(:meta AS JSONB))
                ON CONFLICT (inbox_id) DO UPDATE
                    SET tier = EXCLUDED.tier, metadata = EXCLUDED.metadata
                """
            ),
            {"uid": user_id, "iid": inbox_id, "tier": body.tier, "meta": json.dumps(meta)},
        )
    _log.info("inbox.classification.manual", user_id=user_id, inbox_id=inbox_id, tier=body.tier)
    return {"tier": body.tier, "metadata": meta}
