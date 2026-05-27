from __future__ import annotations

import json

import structlog

from memex.logging import (
    bind_request_context,
    clear_request_context,
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
