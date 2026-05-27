import logging
import sys
from typing import Any, cast

import structlog

from memex.config import settings


def setup_logging() -> None:
    """Configure structlog. Idempotent — safe to call multiple times."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))


def bind_request_context(
    *,
    request_id: str | None = None,
    user_id: int | None = None,
    **extra: Any,
) -> None:
    """Bind per-request fields to the structlog contextvars.

    Single entry point — middleware, auth dep, and ingestor runs call this
    instead of touching structlog.contextvars directly. Future-proofs hooks
    like redaction or whitelisting in one place.
    """
    fields: dict[str, Any] = dict(extra)
    if request_id is not None:
        fields["request_id"] = request_id
    if user_id is not None:
        fields["user_id"] = user_id
    if fields:
        structlog.contextvars.bind_contextvars(**fields)


def clear_request_context() -> None:
    """Clear all contextvars bound for the current request/run."""
    structlog.contextvars.clear_contextvars()
