from __future__ import annotations

import asyncio
import json
import queue
from typing import Any

import pytest
import structlog

from memex.core import log_sink
from memex.core.log_sink import _SinkState
from memex.logging import (
    bind_request_context,
    bound_log_context,
    clear_request_context,
    get_logger,
    setup_logging,
)


def test_setup_logging_is_idempotent() -> None:
    setup_logging()
    setup_logging()


def test_bind_request_context_sets_contextvars() -> None:
    clear_request_context()
    bind_request_context(request_id="req-abc", user_id=42)
    bound = structlog.contextvars.get_contextvars()
    assert bound.get("request_id") == "req-abc"
    assert bound.get("user_id") == 42
    clear_request_context()


def test_bind_request_context_only_request_id() -> None:
    clear_request_context()
    bind_request_context(request_id="req-no-user")
    bound = structlog.contextvars.get_contextvars()
    assert bound.get("request_id") == "req-no-user"
    assert "user_id" not in bound
    clear_request_context()


def test_bind_request_context_extra_kwargs() -> None:
    clear_request_context()
    bind_request_context(request_id="req-extra", run_id="run-1")
    bound = structlog.contextvars.get_contextvars()
    assert bound.get("request_id") == "req-extra"
    assert bound.get("run_id") == "run-1"
    clear_request_context()


def test_clear_request_context_removes_all_bindings() -> None:
    bind_request_context(request_id="req-xyz", user_id=99, run_id="r-1")
    clear_request_context()
    bound = structlog.contextvars.get_contextvars()
    assert "request_id" not in bound
    assert "user_id" not in bound
    assert "run_id" not in bound


def test_logger_created_before_setup_logging_reaches_sink() -> None:
    """REGRESIÓN (bug 2026-06-09): `_log = get_logger(...)` a nivel de módulo corre en el import,
    ANTES de `setup_logging()`. El `.bind()` eager materializaba el logger con la config DEFAULT de
    structlog (sin `persist_processor`) y `cache_logger_on_first_use` lo dejaba clavado ahí para
    siempre: sus eventos jamás llegaban a `log_events` (ni en los CLIs ni en el API). El nombre
    debe bindearse como initial value LAZY: el primer log ya corre con la config real y pasa por
    el sink."""
    # Simula el estado pre-configure del proceso (como en un import temprano).
    structlog.reset_defaults()
    log = get_logger("memex.test.pre_setup")  # ← equivale a un _log a nivel de módulo

    setup_logging()

    # Sink falso (cola en memoria, sin DB ni thread): solo importa que el processor encole.
    original_state = log_sink._state
    q: queue.Queue[dict[str, Any]] = queue.Queue()
    log_sink._state = _SinkState(enabled=True, min_level_no=20, queue=q)
    try:
        log.info("pre_setup.event", foo="bar")
    finally:
        log_sink._state = original_state

    assert q.qsize() == 1, "el evento de un logger pre-setup_logging no pasó por el sink"
    rec = q.get_nowait()
    assert rec["event"] == "pre_setup.event"
    assert rec["logger"] == "memex.test.pre_setup"
    assert json.loads(rec["fields"])["foo"] == "bar"


def test_get_logger_initial_values_pre_setup_reach_sink() -> None:
    """Los initial values extra de `get_logger(name, **iv)` (el reemplazo del `.bind()` eager de
    los ingestors) viajan LAZY igual que el nombre: un logger creado pre-`setup_logging()` emite
    con la config real y los campos llegan a `fields` del sink."""
    structlog.reset_defaults()
    log = get_logger("memex.test.iv", host="mail.example", port=993)

    setup_logging()

    original_state = log_sink._state
    q: queue.Queue[dict[str, Any]] = queue.Queue()
    log_sink._state = _SinkState(enabled=True, min_level_no=20, queue=q)
    try:
        log.info("iv.event")
    finally:
        log_sink._state = original_state

    rec = q.get_nowait()
    assert rec["logger"] == "memex.test.iv"
    fields = json.loads(rec["fields"])
    assert fields["host"] == "mail.example"
    assert fields["port"] == 993


@pytest.mark.parametrize(
    "key",
    [
        "_logger_name",
        "logger",
        "processors",
        "wrapper_class",
        "context_class",
        "cache_logger_on_first_use",
        "logger_factory_args",
    ],
)
def test_get_logger_reserved_initial_values_raise(key: str) -> None:
    """`structlog.get_logger(**kw)` interpreta estas claves como config del proxy: aceptarlas
    tragaría el valor en silencio (o pisaría la config). Deben reventar en el acto."""
    with pytest.raises(ValueError, match="reservados"):
        get_logger("memex.test.reserved", **{key: "x"})


def test_bound_log_context_binds_filters_none_and_restores() -> None:
    clear_request_context()
    with bound_log_context(run_id="7", user_id=1, inbox_id=None):
        bound = structlog.contextvars.get_contextvars()
        assert bound.get("run_id") == "7"
        assert bound.get("user_id") == 1
        assert "inbox_id" not in bound  # None filtrado: sin ramas en el caller
    bound = structlog.contextvars.get_contextvars()
    assert "run_id" not in bound
    assert "user_id" not in bound


def test_bound_log_context_restores_on_exception() -> None:
    clear_request_context()
    with pytest.raises(RuntimeError), bound_log_context(run_id="9"):
        raise RuntimeError("boom")
    assert "run_id" not in structlog.contextvars.get_contextvars()


def test_bound_log_context_propagates_to_gather_and_to_thread() -> None:
    """Blinda el supuesto de la correlación por lotes: las tasks de `asyncio.gather` y el thread
    de `asyncio.to_thread` COPIAN el contexto al crearse → heredan los campos bindeados (FASE 1
    del orquestador y las etapas media/classify de `reprocess` corren así)."""

    async def _in_task() -> Any:
        return structlog.contextvars.get_contextvars().get("run_id")

    def _in_thread() -> Any:
        return structlog.contextvars.get_contextvars().get("run_id")

    async def _main() -> tuple[Any, Any, Any]:
        with bound_log_context(run_id="42"):
            a, b = await asyncio.gather(_in_task(), _in_task())
            c = await asyncio.to_thread(_in_thread)
            return a, b, c

    clear_request_context()
    assert asyncio.run(_main()) == ("42", "42", "42")


def test_processor_pipeline_produces_valid_ndjson_with_contextvars() -> None:
    """Verifies the processor contract: merge_contextvars + add_log_level +
    TimeStamper + JSONRenderer produce a parseable line with the expected
    fields, including those bound via `bind_request_context`.

    Uses an ad-hoc logger writing to an in-memory buffer because pytest's
    capsys/capfd cannot reliably capture structlog's PrintLogger output
    once `setup_logging()` has been called by an earlier test.
    """
    import io

    buf = io.StringIO()
    logger = structlog.wrap_logger(
        structlog.PrintLogger(file=buf),
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
    )

    clear_request_context()
    bind_request_context(request_id="req-buf", user_id=11)
    try:
        logger.info("test.event", foo="bar")
    finally:
        clear_request_context()

    output = buf.getvalue().strip()
    assert output, "expected output from ad-hoc logger"
    payload = json.loads(output)
    assert payload["event"] == "test.event"
    assert payload["foo"] == "bar"
    assert payload["request_id"] == "req-buf"
    assert payload["user_id"] == 11
    assert payload["level"] == "info"
    assert "timestamp" in payload
