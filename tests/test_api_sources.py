from __future__ import annotations

from typing import Any

from sqlalchemy import text


def test_create_source(client: Any) -> None:
    r = client.post(
        "/sources", json={"name": "imap-personal", "type": "imap", "config": {"folder": "INBOX"}}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "imap-personal"
    assert body["type"] == "imap"
    assert body["enabled"] is True
    assert body["config"] == {"folder": "INBOX"}
    assert isinstance(body["id"], int)


def test_create_source_duplicate_name_returns_409(client: Any) -> None:
    payload = {"name": "dup", "type": "imap"}
    assert client.post("/sources", json=payload).status_code == 201
    r = client.post("/sources", json=payload)
    assert r.status_code == 409


def test_list_sources_returns_only_current_user(
    client: Any, seed_source: dict[str, Any], seed_user2: int, conn: Any
) -> None:
    conn.execute(
        text("INSERT INTO sources (user_id, name, type) " "VALUES (:uid, 'other-src', 'imap')"),
        {"uid": seed_user2},
    )
    r = client.get("/sources")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    assert names == {"imap-test"}


def test_get_checkpoint_returns_none_initially(client: Any, seed_source: dict[str, Any]) -> None:
    r = client.get(f"/sources/{seed_source['id']}/checkpoint")
    assert r.status_code == 200
    assert r.json() == {"cursor": None}


def test_put_then_get_checkpoint(client: Any, seed_source: dict[str, Any]) -> None:
    cur = {"uidvalidity": 1, "last_uid": 42}
    r = client.put(f"/sources/{seed_source['id']}/checkpoint", json={"cursor": cur})
    assert r.status_code == 200
    r = client.get(f"/sources/{seed_source['id']}/checkpoint")
    assert r.json() == {"cursor": cur}


def test_checkpoint_cross_tenant_is_404(client: Any, seed_user2: int, conn: Any) -> None:
    other_id = conn.execute(
        text(
            "INSERT INTO sources (user_id, name, type) "
            "VALUES (:uid, 'u2-src', 'imap') RETURNING id"
        ),
        {"uid": seed_user2},
    ).scalar()
    assert client.get(f"/sources/{other_id}/checkpoint").status_code == 404
    assert client.put(f"/sources/{other_id}/checkpoint", json={"cursor": {}}).status_code == 404
