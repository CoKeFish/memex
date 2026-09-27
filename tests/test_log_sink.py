"""Tests UNITARIOS del log sink (`memex.core.log_sink`): sin DB ni threads reales.

Se manipulan las funciones puras directamente y se fuerza `_state` a mano en cada test (no se
depende de `settings`): así el comportamiento es determinista y no contamina al resto de la suite.
Una fixture autouse resetea `_state` antes y después de cada test.
"""

from __future__ import annotations

import json
import queue
from collections.abc import Iterator
from typing import Any

import pytest

from memex.core import log_sink
from memex.core.log_sink import (
    _SINK_LOGGER_NAME,
    INSERT_SQL,
    _BatchWriter,
    _SinkState,
    _to_record,
    persist_processor,
)


@pytest.fixture(autouse=True)
def _reset_sink_state() -> Iterator[None]:
    """Restaura `_state` a un `_SinkState` limpio alrededor de cada test (evita contaminación)."""
    original = log_sink._state
    log_sink._state = _SinkState()
    try:
        yield
    finally:
        log_sink._state = original


# ---- _to_record: extracción de columnas + fields + exception -----------------------------------


def test_to_record_extracts_columns_and_fields() -> None:
    try:
        raise ValueError("boom unitario")
    except ValueError as exc:
        captured = exc

    event_dict: dict[str, Any] = {
        "timestamp": "2026-06-02T12:00:00+00:00",
        "level": "error",
        "event": "worker.failed",
        "logger": "memex.worker",
        "request_id": "req-42",
        "user_id": 7,
        "run_id": "run-9",
        "source_id": 3,
        "inbox_id": 11,
        "extra_field": "valor-extra",
        "exc_info": captured,
    }
    before = dict(event_dict)

    rec = _to_record(event_dict)

    # Columnas de primer nivel bien extraídas.
    assert rec["level"] == "error"
    assert rec["event"] == "worker.failed"
    assert rec["logger"] == "memex.worker"
    assert rec["request_id"] == "req-42"
    assert rec["user_id"] == 7
    assert rec["run_id"] == "run-9"
    assert rec["source_id"] == 3
    assert rec["inbox_id"] == 11
    # ts parseado del ISO a datetime aware.
    assert rec["ts"].year == 2026
    assert rec["ts"].tzinfo is not None
    # exception lleva el traceback formateado de la excepción real.
    assert rec["exception"] is not None
    assert "ValueError" in rec["exception"]
    assert "boom unitario" in rec["exception"]
    assert "Traceback" in rec["exception"]
    # fields es un JSON string que conserva el campo extra y NO arrastra las columnas extraídas.
    fields = json.loads(rec["fields"])
    assert fields["extra_field"] == "valor-extra"
    assert "level" not in fields
    assert "event" not in fields
    assert "exc_info" not in fields
    assert "user_id" not in fields
    # NO muta el event_dict original (copia defensiva).
    assert event_dict == before


def test_to_record_coerces_bad_correlation_and_defaults() -> None:
    # Tipos malos en las columnas de correlación → columna None pero el valor QUEDA en fields
    # (invariante: nada se pierde en silencio); bool NO cuenta como int; int en columna TEXT → str.
    rec = _to_record(
        {
            "level": 123,  # no-str → fallback "info"
            "event": None,  # no-str → ""
            "user_id": True,  # bool → columna None (no es un int válido de correlación)
            "source_id": "x",  # str en columna BIGINT → columna None
            "request_id": 99,  # int en columna TEXT → "99" (un id numérico no se tira)
        }
    )
    assert rec["level"] == "info"
    assert rec["event"] == ""
    assert rec["user_id"] is None
    assert rec["source_id"] is None
    assert rec["request_id"] == "99"
    assert rec["exception"] is None
    fields = json.loads(rec["fields"])
    assert fields["user_id"] is True  # no coercible → conservado en fields
    assert fields["source_id"] == "x"
    assert "request_id" not in fields  # coercionado → consumido hacia la columna


