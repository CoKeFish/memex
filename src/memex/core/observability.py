"""Observability primitives shared across the codebase.

- `ingestion_run`: context manager that materializes an `ingestion_runs` row
  for the lifetime of a single ingestor execution, binding `run_id`,
  `source_id`, and `user_id` to structlog contextvars so every log emitted
  inside automatically carries them.
- `record_llm_call`: persistence helper for `llm_calls`. Used across LLM
  consumers to record cost/latency. Signature is fixed to avoid refactor.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog
from sqlalchemy import text

from memex.db import connection
from memex.logging import get_logger

if TYPE_CHECKING:
    from memex.core.source import ActorRunReport
    from memex.ingestors.runner import RunStats


_log = get_logger("memex.core.observability")

_ERROR_MESSAGE_MAX = 1000

#: Mismo cuanto que llm_calls.cost_usd (NUMERIC(10,6)): 6 decimales.
_COST_QUANTUM = Decimal("0.000001")


@dataclass
class IngestionRunHandle:
    """Returned by `ingestion_run`. Caller signals outcome via finalize/fail."""

    id: str
    user_id: int
    source_id: int
    _started_monotonic: float
    _settled: bool = field(default=False, init=False)

    def finalize(self, stats: RunStats) -> None:
        if self._settled:
            return
        self._settled = True
        duration_ms = int((time.monotonic() - self._started_monotonic) * 1000)
        with connection() as conn:
            conn.execute(
                text(
                    """
                    UPDATE ingestion_runs SET
                      status      = 'ok',
                      ended_at    = NOW(),
                      duration_ms = :duration_ms,
                      posted      = :posted,
                      inserted    = :inserted,
                      duplicates  = :duplicates,
                      errors      = :errors,
                      filtered    = :filtered
                    WHERE id = :id
                    """
                ),
                {
                    "id": self.id,
                    "duration_ms": duration_ms,
                    "posted": stats.posted,
                    "inserted": stats.inserted,
                    "duplicates": stats.duplicates,
                    "errors": stats.errors,
                    "filtered": stats.filtered,
                },
            )
        _log.info(
            "ingestor.run.end",
            posted=stats.posted,
            inserted=stats.inserted,
            duplicates=stats.duplicates,
            errors=stats.errors,
            filtered=stats.filtered,
            duration_ms=duration_ms,
        )

    def fail(self, exc: BaseException) -> None:
        if self._settled:
            return
        self._settled = True
        duration_ms = int((time.monotonic() - self._started_monotonic) * 1000)
        error_message = str(exc)[:_ERROR_MESSAGE_MAX] or None
        with connection() as conn:
            conn.execute(
                text(
                    """
                    UPDATE ingestion_runs SET
                      status        = 'failed',
                      ended_at      = NOW(),
                      duration_ms   = :duration_ms,
                      error_class   = :error_class,
                      error_message = :error_message
                    WHERE id = :id
                    """
                ),
                {
                    "id": self.id,
                    "duration_ms": duration_ms,
                    "error_class": type(exc).__name__,
                    "error_message": error_message,
                },
            )
        _log.error(
            "ingestor.run.fatal",
            exc_type=type(exc).__name__,
            exc_msg=error_message,
            duration_ms=duration_ms,
        )

    def _abort(self) -> None:
        if self._settled:
            return
        self._settled = True
        duration_ms = int((time.monotonic() - self._started_monotonic) * 1000)
        with connection() as conn:
            conn.execute(
                text(
                    """
                    UPDATE ingestion_runs SET
                      status      = 'aborted',
                      ended_at    = NOW(),
                      duration_ms = :duration_ms
                    WHERE id = :id
                    """
                ),
                {"id": self.id, "duration_ms": duration_ms},
            )
        _log.warning("ingestor.run.aborted", duration_ms=duration_ms)


@contextmanager
def ingestion_run(
    *,
    user_id: int,
    source_id: int,
    trigger: str,
) -> Iterator[IngestionRunHandle]:
    """Wrap a single ingestor run with persistent state in `ingestion_runs`.

    Yields a handle. The caller MUST call `handle.finalize(stats)` on success
    or `handle.fail(exc)` on a caught exception. If neither is called (e.g.
    the process is killed mid-run), the row is marked 'aborted' on __exit__.
    """
    run_id = str(uuid4())
    started_monotonic = time.monotonic()

    with connection() as conn:
        conn.execute(
            text(
                """
                INSERT INTO ingestion_runs
                  (id, user_id, source_id, trigger, status)
                VALUES (:id, :user_id, :source_id, :trigger, 'running')
                """
            ),
            {
                "id": run_id,
                "user_id": user_id,
                "source_id": source_id,
                "trigger": trigger,
            },
        )

    structlog.contextvars.bind_contextvars(
        run_id=run_id,
        source_id=source_id,
        user_id=user_id,
        trigger=trigger,
    )
    _log.info("ingestor.run.start", trigger=trigger)

    handle = IngestionRunHandle(
        id=run_id,
        user_id=user_id,
        source_id=source_id,
        _started_monotonic=started_monotonic,
    )
    try:
        yield handle
    finally:
        if not handle._settled:
            handle._abort()
        structlog.contextvars.unbind_contextvars("run_id", "source_id", "user_id", "trigger")


def record_llm_call(
    *,
    user_id: int,
    purpose: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: Decimal,
    latency_ms: int,
    status: str,
    inbox_id: int | None = None,
    source_id: int | None = None,
    cache_hit_tokens: int = 0,
    error_message: str | None = None,
    metadata: dict[str, Any] | None = None,
    response_text: str | None = None,
) -> int:
    """Persist a single LLM call to `llm_calls` and log `llm.call`.

    Single writer of `llm_calls`. `source_id` is a first-class column (migration
    0014) so cost can be cut per source; calendar decisions span sources and pass
    `source_id=None` (identified by `purpose LIKE 'calendar%'`). `request_id` is read
    from structlog contextvars for correlation with the HTTP request log line.

    Per-source cost aggregation query (LEFT JOIN + label so null-source calendar rows
    are visible, not lost):

        SELECT COALESCE(
                 s.name,
                 CASE WHEN lc.purpose LIKE 'calendar%' THEN '(calendar)'
                      ELSE '(sin source)' END
               ) AS source,
               COUNT(*), SUM(lc.prompt_tokens + lc.completion_tokens) AS tokens,
               SUM(lc.cost_usd) AS cost
        FROM llm_calls lc
        LEFT JOIN sources s ON s.id = lc.source_id
        GROUP BY 1 ORDER BY cost DESC;
    """
    ctx = structlog.contextvars.get_contextvars()
    request_id = ctx.get("request_id") if isinstance(ctx.get("request_id"), str) else None
    # Permite atribuir el costo a un inbox puntual sin tocar los workers (el endpoint de
    # procesamiento por-mensaje bindea `inbox_id` a los contextvars): si el caller no lo pasó,
    # se toma del contexto. Las corridas por lotes no lo bindean → queda en None (por source).
    if inbox_id is None:
        ctx_iid = ctx.get("inbox_id")
        inbox_id = ctx_iid if isinstance(ctx_iid, int) else None

    with connection() as conn:
        row_id = conn.execute(
            text(
                """
                INSERT INTO llm_calls
                  (user_id, request_id, inbox_id, source_id, purpose, model,
                   prompt_tokens, completion_tokens, cache_hit_tokens, cost_usd,
                   latency_ms, status, error_message, metadata, response_text)
                VALUES
                  (:user_id, :request_id, :inbox_id, :source_id, :purpose, :model,
                   :prompt_tokens, :completion_tokens, :cache_hit_tokens, :cost_usd,
                   :latency_ms, :status, :error_message, CAST(:metadata AS JSONB), :response_text)
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "request_id": request_id,
                "inbox_id": inbox_id,
                "source_id": source_id,
                "purpose": purpose,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cache_hit_tokens": cache_hit_tokens,
                "cost_usd": str(cost_usd),
                "latency_ms": latency_ms,
                "status": status,
                "error_message": error_message[:_ERROR_MESSAGE_MAX] if error_message else None,
                "metadata": json.dumps(metadata or {}),
                "response_text": response_text,
            },
        ).scalar_one()

    # Una falla de LLM se emite a nivel ERROR (no info) para que /logs la cuente: antes TODA
    # `llm.call` iba a info y las fallas (status='error') no aparecían en log_events (H-9). Esta es
    # la línea central que garantiza que cada fila status='error' tenga su log_event de error; los
    # paths que ya emiten su propio _log.error/_log.warning siguen igual (mayor detalle, no daño).
    log = _log.error if status == "error" else _log.info
    log(
        "llm.call",
        purpose=purpose,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cost_usd=str(cost_usd),
        latency_ms=latency_ms,
        status=status,
        error_message=error_message,
        inbox_id=inbox_id,
        source_id=source_id,
    )
    return int(row_id)


