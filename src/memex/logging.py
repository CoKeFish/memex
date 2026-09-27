import logging
import sys
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
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


#: Claves vetadas en los initial values de `get_logger`: `structlog.get_logger(**kw)` reenvía los
#: kwargs a `wrap_logger(logger, ...)`, donde estos nombres son CONFIG del proxy (no contexto) —
#: se tragarían el valor en silencio. `logger` colisiona con su primer parámetro posicional y
#: `_logger_name` está reservada para el nombre (ver `_LOGGER_NAME_KEY`).
_RESERVED_INITIAL_KEYS = frozenset(
    {
        _LOGGER_NAME_KEY,
        "logger",
        "processors",
        "wrapper_class",
        "context_class",
        "cache_logger_on_first_use",
        "logger_factory_args",
    }
)


def get_logger(name: str | None = None, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Logger del proyecto. El contexto fijo va en `initial_values` (lazy), NUNCA en `.bind()`."""
    reserved = _RESERVED_INITIAL_KEYS & initial_values.keys()
    if reserved:
        raise ValueError(
            f"initial values reservados por structlog/get_logger: {sorted(reserved)}; "
            "renombrá el campo o pasalo como kwarg en cada evento"
        )
    if name is None:
        return cast("structlog.stdlib.BoundLogger", structlog.get_logger(**initial_values))
    # El nombre (y los initial values extra) viajan como INITIAL VALUES del lazy proxy — NUNCA
    # `.bind()` acá: `.bind()` sobre el proxy materializa el logger EN EL ACTO con la config
    # vigente, y un `_log = get_logger(...)` a nivel de módulo corre en el import, ANTES de
    # `setup_logging()` → quedaba clavado para siempre (cache_logger_on_first_use) en la config
    # default de structlog, sin `persist_processor`: sus eventos jamás llegaban a `log_events`.
    # Los initial values se aplican recién en el primer log, ya con la config real
    # (`_promote_logger_name` vuelca el nombre al campo `logger` del event_dict).
    return cast(
        "structlog.stdlib.BoundLogger",
        structlog.get_logger(name, **{_LOGGER_NAME_KEY: name}, **initial_values),
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


@contextmanager
def bound_log_context(**fields: Any) -> Iterator[None]:
    """Bindea campos a los contextvars de structlog por el scope del `with` (corridas por lote,
    unidad de extracción). Todo lo que corra adentro — incluidas tasks de `asyncio.gather` y
    `asyncio.to_thread`, que COPIAN el contexto al crearse — hereda los campos en sus eventos.

    Filtra los `None` (permite `inbox_id=ids[0] if n == 1 else None` sin ramas en el caller) y
    restaura SIEMPRE al salir (tokens de `bound_contextvars`), incluso con excepción. Misma puerta
    única que `bind_request_context` para hooks futuros (redacción/whitelist)."""
    bound = {k: v for k, v in fields.items() if v is not None}
    if not bound:
        yield
        return
    with structlog.contextvars.bound_contextvars(**bound):
        yield
