"""Tests de los endpoints /logs y /logs/stats (mirror de test_api_metrics.py).

Misma fixture `client` (TestClient sin auth) y misma estrategia de seeding por conexión propia.
`log_events` NO está en el TRUNCATE del conftest (`_reset_tables`), así que una fixture
autouse local la vacía antes de cada test para empezar de cero.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from memex.core import log_sink
from memex.db import connection


@pytest.fixture(autouse=True)
def _clean_log_events() -> Iterator[None]:
    """Aísla cada test del sink: lo deja inerte y vacía `log_events`.

    `log_persist` por default es True, así que al importar la app el sink real arranca su escritor
    por lotes; sus propios eventos (`logs.query`/`logs.stats`) caerían en `log_events` de forma
    asíncrona y contaminarían los conteos. Forzamos `enabled=False` (passthrough puro) durante el
    test y truncamos la tabla — que NO cubre el TRUNCATE del conftest — antes de sembrar.
    """
    prev_enabled = log_sink._state.enabled
    log_sink._state.enabled = False
    with connection() as c:
        c.execute(text("TRUNCATE TABLE log_events RESTART IDENTITY"))
    try:
        yield
    finally:
        log_sink._state.enabled = prev_enabled


def _seed_log_event(
    *,
    level: str = "info",
    event: str = "test.event",
    logger: str | None = "memex.test",
    user_id: int | None = 1,
    request_id: str | None = None,
    run_id: str | None = None,
    source_id: int | None = None,
    inbox_id: int | None = None,
    exception: str | None = None,
    fields: str = "{}",
    ts: datetime | None = None,
) -> int:
    """Inserta una fila en `log_events` (conexión propia para que el TestClient la vea)."""
    with connection() as c:
        return int(
            c.execute(
                text(
                    """
                    INSERT INTO log_events
                      (ts, level, event, logger, user_id, request_id, run_id, source_id,
                       inbox_id, exception, fields)
                    VALUES
                      (COALESCE(:ts, NOW()), :level, :event, :logger, :uid, :rid, :run, :src,
                       :inbox, :exc, CAST(:fields AS JSONB))
                    RETURNING id
                    """
                ),
                {
                    "ts": ts,
                    "level": level,
                    "event": event,
                    "logger": logger,
                    "uid": user_id,
                    "rid": request_id,
                    "run": run_id,
                    "src": source_id,
                    "inbox": inbox_id,
                    "exc": exception,
                    "fields": fields,
                },
            ).scalar_one()
        )


# ---- GET /logs: filtros multi-valor (level/event/logger) ---------------------------------------


def test_logs_filter_level_include_and_exclude(client: Any) -> None:
    _seed_log_event(level="info", event="a")
    _seed_log_event(level="error", event="b")
    _seed_log_event(level="warning", event="c")

    only_err = client.get("/logs?level=error").json()
    assert only_err["total"] == 1
    assert only_err["items"][0]["level"] == "error"

    # excluir error → 2 (info + warning)
    excl = client.get("/logs?level=error&level_mode=exclude").json()
    assert excl["total"] == 2
    assert all(i["level"] != "error" for i in excl["items"])

    # multi-valor: info + warning
    multi = client.get("/logs?level=info&level=warning").json()
    assert multi["total"] == 2


def test_logs_filter_event(client: Any) -> None:
    _seed_log_event(event="http.request")
    _seed_log_event(event="http.request")
    _seed_log_event(event="worker.failed")
    r = client.get("/logs?event=http.request").json()
    assert r["total"] == 2
    assert all(i["event"] == "http.request" for i in r["items"])


def test_logs_filter_logger(client: Any) -> None:
    _seed_log_event(logger="memex.api")
    _seed_log_event(logger="memex.worker")
    _seed_log_event(logger="memex.worker")
    r = client.get("/logs?logger=memex.worker").json()
    assert r["total"] == 2
    # excluir un logger
    excl = client.get("/logs?logger=memex.worker&logger_mode=exclude").json()
    assert excl["total"] == 1
    assert excl["items"][0]["logger"] == "memex.api"


# ---- GET /logs: búsqueda substring (q) ----------------------------------------------------------


def test_logs_search_q_matches_event_fields_and_exception(client: Any) -> None:
    _seed_log_event(event="needle.event", fields="{}")
    _seed_log_event(event="other", fields='{"detail": "hay un needle adentro"}')
    _seed_log_event(event="boom", exception="Traceback ... needle en el stack")
    _seed_log_event(event="nada", fields="{}")

    r = client.get("/logs?q=needle").json()
    assert r["total"] == 3  # match en event, en fields y en exception
    events = {i["event"] for i in r["items"]}
    assert events == {"needle.event", "other", "boom"}


# ---- GET /logs: escalares de correlación --------------------------------------------------------


def test_logs_filter_request_id(client: Any) -> None:
    _seed_log_event(event="a", request_id="req-aaa")
    _seed_log_event(event="b", request_id="req-bbb")
    r = client.get("/logs?request_id=req-aaa").json()
    assert r["total"] == 1
    assert r["items"][0]["request_id"] == "req-aaa"


def test_logs_filter_scalars_run_source_inbox(client: Any) -> None:
    _seed_log_event(event="r", run_id="run-1")
    _seed_log_event(event="s", source_id=5)
    _seed_log_event(event="i", inbox_id=9)
    assert client.get("/logs?run_id=run-1").json()["total"] == 1
    assert client.get("/logs?source_id=5").json()["total"] == 1
    assert client.get("/logs?inbox_id=9").json()["total"] == 1


# ---- GET /logs: rango temporal ------------------------------------------------------------------


def test_logs_filter_since_until(client: Any) -> None:
    _seed_log_event(event="viejo", ts=datetime(2026, 5, 10, 12, tzinfo=UTC))
    _seed_log_event(event="medio", ts=datetime(2026, 5, 20, 12, tzinfo=UTC))
    _seed_log_event(event="nuevo", ts=datetime(2026, 5, 30, 12, tzinfo=UTC))
    # [05-15, 05-25): solo "medio" (until es exclusivo, since inclusivo)
    r = client.get("/logs?since=2026-05-15T00:00:00Z&until=2026-05-25T00:00:00Z").json()
    assert r["total"] == 1
    assert r["items"][0]["event"] == "medio"


# ---- GET /logs: paginación ----------------------------------------------------------------------


def test_logs_pagination_total_and_limit_offset(client: Any) -> None:
    for n in range(5):
        _seed_log_event(event=f"e{n}")
    p0 = client.get("/logs?limit=2&offset=0").json()
    assert p0["total"] == 5
    assert len(p0["items"]) == 2
    p2 = client.get("/logs?limit=2&offset=4").json()
    assert p2["total"] == 5
    assert len(p2["items"]) == 1


# ---- GET /logs: orden ---------------------------------------------------------------------------


def test_logs_sort_dir_by_ts(client: Any) -> None:
    _seed_log_event(event="t1", ts=datetime(2026, 5, 1, 12, tzinfo=UTC))
    _seed_log_event(event="t2", ts=datetime(2026, 5, 2, 12, tzinfo=UTC))
    _seed_log_event(event="t3", ts=datetime(2026, 5, 3, 12, tzinfo=UTC))
    desc = [i["event"] for i in client.get("/logs?sort=ts&dir=desc").json()["items"]]
    assert desc == ["t3", "t2", "t1"]
    asc = [i["event"] for i in client.get("/logs?sort=ts&dir=asc").json()["items"]]
    assert asc == ["t1", "t2", "t3"]


# ---- GET /logs: filas con user_id NULL son visibles (feed de debug íntegro) ---------------------


def test_logs_null_user_rows_are_visible(client: Any) -> None:
    _seed_log_event(event="con-user", user_id=1)
    _seed_log_event(event="pre-auth", user_id=None)
    r = client.get("/logs").json()
    events = {i["event"] for i in r["items"]}
    assert events == {"con-user", "pre-auth"}
    null_row = next(i for i in r["items"] if i["event"] == "pre-auth")
    assert null_row["user_id"] is None


def test_logs_empty(client: Any) -> None:
    body = client.get("/logs").json()
    assert body == {"items": [], "total": 0}


def test_logs_tz_invalid_returns_422(client: Any) -> None:
    assert client.get("/logs?tz=Marte/Olympus").status_code == 422


def test_logs_fields_roundtrip_as_dict(client: Any) -> None:
    _seed_log_event(event="con-fields", fields='{"model": "deepseek-chat", "duration_ms": 120}')
    item = client.get("/logs?event=con-fields").json()["items"][0]
    assert item["fields"]["model"] == "deepseek-chat"
    assert item["fields"]["duration_ms"] == 120


# ---- GET /logs/stats: cortes por nivel/evento + error_rate -------------------------------------


def test_stats_by_level_and_by_event(client: Any) -> None:
    _seed_log_event(level="info", event="a")
    _seed_log_event(level="info", event="a")
    _seed_log_event(level="error", event="b")
    s = client.get("/logs/stats").json()
    by_level = {r["level"]: r["count"] for r in s["by_level"]}
    by_event = {r["event"]: r["count"] for r in s["by_event"]}
    assert by_level == {"info": 2, "error": 1}
    assert by_event == {"a": 2, "b": 1}
    assert s["total"] == 3
    assert s["errors"] == 1
    assert abs(s["error_rate"] - (1 / 3)) < 1e-9


def test_stats_error_rate_zero_when_empty(client: Any) -> None:
    s = client.get("/logs/stats").json()
    assert s["total"] == 0
    assert s["errors"] == 0
    assert s["error_rate"] == 0.0


def test_stats_by_logger_excludes_null(client: Any) -> None:
    _seed_log_event(logger="memex.api")
    _seed_log_event(logger="memex.api")
    _seed_log_event(logger=None, event="sin-logger")
    s = client.get("/logs/stats").json()
    by_logger = {r["logger"]: r["count"] for r in s["by_logger"]}
    assert by_logger == {"memex.api": 2}  # la fila sin logger no aparece


# ---- GET /logs/stats: histograma con buckets (tz) ----------------------------------------------


def test_stats_histogram_buckets_respect_tz(client: Any) -> None:
    # 2026-06-02T04:00Z = 2026-06-01 23:00 en Bogotá (UTC-5). Con día de granularidad el bucket
    # debe caer en 06-01 (Bogotá) y no en 06-02 (UTC). Rango de >4d fuerza granularidad 'day'.
    _seed_log_event(level="info", event="x", ts=datetime(2026, 6, 2, 4, tzinfo=UTC))
    _seed_log_event(level="error", event="y", ts=datetime(2026, 6, 2, 4, tzinfo=UTC))
    s = client.get(
        "/logs/stats?since=2026-05-25T00:00:00Z&until=2026-06-05T00:00:00Z&tz=America/Bogota"
    ).json()
    assert len(s["histogram"]) == 1
    bucket = s["histogram"][0]
    assert bucket["bucket"].startswith("2026-06-01")
    assert bucket["total"] == 2
    assert bucket["errors"] == 1


# ---- GET /logs/stats: latencia (percentiles sobre duration_ms) ---------------------------------


def test_stats_latency_null_when_no_duration(client: Any) -> None:
    _seed_log_event(event="sin-latencia", fields="{}")
    lat = client.get("/logs/stats").json()["latency"]
    assert lat["p50"] is None
    assert lat["p95"] is None
    assert lat["p99"] is None


def test_stats_latency_percentiles_when_present(client: Any) -> None:
    for ms in (100, 200, 300, 400):
        _seed_log_event(event="con-latencia", fields=f'{{"duration_ms": {ms}}}')
    # Una fila sin duration_ms NO debe entrar al cálculo de percentiles.
    _seed_log_event(event="sin-latencia", fields="{}")
    lat = client.get("/logs/stats").json()["latency"]
    assert lat["p50"] is not None
    assert lat["p95"] is not None
    assert lat["p99"] is not None
    # p50 dentro del rango sembrado; p99 cerca del máximo.
    assert 100 <= lat["p50"] <= 400
    assert lat["p50"] <= lat["p95"] <= lat["p99"]
    assert lat["p99"] <= 400


def test_stats_sink_dropped_present(client: Any) -> None:
    # `sink_dropped` siempre presente (entero >= 0) — viene del health del sink.
    s = client.get("/logs/stats").json()
    assert isinstance(s["sink_dropped"], int)
    assert s["sink_dropped"] >= 0
