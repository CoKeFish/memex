import logging
import sys
from collections.abc import MutableMapping
from typing import Any, cast

import structlog

from memex.config import settings
from memex.core.log_sink import install_log_sink, persist_processor

#: Clave interna con la que `get_logger` deja el nombre en los initial values del lazy proxy.
#: No puede llamarse "logger" directo: `structlog.get_logger(**initial_values)` los reenvía a
#: `wrap_logger(logger, ...)` y colisiona con su primer parámetro (TypeError).
_LOGGER_NAME_KEY = "_logger_name"


def _promote_logger_name(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Promueve el initial value `_logger_name` → campo `logger` del event_dict.

    El campo `logger` es ADITIVO y lo leen el sink (columna/filtro de `log_events` + chequeo
    anti-recursión) y la línea de stderr; por eso este processor corre temprano, antes de
    `persist_processor`."""
    name = event_dict.pop(_LOGGER_NAME_KEY, None)
    if name is not None:
        event_dict.setdefault("logger", name)
    return event_dict


def setup_logging() -> None:
    """Configure structlog. Idempotent — safe to call multiple times."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _promote_logger_name,
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
    if name is None:
        return cast("structlog.stdlib.BoundLogger", structlog.get_logger())
    # El nombre viaja como INITIAL VALUE del lazy proxy — NUNCA `.bind()` acá: `.bind()` sobre el
    # proxy materializa el logger EN EL ACTO con la config vigente, y un `_log = get_logger(...)`
    # a nivel de módulo corre en el import, ANTES de `setup_logging()` → quedaba clavado para
    # siempre (cache_logger_on_first_use) en la config default de structlog, sin
    # `persist_processor`: sus eventos jamás llegaban a `log_events`. El initial value se aplica
    # recién en el primer log, ya con la config real (`_promote_logger_name` lo vuelca al campo
    # `logger` del event_dict).
    return cast(
        "structlog.stdlib.BoundLogger", structlog.get_logger(name, **{_LOGGER_NAME_KEY: name})
    )


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