def record_apify_runs(
    *,
    user_id: int,
    source_id: int | None,
    ingestion_run_id: str | None,
    reports: list[ActorRunReport],
) -> Decimal | None:
    """Persiste los reports de corridas de actor (Apify) y devuelve el costo agregado.

    Single writer de `apify_runs` (espejo de `record_llm_call`). Una fila por run de
    actor — también error/timeout (un run fallido pudo cobrar lo consumido). En la
    misma transacción actualiza el agregado `ingestion_runs.api_cost_usd` cuando la
    corrida existe (`ingestion_run_id` es None en dry-run y `discover`, que gastan
    igual). Devuelve la suma cuantizada a 6 decimales, o None si ningún report trajo
    costo (Apify lo asienta tarde a veces — la fila queda con cost_usd NULL, visible).
    """
    if not reports:
        return None
    total: Decimal | None = None
    with connection() as conn:
        for r in reports:
            cost: Decimal | None = None
            if r.cost_usd is not None:
                cost = Decimal(str(r.cost_usd)).quantize(_COST_QUANTUM)
                total = cost if total is None else total + cost
            conn.execute(
                text(
                    """
                    INSERT INTO apify_runs
                      (user_id, source_id, ingestion_run_id, platform, account, actor_id,
                       apify_run_id, status, items_scraped, items_kept, cost_usd,
                       charged_events, started_at, finished_at)
                    VALUES
                      (:user_id, :source_id, :ingestion_run_id, :platform, :account, :actor_id,
                       :apify_run_id, :status, :items_scraped, :items_kept, :cost_usd,
                       CAST(:charged_events AS JSONB), :started_at, :finished_at)
                    """
                ),
                {
                    "user_id": user_id,
                    "source_id": source_id,
                    "ingestion_run_id": ingestion_run_id,
                    "platform": r.platform,
                    "account": r.account,
                    "actor_id": r.actor_id,
                    "apify_run_id": r.apify_run_id,
                    "status": r.status,
                    "items_scraped": r.items_scraped,
                    "items_kept": r.items_kept,
                    "cost_usd": str(cost) if cost is not None else None,
                    "charged_events": json.dumps(r.charged_events)
                    if r.charged_events is not None
                    else None,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                },
            )
        if ingestion_run_id is not None and total is not None:
            conn.execute(
                text("UPDATE ingestion_runs SET api_cost_usd = :cost WHERE id = :id"),
                {"cost": str(total), "id": ingestion_run_id},
            )
    _log.info(
        "apify.runs.recorded",
        runs=len(reports),
        cost_usd=str(total) if total is not None else None,
        source_id=source_id,
        ingestion_run_id=ingestion_run_id,
    )
    return total


