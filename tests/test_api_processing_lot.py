"""Lote de procesamiento por ventanas: endpoints /processing/lot* + servicio de avance.

El avance se prueba llamando `lots.run_advance` DIRECTO (await): el endpoint solo lo encola con
`asyncio.create_task` y un TestClient sin lifespan no garantiza que esa task corra. Las etapas
usadas son deterministas (`classify`) — sin LLM ni red, igual que el resto de la suite.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.processing import lots
from memex.scheduler import runs as worker_runs


def _seed_inbox(source_id: int, eid: str, occurred_at: str) -> int:
    with connection() as c:
        iid = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (1, :sid, :eid, :at, CAST(:p AS JSONB))
                RETURNING id
                """
            ),
            {
                "sid": source_id,
                "eid": eid,
                "at": occurred_at,
                "p": json.dumps({"subject": eid, "body_text": "x"}),
            },
        ).scalar()
    assert isinstance(iid, int)
    return iid


def _lot_targets() -> list[int]:
    with connection() as c:
        row = c.execute(text("SELECT target_ids FROM processing_lots WHERE user_id = 1")).scalar()
    assert row is not None
    return [int(i) for i in row]


def _running_reprocess_row() -> int:
    """Simula una corrida en curso (el candado que comparten /run y el lote)."""
    rid = worker_runs.start_run(1, "reprocess")
    with connection() as c:
        c.execute(text("UPDATE worker_runs SET run_type = 'reprocess' WHERE id = :id"), {"id": rid})
    return rid


def _run_row(rid: int) -> dict[str, Any]:
    with connection() as c:
        row = (
            c.execute(
                text("SELECT status, stats, error FROM worker_runs WHERE id = :id"), {"id": rid}
            )
            .mappings()
            .first()
        )
    assert row is not None
    return dict(row)


def _create_lot(client: Any, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"stages": ["classify"], **overrides}
    r = client.post("/processing/lot", json=body)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


