"""API /identidades con el TestClient: CRUD de orgs, listados, detalle, asociación y sync."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection


def test_org_crud_and_detail(client: Any) -> None:
    r = client.post(
        "/identidades/orgs", json={"name": "Unity", "kind": "producto", "domains": ["Unity.com"]}
    )
    assert r.status_code == 200, r.text
    org = r.json()
    assert org["name"] == "Unity"
    assert org["interest"] is True
    assert org["domains"] == ["unity.com"]  # normalizado a minúsculas
    oid = org["id"]

    assert any(o["id"] == oid for o in client.get("/identidades/orgs").json()["items"])

    r = client.patch(
        f"/identidades/orgs/{oid}", json={"description": "game engine", "aliases": ["Unity3D"]}
    )
    assert r.status_code == 200
    assert r.json()["description"] == "game engine"
    assert r.json()["aliases"] == ["Unity3D"]

    detail = client.get(f"/identidades/orgs/{oid}")
    assert detail.status_code == 200
    assert detail.json()["org"]["id"] == oid
    assert detail.json()["members"] == []

    assert client.delete(f"/identidades/orgs/{oid}").status_code == 200
    assert client.get(f"/identidades/orgs/{oid}").status_code == 404


def test_create_org_invalid_kind(client: Any) -> None:
    assert client.post("/identidades/orgs", json={"name": "X", "kind": "nope"}).status_code == 422


def test_empty_listings(client: Any) -> None:
    assert client.get("/identidades/persons").json() == {"items": [], "next_cursor": None}
    assert client.get("/identidades/mentions").json() == {"items": [], "next_cursor": None}
    assert client.get("/identidades/provider-accounts").json() == {"items": []}
    assert client.get("/identidades/sync-runs").json() == {"items": [], "next_cursor": None}


def test_person_detail_and_association(client: Any) -> None:
    with connection() as c:
        pid = c.execute(
            text(
                "INSERT INTO mod_identidades_persons (user_id, display_name, source) "
                "VALUES (1, 'Ada', 'manual') RETURNING id"
            )
        ).scalar_one()
    oid = client.post("/identidades/orgs", json={"name": "Anthropic"}).json()["id"]

    r = client.post(f"/identidades/persons/{pid}/orgs", json={"org_id": oid, "role": "Researcher"})
    assert r.status_code == 200
    assert [p["id"] for p in r.json()["members"]] == [pid]

    person = client.get(f"/identidades/persons/{pid}")
    assert person.status_code == 200
    assert [o["id"] for o in person.json()["orgs"]] == [oid]

    # filtro por org en el listado de personas
    listed = client.get(f"/identidades/persons?org_id={oid}")
    assert [p["id"] for p in listed.json()["items"]] == [pid]


def test_sync_missing_account_returns_errors(client: Any) -> None:
    r = client.post("/identidades/sync", json={"account_id": 9999})
    assert r.status_code == 200
    assert r.json()["errors"] >= 1