@dataclass
class CostAccum:
    """Acumulador de costo (llamadas + tokens + USD) de un bucket."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal = field(default_factory=lambda: Decimal(0))


#: Campos de costo "cero" para respuestas sin corrida LLM (p. ej. "ya estaba resumido").
NO_COST: dict[str, Any] = {"calls": 0, "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0}


def cost_fields(accum: CostAccum) -> dict[str, Any]:
    """Campos planos de costo de un acumulador, listos para una respuesta JSON."""
    return {
        "calls": accum.calls,
        "cost_usd": float(accum.cost_usd),
        "prompt_tokens": accum.prompt_tokens,
        "completion_tokens": accum.completion_tokens,
    }


@dataclass
class CostBySource:
    """Acumulador de costo por source, en memoria, para el resumen del `*.run.end`.

    `by_source[None]` es el bucket sin source (p. ej. calendar): se renderiza como
    "sin_source" en `log_fields` para que el costo sin atribución se VEA, no se pierda.
    """

    total: CostAccum = field(default_factory=CostAccum)
    by_source: dict[int | None, CostAccum] = field(default_factory=dict)

    def record(
        self,
        source_id: int | None,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: Decimal,
    ) -> None:
        """Suma una llamada al total y al bucket de su source (None = sin source)."""
        for bucket in (self.total, self.by_source.setdefault(source_id, CostAccum())):
            bucket.calls += 1
            bucket.prompt_tokens += prompt_tokens
            bucket.completion_tokens += completion_tokens
            bucket.cost_usd += cost_usd

    def log_fields(self) -> dict[str, Any]:
        """Campos planos para el `*.run.end`; el bucket None se renderiza 'sin_source'."""
        return {
            "llm_calls": self.total.calls,
            "llm_prompt_tokens": self.total.prompt_tokens,
            "llm_completion_tokens": self.total.completion_tokens,
            "llm_cost_usd": str(self.total.cost_usd),
            "llm_cost_by_source": {
                ("sin_source" if sid is None else str(sid)): str(acc.cost_usd)
                for sid, acc in self.by_source.items()
            },
        }
