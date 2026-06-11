"""Log sink: persiste cada evento de structlog (sobre un umbral de nivel) a `log_events`.

Es un PROCESSOR de structlog (`persist_processor`) que corre en el hilo del caller pero NO
bloquea: solo encola una copia del evento en una `queue.Queue` acotada y devuelve el `event_dict`
intacto. Un daemon thread (`_BatchWriter`) drena la cola y escribe por LOTES a la DB. Así el path
de logging del request nunca espera a Postgres.

Por qué NO bloquea
------------------
`persist_processor` es O(1): `put_nowait` sobre una cola en memoria y vuelve. Si la cola está
llena se descarta el evento y se CUENTA (`_state.dropped`), nunca se espera. La escritura a la DB
(latencia variable) vive entera en el thread escritor, fuera del request.

Las 3 invariantes anti-recursión (en `persist_processor`)
---------------------------------------------------------
1. Se ignora cualquier evento del propio sink (`logger == _SINK_LOGGER_NAME`) para que un log del
   escritor no se re-encole a sí mismo en un bucle.
2. Solo se encola si el sink está habilitado y el nivel supera el umbral; debajo del umbral o
   inerte → passthrough puro.
3. TODO el cuerpo va en try/except: cualquier fallo (cola llena u otra cosa) incrementa
   `_state.dropped` y NUNCA relanza, para que un problema del sink jamás tumbe el logging real.

Canal de fallo sancionado
--------------------------
La ÚNICA escritura cruda a stderr permitida en código de app (ADR-007 prohíbe print/stdlib) vive
en `_BatchWriter._flush`: si el INSERT del lote falla NO se puede loguear vía structlog (sería
recursión hacia este mismo sink), así que se emite una línea JSON a `sys.stderr` y se DESCARTA el
lote (no se re-encola: evitar retry infinito y una cola que nunca drena). Es deliberado y está
documentado acá.

Espacios de `run_id` (columna TEXT; cada espacio de corridas numera las suyas, el prefijo evita
que se mezclen en `/logs?run_id=`):
  - número pelado ("12")  → procesamiento (`worker_runs`)
  - uuid                  → corridas de ingesta (`ingestion_runs`)
  - "cli-<hex>"           → CLI memex-reprocess (sin fila propia)
  - "cal:<id>"            → sync de calendario (`mod_calendar_sync_runs`, ingress y egress)
  - "idsync:<id>"         → sync de contactos (`mod_identidades_sync_runs`)

Este módulo NO importa `memex.logging` (es su consumidor): la dependencia es
`logging.py -> log_sink`, nunca al revés, para evitar el import circular.
"""

from __future__ import annotations

import atexit
import json
import queue
import sys
import threading
import traceback
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from memex.db import connection

#: Nombre del logger del propio sink: los eventos con este logger se ignoran (anti-recursión).
_SINK_LOGGER_NAME = "memex.core.log_sink"

#: Mapa nivel→número (mismo orden que stdlib logging) para comparar contra el umbral.
_LEVEL_NO = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}

#: INSERT por lote. `fields` viaja ya serializado como string JSON y se castea a JSONB en SQL.
INSERT_SQL = (
    "INSERT INTO log_events "
    "(ts, level, event, logger, user_id, request_id, run_id, source_id, inbox_id, "
    "exception, fields) "
    "VALUES (:ts, :level, :event, :logger, :user_id, :request_id, :run_id, :source_id, "
    ":inbox_id, :exception, CAST(:fields AS JSONB))"
)


@dataclass
class _SinkState:
    """Estado mutable del módulo. El processor debe ser seguro incluso antes de `install`."""

    enabled: bool = False
    min_level_no: int = 20
    queue: queue.Queue[dict[str, Any]] | None = None
    writer: _BatchWriter | None = None
    dropped: int = 0
    installed: bool = False


_state = _SinkState()


def _coerce_str(value: Any) -> str | None:
    """Coacciona a str (columna `logger`, siempre str vía `_promote_logger_name`); otro → None."""
    return value if isinstance(value, str) else None


def _pop_text(ed: MutableMapping[str, Any], key: str) -> str | None:
    """Extrae `key` hacia una columna TEXT de correlación (`request_id`, `run_id`).

    Invariante: ningún id de correlación se pierde en silencio. str → columna; int (no bool) →
    `str()` a columna (los run_id de `worker_runs` son ints y el viejo `_coerce_str` los tiraba:
    todos los logs de procesamiento quedaban sin run_id); None → se consume; cualquier otro tipo
    NO se consume — queda en `fields` JSONB, visible y buscable en vez de desaparecer."""
    value = ed.get(key)
    if value is None:
        ed.pop(key, None)
        return None
    if isinstance(value, str):
        ed.pop(key)
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        ed.pop(key)
        return str(value)
    return None


