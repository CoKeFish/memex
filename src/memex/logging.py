import logging
import sys
from typing import Any, cast

import structlog

from memex.config import settings
from memex.core.log_sink import install_log_sink, persist_processor


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
            # El sink corre JUSTO ANTES del JSONRenderer: acá el event_dict todavía es un dict
            # (el renderer lo convierte en string). Solo encola; no muta el evento.
            persist_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
    # Idempotente: arranca el escritor por lotes la primera vez, no-op en llamadas siguientes.
    install_log_sink()


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    bound = structlog.get_logger(name)
    if name is not None:
        # Bindeamos `logger=name` como campo ADITIVO: así el nombre del logger aparece en el
        # event_dict tanto para el sink (columna/filtro `logger`) como para la línea de stderr.
        bound = bound.bind(logger=name)
    return cast("structlog.stdlib.BoundLogger", bound)


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
