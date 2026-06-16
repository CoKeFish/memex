"""Resumen de mensajes — la ÚNICA pieza que produce `summaries`, dentro de la fase de
co-ocurrencia (C.7). Reemplaza al summarizer separado (ADR-021 / consolidación 0069+).

Granularidad por UNIDAD (igual que ventaneaba el summarizer, `plan_windows`):
- `individual` (correo) → unidad = el mensaje (1 resumen 1:1).
- `batch`/chat → unidad = el LOTE (ventana por `source_id` + gap temporal). El chat SIEMPRE va
  en lote por volumen, nunca individual.

Dos productores coordinados, un solo dueño (esta fase):
- El correo individual CON pares de co-ocurrencia se resume en la MISMA llamada que los juzga
  (`relations.per_message`, consumer `relations_confirm`): reúso, no se paga dos veces.
- Todo lo demás sin resumir (lote chat, lote batch-email, correo individual sin pares) lo cubre
  `run_summaries` acá (consumer `summarizer`), una llamada por unidad, con un prompt según la
  naturaleza (correo vs conversación/lote).

Coherencia: el camino de juicio persiste el resumen SOLO para tier `individual`; los `batch`
quedan sin resumir hasta que `run_summaries` los agrupa en lotes (evita lotes partidos). Cursor =
ausencia de fila en `summary_inbox_links` (igual que el classifier). `persist_summary` re-linkea
(respeta `UNIQUE(inbox_id)`) y hace GC del resumen viejo huérfano, así sirve tanto al alta normal
como al `force` on-demand. Best-effort por unidad; el saldo agotado (`LLMQuotaError`) aborta la
corrida. Precondición operativa: un worker por vez (la UNIQUE protege integridad, no doble-gasto).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.deadletter import STAGE_SUMMARIZE, not_in_review_sql, record_failures
from memex.core.media import MAX_OCR_ATTEMPTS, MEDIA_NOT_TERMINAL_SQL
from memex.core.observability import NO_COST as _NO_COST
from memex.core.observability import CostBySource, record_llm_call
from memex.core.observability import cost_fields as _cost_fields
from memex.db import connection
from memex.llm import ChatMessage, LLMClient, LLMQuotaError, aclose_llm, build_llm_client
from memex.logging import get_logger
from memex.processing.render import render_payload
from memex.processing.windows import (
    MAX_GAP_SECONDS,
    MAX_WINDOW_SIZE,
    Window,
    WorkRow,
    plan_windows,
)
from memex.relevance.verdicts import workset_gate_clause, workset_tier_clause

_log = get_logger("memex.relations.summary")

# El LIMIT corta a nivel de MENSAJE (no de ventana): una secuencia batch contigua más larga que
# el límite se fragmenta entre corridas (ineficiencia, NO incorrectitud — todo se resume igual por
# idempotencia). Default múltiplo de MAX_WINDOW_SIZE (40) para minimizar cortes.
_DEFAULT_LIMIT = 200
# Tope de tokens de salida del resumen. Si el modelo trunca (finish_reason="length") se persiste
# igual pero se marca truncated=true en metadata para auditar/re-hacer después.
_MAX_TOKENS = 1024
# finish_reason que cuenta como completo. Cualquier otro (length, content_filter) = truncado.
_OK_FINISH = frozenset({"stop"})
# Tope alto para que la ventana de un mensaje reciente entre en el scan (occurred_at ASC).
_WINDOW_SCAN_LIMIT = 10_000


# --- prompts por naturaleza (correo individual vs conversación/lote) ------------------- #

#: Resumen de UN correo (unidad individual). Misma intención que el bloque `summary` del juicio
#: por-mensaje, para que un correo se resuma igual lo juzgue o no la co-ocurrencia.
INDIVIDUAL_SUMMARY_PROMPT = (
    "Sos un asistente que resume UN correo o mensaje personal en español.\n"
    "Resumí de forma CONCISA y FIEL lo importante de ESE mensaje: quién escribe, qué informa o "
    "pide, montos, fechas, decisiones y pendientes.\n"
    "NO inventes nada que no esté en el mensaje. NO incluyas preámbulos ni meta-comentarios.\n"
    "Devolvé SOLO el resumen, en texto plano."
)

#: Resumen de un LOTE: una ventana de chat (conversación) o un lote de correos del mismo
#: remitente. La unidad es el CONJUNTO, no un mensaje suelto.
BATCH_SUMMARY_PROMPT = (
    "Sos un asistente que resume una VENTANA de mensajes en español: una conversación de chat o "
    "un lote de correos del mismo remitente que llegaron juntos.\n"
    "Resumí lo que se habló o recibió EN EL CONJUNTO: de qué se trató, quiénes participan, "
    "montos, fechas, decisiones y pendientes. Es el resumen del LOTE entero, no de un solo "
    "mensaje.\n"
    "NO inventes nada que no esté en los mensajes. NO incluyas preámbulos ni meta-comentarios.\n"
    "Devolvé SOLO el resumen, en texto plano."
)


def _system_prompt_for(tier: str) -> str:
    """El prompt según la naturaleza de la unidad: `individual` (correo) vs lote (`batch`/chat)."""
    return INDIVIDUAL_SUMMARY_PROMPT if tier == "individual" else BATCH_SUMMARY_PROMPT


def _build_user_content(rendered: Sequence[str]) -> str:
    """Arma el bloque de mensajes originales renderizados para el turno `user`."""
    return "Mensajes:\n\n" + "\n\n".join(rendered)


# --- stats + errores ------------------------------------------------------------------- #


@dataclass
class SummarizeStats:
    """Resumen de una corrida de `run_summaries`: resúmenes escritos, mensajes cubiertos,
    saltados, errores, costo por source."""

    summaries: int = 0
    messages: int = 0
    skipped: int = 0
    errors: int = 0
    by_tier: dict[str, int] = field(default_factory=dict)
    cost: CostBySource = field(default_factory=CostBySource)

    def bump_tier(self, tier: str, messages: int) -> None:
        self.by_tier[tier] = self.by_tier.get(tier, 0) + 1
        self.messages += messages
        self.summaries += 1


class InboxNotClassifiedError(Exception):
    """El mensaje existe pero no tiene clasificación (precondición de resumir)."""


def _coerce_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            _log.warning("summary.payload.json_error", preview=raw[:80])
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# --- workset (gate de relevancia + tiers + OCR terminal + dead-letter) ------------------ #


def _load_workset(
    user_id: int,
    source_id: int | None,
    tier: str | None,
    limit: int,
    inbox_ids: list[int] | None = None,
) -> list[WorkRow]:
    """Mensajes con tier batch/individual cuyo inbox_id NO está en ningún summary link.

    `limit` corta a nivel de MENSAJE (no de ventana): ver nota en `_DEFAULT_LIMIT`.
    `inbox_ids` acota a un set explícito (reproceso por lote): conserva los mismos filtros de
    tier/gates, así blacklist se sigue saltando y batch se sigue ventaneando.
    """
    params: dict[str, Any] = {
        "uid": user_id,
        "limit": limit,
        "ocrmax": MAX_OCR_ATTEMPTS,
        "dl_stage": STAGE_SUMMARIZE,
    }
    filters = ""
    if source_id is not None:
        filters += " AND i.source_id = :sid"
        params["sid"] = source_id
    if tier is not None:
        filters += " AND c.tier = :tier"
        params["tier"] = tier
    if inbox_ids is not None:
        filters += " AND i.id = ANY(:iids)"
        params["iids"] = inbox_ids

    with connection() as conn:
        # Gate de relevancia (correos): encendido, un correo sin relevancia efectiva (mark manual
        # o veredicto `relevant`) no entra al workset; apagado → cláusula vacía.
        gate_clause, gate_params = workset_gate_clause(conn, user_id)
        params.update(gate_params)
        # Tier = dial de costo: apagado excluye blacklist; encendido lo decide la relevancia.
        tier_clause, _ = workset_tier_clause(conn, user_id)
        rows = (
            conn.execute(
                text(
                    f"""
                    SELECT i.id, i.source_id, i.occurred_at, i.payload, c.tier,
                           COALESCE(ma.ocr_text, '') AS ocr_text
                    FROM classifications c
                    JOIN inbox i   ON i.id = c.inbox_id
                    JOIN sources s ON s.id = i.source_id
                    LEFT JOIN summary_inbox_links sl ON sl.inbox_id = i.id
                    LEFT JOIN (
                        SELECT inbox_id, string_agg(ocr_text, E'\n' ORDER BY id) AS ocr_text
                        FROM media_assets
                        WHERE ocr_status = 'ok' AND ocr_text IS NOT NULL AND ocr_text <> ''
                        GROUP BY inbox_id
                    ) ma ON ma.inbox_id = i.id
                    WHERE c.user_id = :uid
                      {tier_clause}
                      AND sl.summary_id IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM media_assets m
                          WHERE m.inbox_id = i.id AND {MEDIA_NOT_TERMINAL_SQL}
                      )
                      AND {not_in_review_sql("i.id")}
                      {gate_clause}
                      {filters}
                    ORDER BY i.source_id, i.occurred_at
                    LIMIT :limit
                    """
                ),
                params,
            )
            .mappings()
            .all()
        )

    return [
        WorkRow(
            inbox_id=int(r["id"]),
            source_id=int(r["source_id"]),
            occurred_at=r["occurred_at"],
            payload=_coerce_payload(r["payload"]),
            tier=str(r["tier"]),
            ocr_text=str(r["ocr_text"]),
        )
        for r in rows
    ]


# --- persistencia (único upsert de `summaries`, compartido con per_message) -------------- #


def persist_summary(
    conn: Connection,
    user_id: int,
    inbox_ids: Sequence[int],
    content: str,
    *,
    tier: str,
    origin: str,
    source_id: int | None = None,
    metadata_extra: dict[str, Any] | None = None,
) -> int | None:
    """Inserta el resumen y lo linkea a TODOS los `inbox_ids` de la unidad (1 para individual, N
    para el lote), respetando el `UNIQUE(inbox_id)` de `summary_inbox_links` (re-linkea) y haciendo
    GC del resumen viejo si quedó huérfano. Devuelve el nuevo `summary_id`, o None si `content`
    está vacío (no se persiste → reintentable). Único punto que escribe `summaries`."""
    if not content.strip():
        return None
    ids = list(inbox_ids)
    old_ids = [
        int(r[0])
        for r in conn.execute(
            text("SELECT DISTINCT summary_id FROM summary_inbox_links WHERE inbox_id = ANY(:iids)"),
            {"iids": ids},
        ).all()
    ]
    metadata: dict[str, Any] = {"origin": origin, "n": len(ids)}
    if source_id is not None:
        metadata["source_id"] = source_id
    if metadata_extra:
        metadata.update(metadata_extra)
    new_id = conn.execute(
        text(
            """
            INSERT INTO summaries (user_id, tier, content, metadata)
            VALUES (:uid, :tier, :content, CAST(:metadata AS JSONB))
            RETURNING id
            """
        ),
        {"uid": user_id, "tier": tier, "content": content, "metadata": json.dumps(metadata)},
    ).scalar_one()
    conn.execute(
        text("DELETE FROM summary_inbox_links WHERE inbox_id = ANY(:iids)"),
        {"iids": ids},
    )
    conn.execute(
        text("INSERT INTO summary_inbox_links (summary_id, inbox_id) VALUES (:sid, :iid)"),
        [{"sid": int(new_id), "iid": iid} for iid in ids],
    )
    for old in old_ids:
        conn.execute(
            text(
                "DELETE FROM summaries s WHERE s.id = :old AND s.user_id = :uid "
                "AND NOT EXISTS (SELECT 1 FROM summary_inbox_links WHERE summary_id = :old)"
            ),
            {"old": old, "uid": user_id},
        )
    return int(new_id)


def _record_cost(
    user_id: int,
    window: Window,
    stats: SummarizeStats,
    *,
    status: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: Decimal,
    latency_ms: int,
    cache_hit_tokens: int = 0,
    error_message: str | None = None,
) -> None:
    record_llm_call(
        user_id=user_id,
        purpose=f"summarize_{window.tier}",
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        status=status,
        source_id=window.source_id,
        error_message=error_message,
        metadata={"n": len(window.rows)},
    )
    stats.cost.record(
        window.source_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
    )


async def _process_window(
    user_id: int, client: LLMClient, window: Window, stats: SummarizeStats
) -> None:
    """Procesa UNA unidad (mensaje individual o lote): renderiza, una llamada de resumen con el
    prompt según naturaleza, persiste. Lanza si el LLM falla o si persistir choca (lo maneja el
    caller)."""
    rendered = [render_payload(row.payload, row.ocr_text) for row in window.rows]
    if not any(part.strip() for part in rendered):
        stats.skipped += 1
        _log.warning(
            "summary.window.empty_input",
            source_id=window.source_id,
            n=len(window.rows),
            inbox_ids=[r.inbox_id for r in window.rows],
        )
        return  # sin payload útil → reintentable; no se persiste ni se gasta LLM

    messages = [
        ChatMessage("system", _system_prompt_for(window.tier)),
        ChatMessage("user", _build_user_content(rendered)),
    ]
    result = await client.complete(messages, temperature=0.0, max_tokens=_MAX_TOKENS)
    content = result.content.strip()

    if not content:
        stats.skipped += 1
        _log.warning(
            "summary.window.empty_content",
            source_id=window.source_id,
            n=len(window.rows),
            inbox_ids=[r.inbox_id for r in window.rows],
        )
        _record_cost(
            user_id,
            window,
            stats,
            status="error",
            model=result.model,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            cache_hit_tokens=result.usage.cache_hit_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            error_message="empty content",
        )
        return  # no se persiste → reintentable

    truncated = result.finish_reason is not None and result.finish_reason not in _OK_FINISH
    if truncated:
        _log.warning(
            "summary.window.truncated",
            finish_reason=result.finish_reason,
            source_id=window.source_id,
            n=len(window.rows),
            inbox_ids=[r.inbox_id for r in window.rows],
        )

    # Persistir ANTES de registrar el costo: así nunca hay un costo 'ok' sin summary.
    with connection() as conn:
        persist_summary(
            conn,
            user_id,
            [r.inbox_id for r in window.rows],
            content,
            tier=window.tier,
            origin="summarize",
            source_id=window.source_id,
            metadata_extra={"truncated": truncated, "finish_reason": result.finish_reason},
        )
    _record_cost(
        user_id,
        window,
        stats,
        status="ok",
        model=result.model,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cache_hit_tokens=result.usage.cache_hit_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
    )
    stats.bump_tier(window.tier, len(window.rows))


def _force_clear_summaries(user_id: int, inbox_ids: list[int]) -> list[int]:
    """Borra los summaries que tocan a `inbox_ids` y devuelve el set EXPANDIDO (targets +
    co-miembros de sus ventanas) para re-resumir la ventana COMPLETA sin dejar co-miembros
    huérfanos. Espeja a nivel lote el `force` per-mensaje de `summarize_inbox`."""
    with connection() as conn:
        affected = (
            conn.execute(
                text(
                    "SELECT DISTINCT inbox_id FROM summary_inbox_links WHERE summary_id IN "
                    "(SELECT summary_id FROM summary_inbox_links WHERE inbox_id = ANY(:iids))"
                ),
                {"iids": inbox_ids},
            )
            .scalars()
            .all()
        )
        conn.execute(
            text(
                "DELETE FROM summaries WHERE user_id = :uid AND id IN "
                "(SELECT summary_id FROM summary_inbox_links WHERE inbox_id = ANY(:iids))"
            ),
            {"uid": user_id, "iids": inbox_ids},
        )
    return sorted(set(inbox_ids) | {int(x) for x in affected})


async def run_summaries(
    user_id: int,
    *,
    source_id: int | None = None,
    tier: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    max_window_size: int = MAX_WINDOW_SIZE,
    max_gap_seconds: int = MAX_GAP_SECONDS,
    inbox_ids: list[int] | None = None,
    force: bool = False,
    client: LLMClient | None = None,
) -> SummarizeStats:
    """Resume el work-set no-resumido del user, una llamada por UNIDAD (`plan_windows`). `client`
    inyectable (tests con fake); default: consumer `summarizer`.

    `inbox_ids` acota a un set explícito (reproceso por lote): respeta los mismos tiers que el
    daemon (salta blacklist, ventanea batch). `force` re-resume esos ids aunque ya tengan summary
    (los borra primero, expandiendo a la ventana completa). Best-effort por ventana."""
    stats = SummarizeStats()
    load_ids = inbox_ids
    if force and inbox_ids:
        load_ids = _force_clear_summaries(user_id, inbox_ids)
    # Un set explícito no debe cortarse por el LIMIT a nivel de mensaje.
    eff_limit = max(limit, len(load_ids)) if load_ids is not None else limit
    windows = plan_windows(
        _load_workset(user_id, source_id, tier, eff_limit, load_ids),
        max_window_size=max_window_size,
        max_gap_seconds=max_gap_seconds,
    )
    if not windows:
        _log.info("summary.run.empty", user_id=user_id, source_id=source_id, tier=tier)
        return stats

    owns_client = client is None
    active: LLMClient = client or build_llm_client("summarizer", user_id=user_id)
    try:
        for window in windows:
            try:
                await _process_window(user_id, active, window, stats)
            except LLMQuotaError:
                # Saldo agotado: abortar la corrida (no es best-effort por ventana).
                _log.error("summary.run.aborted_no_quota", source_id=window.source_id)
                raise
            except Exception as e:  # best-effort: una ventana fallida no frena las demás
                stats.errors += 1
                _log.error(
                    "summary.window.failed",
                    tier=window.tier,
                    source_id=window.source_id,
                    n=len(window.rows),
                    inbox_ids=[r.inbox_id for r in window.rows],
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
                _record_cost(
                    user_id,
                    window,
                    stats,
                    status="error",
                    model="unknown",
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_usd=Decimal("0"),
                    latency_ms=0,
                    error_message=str(e)[:500],
                )
                # Dead-letter: suma fallo a cada mensaje; al 3er fallo → 'pendiente de revisión'.
                record_failures(user_id, STAGE_SUMMARIZE, [r.inbox_id for r in window.rows], str(e))
    finally:
        if owns_client:
            await aclose_llm(active)

    _log.info(
        "summary.run.end",
        user_id=user_id,
        source_id=source_id,
        summaries=stats.summaries,
        messages=stats.messages,
        skipped=stats.skipped,
        errors=stats.errors,
        **{f"tier_{tier_name}": count for tier_name, count in stats.by_tier.items()},
        **stats.cost.log_fields(),
    )
    return stats


# --- on-demand por mensaje (dashboard: resumir UN inbox o su ventana) ------------------- #


def _load_one_workrow(user_id: int, inbox_id: int) -> WorkRow:
    """Arma la WorkRow de un inbox puntual (inbox + tier + ocr). Lanza si no existe/clasifica."""
    with connection() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT i.source_id, i.occurred_at, i.payload, c.tier,
                           COALESCE((
                               SELECT string_agg(ocr_text, E'\n' ORDER BY id)
                               FROM media_assets
                               WHERE inbox_id = i.id AND ocr_status = 'ok'
                                 AND ocr_text IS NOT NULL AND ocr_text <> ''
                           ), '') AS ocr_text
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
    if row is None:
        raise LookupError(f"inbox {inbox_id} not found")
    if row["tier"] is None:
        raise InboxNotClassifiedError(f"inbox {inbox_id} not classified")
    return WorkRow(
        inbox_id=inbox_id,
        source_id=int(row["source_id"]),
        occurred_at=row["occurred_at"],
        payload=_coerce_payload(row["payload"]),
        tier=str(row["tier"]),
        ocr_text=str(row["ocr_text"]),
    )


def _existing_summary(user_id: int, inbox_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT s.id, s.tier, s.content, s.created_at
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
    return dict(row) if row else None


def inbox_window(user_id: int, inbox_id: int) -> dict[str, Any]:
    """Lote de procesamiento de un mensaje (solo lectura): `{mode, summary_id, member_ids}`.

    - Con summary → co-linkeados al MISMO summary (`mode="summary"`): el lote que YA se procesó
      junto (los miembros pueden estar resumidos, por eso NO se usa el work-set acá).
    - Sin summary y tier batch/individual → ventana PROSPECTIVA (`mode="prospective"`): lo que
      `plan_windows` armaría hoy sobre el work-set no-resumido de la fuente — exactamente lo que
      haría «Resumir su lote». Cambia entre corridas: los vecinos ya resumidos salen del work-set.
    - blacklist / sin clasificar → sin lote (`mode="none"`, sin miembros).

    Lanza `LookupError` si el inbox no existe o es de otro user (→ 404 en el router).
    """
    try:
        row = _load_one_workrow(user_id, inbox_id)
    except InboxNotClassifiedError:
        return {"mode": "none", "summary_id": None, "member_ids": []}

    existing = _existing_summary(user_id, inbox_id)
    if existing is not None:
        with connection() as conn:
            ids = (
                conn.execute(
                    text(
                        "SELECT inbox_id FROM summary_inbox_links "
                        "WHERE summary_id = :sid ORDER BY inbox_id"
                    ),
                    {"sid": int(existing["id"])},
                )
                .scalars()
                .all()
            )
        return {
            "mode": "summary",
            "summary_id": int(existing["id"]),
            "member_ids": [int(x) for x in ids],
        }

    if row.tier not in ("batch", "individual"):
        return {"mode": "none", "summary_id": None, "member_ids": []}

    windows = plan_windows(_load_workset(user_id, row.source_id, None, _WINDOW_SCAN_LIMIT))
    window = next((w for w in windows if any(r.inbox_id == inbox_id for r in w.rows)), None)
    member_ids = [r.inbox_id for r in window.rows] if window is not None else [inbox_id]
    return {"mode": "prospective", "summary_id": None, "member_ids": member_ids}


async def summarize_inbox(
    user_id: int,
    inbox_id: int,
    *,
    scope: str = "individual",
    force: bool = False,
    client: LLMClient | None = None,
) -> dict[str, Any]:
    """Resume UN mensaje (individual) o su ventana (scope='window'). Reusa `_process_window`.

    Idempotente: si ya está resumido y no es `force`, devuelve el existente. `force` lo borra
    (cascade a sus links) y re-corre. Lanza `LookupError` / `InboxNotClassifiedError`.

    NO aplica el gate de relevancia: el click explícito por-mensaje es un bypass deliberado
    (paridad con blacklist, que este camino también resume a pedido)."""
    row = _load_one_workrow(user_id, inbox_id)

    if not force:
        existing = _existing_summary(user_id, inbox_id)
        if existing is not None:
            return {"status": "already", "messages": 1, **_NO_COST, **existing}

    # Construir el cliente ANTES de borrar nada: si falta config, `force` no debe destruir el
    # resumen previo sin poder recrearlo.
    owns_client = client is None
    llm: LLMClient = client or build_llm_client("summarizer", user_id=user_id)
    stats = SummarizeStats()
    messages = 0
    try:
        if force:
            with connection() as conn:
                conn.execute(
                    text(
                        "DELETE FROM summaries WHERE id IN "
                        "(SELECT summary_id FROM summary_inbox_links WHERE inbox_id = :id)"
                    ),
                    {"id": inbox_id},
                )
        window: Window | None = None
        if scope == "window" and row.tier in ("batch", "individual"):
            workset = _load_workset(user_id, row.source_id, None, _WINDOW_SCAN_LIMIT)
            windows = plan_windows(workset)
            window = next((w for w in windows if any(r.inbox_id == inbox_id for r in w.rows)), None)
        if window is None:
            window = Window("individual", row.source_id, (row,))
        messages = len(window.rows)
        await _process_window(user_id, llm, window, stats)
    finally:
        if owns_client:
            await aclose_llm(llm)

    out = _existing_summary(user_id, inbox_id)
    return {
        "status": "ok" if out else "skipped",
        "messages": messages,
        **_cost_fields(stats.cost.total),
        **(out or {}),
    }
