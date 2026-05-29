"""Observability primitives shared across the codebase.

- `ingestion_run`: context manager that materializes an `ingestion_runs` row
  for the lifetime of a single ingestor execution, binding `run_id`,
  `source_id`, and `user_id` to structlog contextvars so every log emitted
  inside automatically carries them.
- `record_llm_call`: persistence helper for `llm_calls`. Not used yet — the
  summarizer is the eventual caller. Signature is fixed to avoid refactor.
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
    from memex.ingestors.runner import RunStats


_log = get_logger("memex.core.observability")

_ERROR_MESSAGE_MAX = 1000


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
        structlog.contextvars.unbind_contextvars("run_id", "source_id", "user_id")


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
    error_message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Persist a single LLM call to `llm_calls` and log `llm.call`.

    Not used yet — fixed signature so the future summarizer can plug in
    without refactor. `request_id` is read from structlog contextvars for
    correlation with the HTTP request log line (if any).
    """
    ctx = structlog.contextvars.get_contextvars()
    request_id = ctx.get("request_id") if isinstance(ctx.get("request_id"), str) else None

    with connection() as conn:
        row_id = conn.execute(
            text(
                """
                INSERT INTO llm_calls
                  (user_id, request_id, inbox_id, purpose, model,
                   prompt_tokens, completion_tokens, cost_usd, latency_ms,
                   status, error_message, metadata)
                VALUES
                  (:user_id, :request_id, :inbox_id, :purpose, :model,
                   :prompt_tokens, :completion_tokens, :cost_usd, :latency_ms,
                   :status, :error_message, CAST(:metadata AS JSONB))
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "request_id": request_id,
                "inbox_id": inbox_id,
                "purpose": purpose,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": str(cost_usd),
                "latency_ms": latency_ms,
                "status": status,
                "error_message": error_message[:_ERROR_MESSAGE_MAX] if error_message else None,
                "metadata": json.dumps(metadata or {}),
            },
        ).scalar_one()

    _log.info(
        "llm.call",
        purpose=purpose,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=str(cost_usd),
        latency_ms=latency_ms,
        status=status,
        inbox_id=inbox_id,
    )
    return int(row_id)