def _pop_bigint(ed: MutableMapping[str, Any], key: str) -> int | None:
    """Extrae `key` hacia una columna BIGINT de correlación (`user_id`, `source_id`, `inbox_id`).

    Misma invariante que `_pop_text`: int (no bool) → columna; None → se consume; cualquier otro
    tipo NO se consume — queda en `fields`. No se coacciona str→int a propósito (cero ambigüedad;
    el valor igual queda visible en `fields`)."""
    value = ed.get(key)
    if value is None:
        ed.pop(key, None)
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        ed.pop(key)
        return value
    return None


def _format_exception(exc_info: Any, stack: Any) -> str | None:
    """Renderiza `exc_info` (excepción, tupla (type,exc,tb) o True) + `stack` a un string."""
    parts: list[str] = []
    # exc_info puede venir como: una instancia de excepción, una tupla (type, exc, tb), o True.
    # `True` significa "tomá la excepción en curso" (sys.exc_info()); si no hay, no aporta nada.
    if exc_info is not None and exc_info is not False:
        if isinstance(exc_info, BaseException):
            tb_lines = traceback.format_exception(type(exc_info), exc_info, exc_info.__traceback__)
            parts.append("".join(tb_lines))
        elif isinstance(exc_info, tuple) and len(exc_info) == 3:
            parts.append("".join(traceback.format_exception(*exc_info)))
        elif exc_info is True:
            etype, evalue, etb = sys.exc_info()
            if evalue is not None:
                parts.append("".join(traceback.format_exception(etype, evalue, etb)))
    if isinstance(stack, str) and stack:
        parts.append(stack)
    joined = "\n".join(p for p in parts if p)
    return joined or None


def _to_record(event_dict: MutableMapping[str, Any]) -> dict[str, Any]:
    """Convierte un `event_dict` de structlog en una fila lista para `INSERT_SQL`.

    Copia el dict (NO muta el original que sigue su camino hacia stderr), extrae las columnas
    de primer nivel y serializa el RESTO de kwargs a `fields` (JSON string, `default=str` para que
    Decimal/datetime nunca revienten). `exception` queda como traceback formateado si lo hubo.
    """
    ed = dict(event_dict)  # copia defensiva: no mutamos el dict del pipeline

    # ts: structlog ya puso un timestamp ISO (TimeStamper); fallback a ahora si falta/parsea mal.
    ts_raw = ed.pop("timestamp", None)
    ts: datetime
    if isinstance(ts_raw, str):
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            ts = datetime.now(UTC)
    elif isinstance(ts_raw, datetime):
        ts = ts_raw
    else:
        ts = datetime.now(UTC)

    level = ed.pop("level", None)
    level_str = level if isinstance(level, str) else "info"

    event = ed.pop("event", None)
    event_str = event if isinstance(event, str) else ""

    logger = _coerce_str(ed.pop("logger", None))

    # Correlación pop-condicional: lo coercible va a su columna; lo no coercible QUEDA en fields.
    user_id = _pop_bigint(ed, "user_id")
    request_id = _pop_text(ed, "request_id")
    run_id = _pop_text(ed, "run_id")
    source_id = _pop_bigint(ed, "source_id")
    inbox_id = _pop_bigint(ed, "inbox_id")

    # exc_info / stack se consumen acá (no van a `fields`): se vuelcan a la columna `exception`.
    exc_info = ed.pop("exc_info", None)
    stack = ed.pop("stack", None)
    exception = _format_exception(exc_info, stack)

    # El resto de kwargs estructurados → fields. default=str absorbe Decimal/datetime/UUID/etc.
    fields_json = json.dumps(ed, default=str)

    return {
        "ts": ts,
        "level": level_str,
        "event": event_str,
        "logger": logger,
        "user_id": user_id,
        "request_id": request_id,
        "run_id": run_id,
        "source_id": source_id,
        "inbox_id": inbox_id,
        "exception": exception,
        "fields": fields_json,
    }


