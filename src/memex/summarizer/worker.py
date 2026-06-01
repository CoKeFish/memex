"""Worker de resumen: lee mensajes clasificados originales y escribe `summaries`.

Server-side + async (la capa LLM es async). Trackea progreso por la AUSENCIA de fila en
`summary_inbox_links` (NO por `inbox.processed_at`), igual que el classifier. Saltea
`blacklist`. El cliente LLM es inyectable (tests con fake, sin red).

Manejo de fallos (cada ventana es independiente y reintentable):
- Input vacío (todos los mensajes renderizan a "") → se saltea sin llamar al LLM.
- Content vacío del LLM → no se persiste (reintentable), se registra `status='error'`.
- Truncado (`finish_reason != 'stop'`) → se persiste pero se marca `truncated` en metadata.
- Excepción del LLM en una ventana → se loguea con contexto, se registra `status='error'`,
  y se sigue con las demás (best-effort). La idempotencia evita re-resumir lo ya hecho.

Concurrencia: `summary_inbox_links` tiene `UNIQUE(inbox_id)` (migración 0007), así que dos
corridas no pueden duplicar un summary (el 2do link viola la UNIQUE → rollback de esa
ventana). Precondición operativa: correr UN worker por vez (no se toma lock a nivel fila;
la UNIQUE protege la integridad, no el doble-gasto de LLM bajo concurrencia real).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from memex.core.deadletter import STAGE_SUMMARIZE, not_in_review_sql, record_failures
from memex.core.media import MAX_OCR_ATTEMPTS, MEDIA_NOT_TERMINAL_SQL
from memex.core.observability import (
    NO_COST as _NO_COST,
)
from memex.core.observability import (
    CostBySource,
    record_llm_call,
)
from memex.core.observability import (
    cost_fields as _cost_fields,
)
from memex.db import connection
from memex.llm import ChatMessage, DeepSeekClient, LLMClient, LLMConfig, LLMQuotaError
from memex.logging import get_logger
from memex.summarizer.prompt import SYSTEM_PROMPT, build_user_content
from memex.summarizer.render import render_payload
from memex.summarizer.windows import (
    MAX_GAP_SECONDS,
    MAX_WINDOW_SIZE,
    Window,
    WorkRow,
    plan_windows,
)

_log = get_logger("memex.summarizer.worker")

# El LIMIT corta a nivel de MENSAJE (no de ventana): una secuencia batch contigua más larga
# que el límite se fragmenta entre corridas (ineficiencia, NO incorrectitud — todo se resume
# igual por idempotencia). Default múltiplo de MAX_WINDOW_SIZE (40) para minimizar cortes.
_DEFAULT_LIMIT = 200
# Tope de tokens de salida del resumen. Si el modelo trunca (finish_reason="length") se
# persiste igual pero se marca truncated=true en metadata para auditar/re-hacer después.
_MAX_TOKENS = 1024
# finish_reason que cuenta como completo. Cualquier otro (length, content_filter) = truncado.
_OK_FINISH = frozenset({"stop"})


@dataclass
class SummarizeStats:
    """Resumen de una corrida: resúmenes escritos, mensajes cubiertos, saltados, errores."""

    summaries: int = 0
    messages: int = 0
    skipped: int = 0
    errors: int = 0
    by_tier: dict[str, int] = field(default_factory=dict)
    #: Costo LLM acumulado de la corrida (total + por source); se emite en `summarizer.run.end`.
    cost: CostBySource = field(default_factory=CostBySource)

    def bump_tier(self, tier: str, messages: int) -> None:
        self.by_tier[tier] = self.by_tier.get(tier, 0) + 1
        self.messages += messages
        self.summaries += 1


def _coerce_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            _log.warning("summarizer.payload.json_error", preview=raw[:80])
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _load_workset(
    user_id: int, source_id: int | None, tier: str | None, limit: int
) -> list[WorkRow]:
    """Mensajes con tier batch/individual cuyo inbox_id NO está en ningún summary link.

    `limit` corta a nivel de MENSAJE (no de ventana): ver nota en `_DEFAULT_LIMIT`.
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

    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    f"""
                    SELECT i.id, i.source_id, i.occurred_at, i.payload, c.tier,
                           COALESCE(ma.ocr_text, '') AS ocr_text
                    FROM classifications c
                    JOIN inbox i ON i.id = c.inbox_id
                    LEFT JOIN summary_inbox_links sl ON sl.inbox_id = i.id
                    LEFT JOIN (
                        SELECT inbox_id, string_agg(ocr_text, E'\n' ORDER BY id) AS ocr_text
                        FROM media_assets
                        WHERE ocr_status = 'ok' AND ocr_text IS NOT NULL AND ocr_text <> ''
                        GROUP BY inbox_id
                    ) ma ON ma.inbox_id = i.id
                    WHERE c.user_id = :uid
                      AND c.tier IN ('batch', 'individual')
                      AND sl.summary_id IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM media_assets m
                          WHERE m.inbox_id = i.id AND {MEDIA_NOT_TERMINAL_SQL}
                      )
                      AND {not_in_review_sql("i.id")}
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


def _persist_summary(
    user_id: int, window: Window, content: str, *, metadata_extra: dict[str, Any]
) -> None:
    """Inserta el summary + sus links en una sola transacción (atomicidad summary↔links).

    Si los links violan `UNIQUE(inbox_id)` (corrida concurrente), la tx hace rollback
    completo: ni summary ni links quedan → sin orphan. El caller lo trata como ventana fallida.
    """
    metadata = {"source_id": window.source_id, "n": len(window.rows), **metadata_extra}
    with connection() as conn:
        summary_id = conn.execute(
            text(
                """
                INSERT INTO summaries (user_id, tier, content, metadata)
                VALUES (:uid, :tier, :content, CAST(:metadata AS JSONB))
                RETURNING id
                """
            ),
            {
                "uid": user_id,
                "tier": window.tier,
                "content": content,
                "metadata": json.dumps(metadata),
            },
        ).scalar_one()
        conn.execute(
            text("INSERT INTO summary_inbox_links (summary_id, inbox_id) VALUES (:sid, :iid)"),
            [{"sid": int(summary_id), "iid": r.inbox_id} for r in window.rows],
        )


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
        source_id=window.source_id,  # atribución first-class por source
        error_message=error_message,
        metadata={"n": len(window.rows)},
    )
    # Acumular en memoria para el resumen por corrida (total + por source).
    stats.cost.record(
        window.source_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
    )


async def _process_window(
    user_id: int, client: LLMClient, window: Window, stats: SummarizeStats
) -> None:
    """Procesa UNA ventana. Lanza si el LLM falla o si persistir choca (lo maneja el caller)."""
    rendered = [render_payload(row.payload, row.ocr_text) for row in window.rows]
    if not any(part.strip() for part in rendered):
        stats.skipped += 1
        _log.warning(
            "summarizer.window.empty_input", source_id=window.source_id, n=len(window.rows)
        )
        return  # sin payload útil → reintentable; no se persiste ni se gasta LLM

    messages = [
        ChatMessage("system", SYSTEM_PROMPT),
        ChatMessage("user", build_user_content(rendered)),
    ]
    result = await client.complete(messages, temperature=0.0, max_tokens=_MAX_TOKENS)
    content = result.content.strip()

    if not content:
        stats.skipped += 1
        _log.warning(
            "summarizer.window.empty_content", source_id=window.source_id, n=len(window.rows)
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
            "summarizer.window.truncated",
            finish_reason=result.finish_reason,
            source_id=window.source_id,
            n=len(window.rows),
        )

    # Persistir ANTES de registrar el costo: así nunca hay un costo 'ok' sin summary.
    _persist_summary(
        user_id,
        window,
        content,
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
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
    )
    stats.bump_tier(window.tier, len(window.rows))


async def run_summarization(
    user_id: int,
    *,
    source_id: int | None = None,
    tier: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    max_window_size: int = MAX_WINDOW_SIZE,
    max_gap_seconds: int = MAX_GAP_SECONDS,
    client: LLMClient | None = None,
) -> SummarizeStats:
    """Resume el work-set no-resumido del user. `client` inyectable (tests con fake).

    `max_window_size`/`max_gap_seconds` son las perillas de ventaneo (ver `plan_windows`).
    Best-effort por ventana: una ventana que falla se loguea + registra y NO frena las demás.
    """
    stats = SummarizeStats()
    windows = plan_windows(
        _load_workset(user_id, source_id, tier, limit),
        max_window_size=max_window_size,
        max_gap_seconds=max_gap_seconds,
    )
    if not windows:
        _log.info("summarizer.run.empty", user_id=user_id, source_id=source_id, tier=tier)
        return stats

    owns_client = client is None
    active: LLMClient = client if client is not None else DeepSeekClient(LLMConfig.from_env())
    try:
        for window in windows:
            try:
                await _process_window(user_id, active, window, stats)
            except LLMQuotaError:
                # Saldo agotado: abortar la corrida (no es best-effort por ventana). El cliente se
                # cierra en el finally y el CLI sale con un mensaje accionable.
                _log.error("summarizer.run.aborted_no_quota", source_id=window.source_id)
                raise
            except Exception as e:  # best-effort: una ventana fallida no frena las demás
                stats.errors += 1
                _log.error(
                    "summarizer.window.failed",
                    tier=window.tier,
                    source_id=window.source_id,
                    n=len(window.rows),
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
        if owns_client and isinstance(active, DeepSeekClient):
            await active.aclose()

    _log.info(
        "summarizer.run.end",
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


# --- procesamiento por mensaje (dashboard: resumir UN inbox o su ventana) ----------- #

#: Tope alto para que la ventana de un mensaje reciente entre en el scan (occurred_at ASC).
_WINDOW_SCAN_LIMIT = 10_000


class InboxNotClassifiedError(Exception):
    """El mensaje existe pero no tiene clasificación (precondición de resumir)."""


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
    """
    row = _load_one_workrow(user_id, inbox_id)

    if not force:
        existing = _existing_summary(user_id, inbox_id)
        if existing is not None:
            return {"status": "already", "messages": 1, **_NO_COST, **existing}

    # Construir el cliente (valida DEEPSEEK_API_KEY) ANTES de borrar nada: si falta la key, `force`
    # no debe destruir el resumen previo sin poder recrearlo.
    owns_client = client is None
    llm: LLMClient = client if client is not None else DeepSeekClient(LLMConfig.from_env())
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
        if owns_client and isinstance(llm, DeepSeekClient):
            await llm.aclose()

    out = _existing_summary(user_id, inbox_id)
    return {
        "status": "ok" if out else "skipped",
        "messages": messages,
        **_cost_fields(stats.cost.total),
        **(out or {}),
    }
