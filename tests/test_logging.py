from __future__ import annotations

import json
import queue
from typing import Any

import structlog

from memex.core import log_sink
from memex.core.log_sink import _SinkState
from memex.logging import (
    bind_request_context,
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
