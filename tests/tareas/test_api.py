"""API /tareas: lista por asunto con todos los estados y cambio de estado desde el dashboard."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.tareas.module import add


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    from memex.api.app import app
    from memex.config import settings

    monkeypatch.setattr(settings, "auth_enforced", False)
    monkeypatch.setattr(settings, "api_token", "")
    return TestClient(app)


def _asunto(name: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_asuntos (user_id, name, kind, status, aliases) "
                    "VALUES (1, :n, 'programa', 'abierto', ARRAY[]::text[]) RETURNING id"
                ),
                {"n": name},
            ).scalar_one()
        )


def test_list_by_asunto_all_states_and_set_status(client: Any) -> None:
    curso = _asunto(f"Curso API {uuid4().hex[:5]}")
    with connection() as c:
        a = add(
            c, 1, title=f"Ver el video {uuid4().hex[:4]}", asunto=curso, due_on=date(2026, 9, 8)
        )
        b = add(c, 1, title=f"Llegar puntual {uuid4().hex[:4]}", asunto=curso)

    r = client.get(f"/tareas?asunto_id={curso}&status=all")
    assert r.status_code == 200, r.text
    ids = {t["id"] for t in r.json()["items"]}
    assert {a["id"], b["id"]} <= ids
    row = next(t for t in r.json()["items"] if t["id"] == a["id"])
    assert (row["asunto_name"], row["status"], row["due_on"]) and row["asunto_id"] == curso

    r = client.post(f"/tareas/{a['id']}/status", json={"status": "completada"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completada" and r.json()["done_at"]
    assert a["id"] not in {
        t["id"] for t in client.get(f"/tareas?asunto_id={curso}").json()["items"]
    }

    r = client.post(f"/tareas/{a['id']}/status", json={"status": "pendiente"})
    assert r.json()["status"] == "pendiente" and r.json()["done_at"] is None

    assert client.post(f"/tareas/{a['id']}/status", json={"status": "hecha"}).status_code == 422
    assert client.post("/tareas/999999999/status", json={"status": "completada"}).status_code == 404


def test_discard_endpoint_marks_not_a_task(client: Any) -> None:
    curso = _asunto(f"Curso API descartar {uuid4().hex[:5]}")
    with connection() as c:
        t = add(c, 1, title=f"Participar en clase {uuid4().hex[:4]}", asunto=curso)
    r = client.post(f"/tareas/{t['id']}/discard", json={"note": "exhortación"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "cancelada" and body["metadata"]["discard_reason"] == "no_es_tarea"
    assert client.post("/tareas/999999999/discard", json={}).status_code == 404


def test_parent_endpoint_links_and_unlinks(client: Any) -> None:
    curso = _asunto(f"Curso API proyecto {uuid4().hex[:5]}")
    with connection() as c:
        proyecto = add(
            c,
            1,
            title=f"Proyecto de Canva {uuid4().hex[:4]}",
            asunto=curso,
            metadata={"is_parent": True},
        )
        paso = add(c, 1, title=f"Paso 1 {uuid4().hex[:4]}", asunto=curso)
    r = client.post(f"/tareas/{paso['id']}/parent", json={"parent_id": proyecto["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["parent_id"] == proyecto["id"]
    row = next(
        t
        for t in client.get(f"/tareas?asunto_id={curso}&status=all").json()["items"]
        if t["id"] == paso["id"]
    )
    assert row["parent_id"] == proyecto["id"]
    assert (
        client.post(f"/tareas/{paso['id']}/parent", json={"parent_id": None}).json()["parent_id"]
        is None
    )
    assert (
        client.post(
            f"/tareas/{proyecto['id']}/parent", json={"parent_id": proyecto["id"]}
        ).status_code
        == 422
    )