def test_to_record_run_id_int_goes_to_column_as_str() -> None:
    """REGRESIÓN: los run_id int (`worker_runs` de los lotes de procesamiento) se tragaban
    enteros — ni columna ni fields; `/logs?run_id=` no encontraba ninguna corrida de
    procesamiento. Ahora van a la columna TEXT como str."""
    rec = _to_record({"level": "info", "event": "e", "run_id": 123})
    assert rec["run_id"] == "123"
    assert "run_id" not in json.loads(rec["fields"])


def test_to_record_uncoercible_correlation_stays_in_fields() -> None:
    rec = _to_record({"level": "info", "event": "e", "run_id": {"id": 1}, "inbox_id": "11"})
    assert rec["run_id"] is None
    assert rec["inbox_id"] is None
    fields = json.loads(rec["fields"])
    assert fields["run_id"] == {"id": 1}
    assert fields["inbox_id"] == "11"


def test_to_record_none_correlation_is_consumed() -> None:
    # None explícito (bindeado río arriba) se consume: ni columna ni ruido "null" en fields.
    rec = _to_record({"level": "info", "event": "e", "run_id": None, "inbox_id": None})
    assert rec["run_id"] is None
    assert rec["inbox_id"] is None
    fields = json.loads(rec["fields"])
    assert "run_id" not in fields
    assert "inbox_id" not in fields


def test_to_record_non_serializable_field_does_not_blow_up() -> None:
    # default=str absorbe objetos no serializables (acá uno custom sin __json__).
    class Weird:
        def __str__(self) -> str:
            return "weird-repr"

    rec = _to_record({"level": "info", "event": "e", "obj": Weird()})
    fields = json.loads(rec["fields"])
    assert fields["obj"] == "weird-repr"


# ---- persist_processor: encolado, umbral, overflow, anti-recursión -----------------------------


def test_persist_processor_enqueues_above_threshold_and_returns_same_dict() -> None:
    q: queue.Queue[dict[str, Any]] = queue.Queue()
    log_sink._state.enabled = True
    log_sink._state.min_level_no = 20  # info
    log_sink._state.queue = q

    event_dict = {"level": "info", "event": "ok.event", "foo": "bar"}
    out = persist_processor(None, "info", event_dict)

    # Devuelve EXACTAMENTE el mismo objeto (side-effect puro).
    assert out is event_dict
    assert q.qsize() == 1
    enqueued = q.get_nowait()
    assert enqueued["event"] == "ok.event"
    assert json.loads(enqueued["fields"])["foo"] == "bar"


def test_persist_processor_enqueues_run_id_int_as_text() -> None:
    """End-to-end del fix: un evento con run_id int (como bindean los lotes) llega a la cola
    con la columna run_id poblada como str."""
    q: queue.Queue[dict[str, Any]] = queue.Queue()
    log_sink._state.enabled = True
    log_sink._state.min_level_no = 20
    log_sink._state.queue = q

    persist_processor(None, "info", {"level": "info", "event": "lote.done", "run_id": 7})
    rec = q.get_nowait()
    assert rec["run_id"] == "7"
    assert "run_id" not in json.loads(rec["fields"])


def test_persist_processor_skips_below_threshold() -> None:
    q: queue.Queue[dict[str, Any]] = queue.Queue()
    log_sink._state.enabled = True
    log_sink._state.min_level_no = 30  # warning
    log_sink._state.queue = q

    out = persist_processor(None, "info", {"level": "info", "event": "debajo.umbral"})
    assert out["event"] == "debajo.umbral"
    assert q.qsize() == 0


def test_persist_processor_skips_when_disabled() -> None:
    q: queue.Queue[dict[str, Any]] = queue.Queue()
    log_sink._state.enabled = False
    log_sink._state.min_level_no = 20
    log_sink._state.queue = q

    persist_processor(None, "error", {"level": "error", "event": "inerte"})
    assert q.qsize() == 0


def test_persist_processor_full_queue_increments_dropped_and_does_not_raise() -> None:
    q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
    log_sink._state.enabled = True
    log_sink._state.min_level_no = 20
    log_sink._state.queue = q

    persist_processor(None, "info", {"level": "info", "event": "primero"})
    assert q.qsize() == 1
    assert log_sink._state.dropped == 0

    # Segundo evento: cola llena → cuenta dropped, NO relanza, devuelve el dict.
    out = persist_processor(None, "info", {"level": "info", "event": "segundo"})
    assert out["event"] == "segundo"
    assert log_sink._state.dropped == 1
    assert q.qsize() == 1  # el segundo no entró