# --- alta / estado ---
def test_configure_lot_snapshot_chronological(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    # Insertados en orden de id INVERSO al cronológico: el snapshot debe quedar por occurred_at.
    i_new = _seed_inbox(sid, "l-nuevo", "2026-06-03T10:00:00Z")
    i_mid = _seed_inbox(sid, "l-medio", "2026-06-02T10:00:00Z")
    i_old = _seed_inbox(sid, "l-viejo", "2026-06-01T10:00:00Z")

    state = _create_lot(client)
    assert state["total"] == 3
    assert state["frontier"] == 0
    assert state["status"] == "active"
    assert state["window_size"] == 50  # default del medio email (fuente imap)
    assert state["defaults"] == {"email": 50, "chat": 200, "social": 100}
    assert state["history"] == []
    assert state["spent_usd"] == 0.0
    assert state["busy"] is False
    assert _lot_targets() == [i_old, i_mid, i_new]

    got = client.get("/processing/lot")
    assert got.status_code == 200
    assert got.json()["total"] == 3


def test_configure_lot_validations(client: Any, seed_source: dict[str, Any]) -> None:
    assert client.post("/processing/lot", json={"stages": []}).status_code == 422
    # Filtro que no matchea nada (fuente sin mensajes) → 422, no un lote vacío.
    r = client.post(
        "/processing/lot", json={"stages": ["classify"], "source_id": seed_source["id"]}
    )
    assert r.status_code == 422


def test_get_lot_404_without_one(client: Any) -> None:
    assert client.get("/processing/lot").status_code == 404


def test_delete_lot(client: Any, seed_source: dict[str, Any]) -> None:
    _seed_inbox(seed_source["id"], "d1", "2026-06-01T10:00:00Z")
    _create_lot(client)
    assert client.delete("/processing/lot").status_code == 204
    assert client.get("/processing/lot").status_code == 404


# --- defaults por medio ---
def test_window_defaults_patch_and_resolution(client: Any, seed_source: dict[str, Any]) -> None:
    r = client.get("/processing/window-defaults")
    assert r.status_code == 200
    assert r.json()["sizes"] == {"email": 50, "chat": 200, "social": 100}

    r = client.patch("/processing/window-defaults", json={"sizes": {"email": 7}})
    assert r.status_code == 200
    assert r.json()["sizes"]["email"] == 7
    assert r.json()["sizes"]["chat"] == 200  # los no enviados no se tocan

    assert (
        client.patch("/processing/window-defaults", json={"sizes": {"palomas": 9}}).status_code
        == 422
    )
    assert (
        client.patch("/processing/window-defaults", json={"sizes": {"email": 0}}).status_code == 422
    )

    # El lote nuevo de una fuente email toma el default editado; el explícito gana.
    _seed_inbox(seed_source["id"], "w1", "2026-06-01T10:00:00Z")
    assert _create_lot(client)["window_size"] == 7
    assert _create_lot(client, window_size=3)["window_size"] == 3


# --- avance (servicio directo, determinista) ---
def test_advance_window_then_rest(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    for n, day in enumerate(("01", "02", "03"), start=1):
        _seed_inbox(sid, f"a{n}", f"2026-06-{day}T10:00:00Z")
    _create_lot(client, window_size=2)

    rid = _running_reprocess_row()
    asyncio.run(lots.run_advance(1, rid, rest=False))

    state = client.get("/processing/lot").json()
    assert state["frontier"] == 2
    assert state["status"] == "active"
    assert len(state["history"]) == 1
    win = state["history"][0]
    assert (win["start_idx"], win["end_idx"], win["n"]) == (0, 2, 2)
    assert win["results"]["classify"]["classified"] == 2
    assert win["cost_usd"] == 0.0 and win["errors"] == 0
    run = _run_row(rid)
    assert run["status"] == "ok"
    assert run["stats"] == {"windows": 1, "targets": 2, "cost_usd": 0.0}

    rid2 = _running_reprocess_row()
    asyncio.run(lots.run_advance(1, rid2, rest=True))
    state = client.get("/processing/lot").json()
    assert state["frontier"] == 3
    assert state["status"] == "done"
    assert len(state["history"]) == 2
    assert state["history"][1]["n"] == 1
    assert state["spent_usd"] == 0.0

    # Con el lote completo, el endpoint contesta done sin encolar nada.
    r = client.post("/processing/lot/advance")
    assert r.status_code == 200
    assert r.json() == {"run_id": None, "status": "done", "window": None}


def test_advance_logs_carry_run_id_and_single_target_inbox(
    client: Any, seed_source: dict[str, Any], sink_capture: Any
) -> None:
    """La corrida de lote bindea run_id por CONTEXTVARS: tanto los eventos propios de lots
    (`processing.lot.window_done`) como los heredados río abajo (`reprocess.done`) llegan al sink
    con la columna run_id = str(id de worker_runs) — antes el run_id int se tragaba entero. Con
    ventana de 1 target, `reprocess` además bindea inbox_id."""
    sid = seed_source["id"]
    iid = _seed_inbox(sid, "L1", "2026-06-01T10:00:00Z")
    _create_lot(client, window_size=1)

    rid = _running_reprocess_row()
    asyncio.run(lots.run_advance(1, rid, rest=False))

    records: list[dict[str, Any]] = []
    while not sink_capture.empty():
        records.append(sink_capture.get_nowait())
    by_event = {r["event"]: r for r in records}
    assert by_event["processing.lot.window_done"]["run_id"] == str(rid)
    assert by_event["reprocess.done"]["run_id"] == str(rid)
    assert by_event["reprocess.done"]["inbox_id"] == iid
    # Fuera del scope del lote no queda contexto colgado (bound_log_context restaura).
    import structlog

    assert "run_id" not in structlog.contextvars.get_contextvars()


def test_advance_window_size_override_persists(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    for n, day in enumerate(("01", "02", "03"), start=1):
        _seed_inbox(sid, f"o{n}", f"2026-06-{day}T10:00:00Z")
    _create_lot(client, window_size=2)

    rid = _running_reprocess_row()
    asyncio.run(lots.run_advance(1, rid, rest=False, window_size=1))

    state = client.get("/processing/lot").json()
    assert state["frontier"] == 1
    assert state["window_size"] == 1  # el override queda como nuevo default del lote


def test_hard_stage_failure_keeps_frontier(
    client: Any, seed_source: dict[str, Any], monkeypatch: Any
) -> None:
    """Una falla DURA de etapa (slot {"error": ...}) corta sin avanzar: el lote queda reanudable."""
    sid = seed_source["id"]
    _seed_inbox(sid, "f1", "2026-06-01T10:00:00Z")
    _seed_inbox(sid, "f2", "2026-06-02T10:00:00Z")
    _create_lot(client, window_size=2)

    async def fake_reprocess(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "targets": 2,
            "stages": ["classify"],
            "results": {"classify": {"error": "no quota"}},
            "cost_usd": 0.0,
        }

    monkeypatch.setattr(lots, "reprocess", fake_reprocess)
    rid = _running_reprocess_row()
    asyncio.run(lots.run_advance(1, rid, rest=True))

    state = client.get("/processing/lot").json()
    assert state["frontier"] == 0
    assert state["history"] == []
    run = _run_row(rid)
    assert run["status"] == "error"
    assert "classify" in (run["error"] or "")


def test_run_advance_without_lot_errors(client: Any) -> None:
    rid = _running_reprocess_row()
    asyncio.run(lots.run_advance(1, rid, rest=False))
    run = _run_row(rid)
    assert run["status"] == "error"
    assert "lote" in (run["error"] or "")


# --- candado de "una corrida a la vez" ---
def test_busy_blocks_lot_operations(client: Any, seed_source: dict[str, Any]) -> None:
    _seed_inbox(seed_source["id"], "b1", "2026-06-01T10:00:00Z")
    _create_lot(client)
    _running_reprocess_row()  # corrida en curso (queda 'running')

    assert client.post("/processing/lot/advance").status_code == 409
    assert client.post("/processing/lot/advance-rest").status_code == 409
    assert client.post("/processing/lot", json={"stages": ["classify"]}).status_code == 409
    assert client.delete("/processing/lot").status_code == 409
    assert client.get("/processing/lot").json()["busy"] is True