def persist_processor(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Processor de structlog: encola el evento (no bloqueante) y devuelve el `event_dict` intacto.

    Va JUSTO ANTES del JSONRenderer (que devuelve un string): acá `event_dict` todavía es un dict.
    Corre en el hilo del caller, O(1). Ver las 3 invariantes anti-recursión en el docstring del
    módulo. Devuelve SIEMPRE el `event_dict` original (este processor es un side-effect puro).
    """
    # Invariante 1: ignorar los eventos del propio sink (no re-encolar en bucle).
    if (
        event_dict.get("logger") == _SINK_LOGGER_NAME
        or getattr(logger, "name", None) == _SINK_LOGGER_NAME
    ):
        return event_dict

    # Invariante 2: solo encolar si está habilitado y el nivel supera el umbral.
    level = event_dict.get("level")
    level_no = _LEVEL_NO.get(level, 0) if isinstance(level, str) else 0
    if not _state.enabled or level_no < _state.min_level_no:
        return event_dict

    # Invariante 3: cualquier fallo cuenta como dropped y NUNCA relanza.
    try:
        q = _state.queue
        if q is None:
            return event_dict
        q.put_nowait(_to_record(event_dict))
    except queue.Full:
        _state.dropped += 1
    except Exception:
        _state.dropped += 1
    return event_dict


class _BatchWriter(threading.Thread):
    """Daemon thread que drena la cola y escribe `log_events` por LOTES.

    Junta hasta `batch_size` items (o lo que haya al vencer `flush_interval_s`) y los inserta en
    una sola llamada (executemany vía lista de dicts). Al pedírsele stop, drena lo que quede y hace
    un flush final. Nunca usa structlog (sería recursión hacia el sink); su único canal de fallo es
    una línea JSON a stderr en `_flush`.
    """

    def __init__(self, q: queue.Queue[dict[str, Any]], batch_size: int, flush_interval_s: float):
        super().__init__(name="memex-log-sink", daemon=True)
        self._q = q
        self._batch_size = max(1, batch_size)
        self._flush_interval_s = flush_interval_s
        # OJO: NO usar el nombre `_stop` — pisa el método interno `Thread._stop()` que `join()`
        # invoca en ciertos timings de `join()` (→ TypeError). De ahí el sufijo `_event`.
        self._stop_event = threading.Event()
        self.db_errors: int = 0

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            batch = self._collect_batch()
            if batch:
                self._flush(batch)
        # Stop pedido: drenar lo que quede en la cola y flush final (no perder lo encolado).
        self._drain_and_flush()

    def _collect_batch(self) -> list[dict[str, Any]]:
        """Bloquea hasta `flush_interval_s` por el primer item; luego llena sin bloquear."""
        batch: list[dict[str, Any]] = []
        try:
            first = self._q.get(timeout=self._flush_interval_s)
        except queue.Empty:
            return batch
        batch.append(first)
        while len(batch) < self._batch_size:
            try:
                batch.append(self._q.get_nowait())
            except queue.Empty:
                break
        return batch

    def _drain_and_flush(self) -> None:
        """Vacía la cola en lotes y hace el flush final tras el stop."""
        while True:
            batch: list[dict[str, Any]] = []
            while len(batch) < self._batch_size:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            if not batch:
                break
            self._flush(batch)

    def _flush(self, batch: list[dict[str, Any]]) -> None:
        """Inserta el lote. Si falla: cuenta el error, escribe a stderr y DESCARTA el lote.

        El batch se DROPea (no se re-encola) a propósito: re-encolar un lote que falla por un
        problema de DB llevaría a retry infinito y a una cola que nunca drena.
        """
        if not batch:
            return
        try:
            with connection() as conn:
                conn.execute(text(INSERT_SQL), batch)
        except Exception as e:
            self.db_errors += 1
            # Único canal de fallo sancionado (ver docstring del módulo): structlog acá sería
            # recursión. Emitimos una línea JSON cruda a stderr y descartamos el lote.
            sys.stderr.write(
                json.dumps(
                    {
                        "event": "log_sink.flush_failed",
                        "level": "error",
                        "dropped_batch": len(batch),
                        "db_errors": self.db_errors,
                        "error": str(e),
                    },
                    default=str,
                )
                + "\n"
            )


def install_log_sink() -> None:
    """Arranca el sink (idempotente). Lee la config; si `log_persist=False` queda inerte.

    Llamado al final de `setup_logging()`. Tras instalar, `persist_processor` empieza a encolar.
    """
    if _state.installed:
        return
    from memex.config import settings

    if not settings.log_persist:
        _state.enabled = False
        _state.installed = True
        return

    _state.min_level_no = _LEVEL_NO.get(settings.log_persist_level.lower(), 20)
    q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=settings.log_persist_queue_max)
    writer = _BatchWriter(
        q,
        batch_size=settings.log_persist_batch_size,
        flush_interval_s=settings.log_persist_flush_ms / 1000,
    )
    writer.start()
    _state.queue = q
    _state.writer = writer
    atexit.register(shutdown_sink)
    _state.enabled = True
    _state.installed = True


def shutdown_sink() -> None:
    """Pide stop al escritor y espera el flush final (idempotente). Deja el sink usable."""
    writer = _state.writer
    if writer is not None:
        writer.stop()
        writer.join(timeout=5.0)


def sink_health() -> dict[str, int]:
    """Salud del sink: eventos descartados, errores de DB y tamaño actual de la cola."""
    writer = _state.writer
    q = _state.queue
    return {
        "dropped": _state.dropped,
        "db_errors": writer.db_errors if writer is not None else 0,
        "queue_size": q.qsize() if q is not None else 0,
    }
