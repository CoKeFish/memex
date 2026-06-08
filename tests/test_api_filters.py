"""CRUD HTTP de /filters (gestión de filter_rules desde el dashboard)."""

from __future__ import annotations

from typing import Any


def test_filters_crud(client: Any, seed_source: dict[str, Any]) -> None:
    # crear
    r = client.post(
        "/filters",
        json={
            "source_type": "imap",
            "scope": {"from.email": {"equals": "x@y.com"}},
            "action": "ignore",
            "priority": 150,
        },
    )
    assert r.status_code == 200
    body = r.json()
    rid = body["id"]
    assert body["action"] == "ignore" and body["enabled"] is True and body["priority"] == 150
    assert "user_id" not in body  # no se expone

    # listar
    items = client.get("/filters").json()["items"]
    assert any(i["id"] == rid for i in items)

    # patch (deshabilitar + cambiar prioridad)
    p = client.patch(f"/filters/{rid}", json={"enabled": False, "priority": 200})
    assert p.status_code == 200 and p.json()["enabled"] is False and p.json()["priority"] == 200

    # borrar
    assert client.delete(f"/filters/{rid}").json()["deleted"] is True
    assert client.patch(f"/filters/{rid}", json={"enabled": True}).status_code == 404
    assert client.delete(f"/filters/{rid}").status_code == 404


def test_filter_invalid_action_is_422(client: Any) -> None:
    r = client.post("/filters", json={"scope": {}, "action": "nope"})
    assert r.status_code == 422


def test_filter_create_is_idempotent(client: Any, seed_source: dict[str, Any]) -> None:
    """Crear una regla idéntica dos veces no duplica: devuelve la misma (bloquear/descartar 2x)."""
    body = {
        "source_type": "imap",
        "scope": {"from.email": {"equals": "dup@x.com"}},
        "action": "ignore",
    }
    a = client.post("/filters", json=body).json()
    b = client.post("/filters", json=body).json()
    assert a["id"] == b["id"]
    rules = client.get("/filters").json()["items"]
    same = [r for r in rules if r["scope"] == {"from.email": {"equals": "dup@x.com"}}]
    assert len(same) == 1