def test_persist_processor_ignores_own_sink_logger() -> None:
    # Anti-recursión: un evento cuyo logger es el del propio sink NO se encola.
    q: queue.Queue[dict[str, Any]] = queue.Queue()
    log_sink._state.enabled = True
    log_sink._state.min_level_no = 20
    log_sink._state.queue = q

    out = persist_processor(
        None, "error", {"level": "error", "event": "self.event", "logger": _SINK_LOGGER_NAME}
    )
    assert out["event"] == "self.event"
    assert q.qsize() == 0


# ---- _BatchWriter._flush: canal de fallo sancionado (stderr), sin propagar ----------------------


def test_batchwriter_flush_failure_writes_stderr_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom_connection() -> Any:
        raise RuntimeError("db caída")

    # Parcheamos `connection` en el módulo para que el INSERT del lote reviente.
    monkeypatch.setattr(log_sink, "connection", _boom_connection)

    q: queue.Queue[dict[str, Any]] = queue.Queue()
    writer = _BatchWriter(q, batch_size=10, flush_interval_s=0.01)
    batch = [_to_record({"level": "info", "event": "x"})]

    # No debe propagar (anti-recursión: el escritor no puede morir por un fallo de DB).
    writer._flush(batch)

    assert writer.db_errors == 1
    # Una línea JSON cruda a stderr con el evento del canal de fallo sancionado.
    err = capsys.readouterr().err
    assert "log_sink.flush_failed" in err
    payload = json.loads(err.strip())
    assert payload["event"] == "log_sink.flush_failed"
    assert payload["level"] == "error"
    assert payload["dropped_batch"] == 1
    assert payload["db_errors"] == 1
    assert "db caída" in payload["error"]


def test_batchwriter_flush_empty_batch_is_noop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom_connection() -> Any:
        raise RuntimeError("no debería llamarse")

    monkeypatch.setattr(log_sink, "connection", _boom_connection)
    writer = _BatchWriter(queue.Queue(), batch_size=10, flush_interval_s=0.01)
    writer._flush([])  # lote vacío → ni toca la DB ni escribe a stderr
    assert writer.db_errors == 0
    assert capsys.readouterr().err == ""


def test_insert_sql_has_all_columns() -> None:
    # Smoke del contrato del INSERT: todos los binds que produce _to_record aparecen en el SQL.
    rec = _to_record({"level": "info", "event": "e"})
    for key in rec:
        assert f":{key}" in INSERT_SQL


# ---- install_log_sink: idempotencia -------------------------------------------------------------


def test_install_log_sink_idempotent_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Con log_persist=False queda inerte pero marcado como instalado; segunda llamada = no-op.
    from memex.config import settings

    monkeypatch.setattr(settings, "log_persist", False)
    log_sink.install_log_sink()
    assert log_sink._state.installed is True
    assert log_sink._state.enabled is False
    assert log_sink._state.writer is None
    # Segunda llamada no cambia nada.
    log_sink.install_log_sink()
    assert log_sink._state.writer is None


def test_install_log_sink_idempotent_does_not_start_two_writers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Con log_persist=True arranca UN escritor; una segunda llamada no arranca otro.
    from memex.config import settings

    monkeypatch.setattr(settings, "log_persist", True)
    monkeypatch.setattr(settings, "log_persist_level", "INFO")
    monkeypatch.setattr(settings, "log_persist_batch_size", 10)
    monkeypatch.setattr(settings, "log_persist_flush_ms", 50)
    monkeypatch.setattr(settings, "log_persist_queue_max", 100)

    try:
        log_sink.install_log_sink()
        assert log_sink._state.installed is True
        assert log_sink._state.enabled is True
        first_writer = log_sink._state.writer
        assert first_writer is not None
        assert first_writer.is_alive()

        # Segunda llamada: mismo writer, no arranca otro.
        log_sink.install_log_sink()
        assert log_sink._state.writer is first_writer
    finally:
        # Paramos el daemon thread real que arrancó install (la fixture solo restaura _state).
        if log_sink._state.writer is not None:
            log_sink._state.writer.stop()
            log_sink._state.writer.join(timeout=5.0)
