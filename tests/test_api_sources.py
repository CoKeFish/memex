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
        text("INSERT INTO sources (user_id, name, type) VALUES (:uid, 'other-src', 'imap')"),
        {"uid": seed_user2},
    )
    r = client.get("/sources")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    assert names == {"imap-test"}


def test_ensure_source_creates_when_missing(client: Any) -> None:
    r = client.post(
        "/sources/ensure",
        json={"name": "imap-uni", "type": "imap", "config": {"folder": "INBOX"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "imap-uni"
    assert body["type"] == "imap"
    assert body["config"] == {"folder": "INBOX"}
    assert isinstance(body["id"], int)


def test_ensure_source_returns_existing_without_mutating(client: Any) -> None:
    first = client.post("/sources/ensure", json={"name": "x", "type": "imap"}).json()
    second = client.post(
        "/sources/ensure",
        json={"name": "x", "type": "imap", "config": {"foo": "bar"}},
    ).json()
    assert second["id"] == first["id"]
    assert second["config"] == first["config"]  # no se sobreescribió


def test_ensure_source_isolates_per_user(client: Any, seed_user2: int, conn: Any) -> None:
    conn.execute(
        text("INSERT INTO sources (user_id, name, type) VALUES (:uid, 'shared-name', 'imap')"),
        {"uid": seed_user2},
    )
    r = client.post("/sources/ensure", json={"name": "shared-name", "type": "imap"})
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == 1  # del cliente, no del otro user


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
            "INSERT INTO sources (user_id, name, type) VALUES (:uid, 'u2-src', 'imap') RETURNING id"
        ),
        {"uid": seed_user2},
    ).scalar()
    assert client.get(f"/sources/{other_id}/checkpoint").status_code == 404
    assert client.put(f"/sources/{other_id}/checkpoint", json={"cursor": {}}).status_code == 404


# ----- social allowlist (followed accounts) -------------------------------- #


def _make_social_source(client: Any, source_type: str = "instagram") -> dict[str, Any]:
    r = client.post(
        "/sources",
        json={"name": f"{source_type}-test", "type": source_type, "config": {"accounts": []}},
    )
    assert r.status_code == 201
    body: dict[str, Any] = r.json()
    return body


def test_add_social_account_normalizes_and_persists(client: Any) -> None:
    src = _make_social_source(client)
    r = client.post(f"/sources/{src['id']}/social/accounts", json={"handle": "@UTN.FRBA"})
    assert r.status_code == 200
    assert r.json()["config"]["accounts"] == [{"account": "utn.frba", "priority": False}]


def test_add_social_account_accepts_url_and_priority(client: Any) -> None:
    src = _make_social_source(client)
    r = client.post(
        f"/sources/{src['id']}/social/accounts",
        json={"handle": "https://www.instagram.com/Fiuba/", "priority": True},
    )
    assert r.status_code == 200
    assert r.json()["config"]["accounts"] == [{"account": "fiuba", "priority": True}]


def test_add_social_account_dedupes_409(client: Any) -> None:
    src = _make_social_source(client)
    first = client.post(f"/sources/{src['id']}/social/accounts", json={"handle": "utn.frba"})
    assert first.status_code == 200
    # mismo handle vía URL → normaliza igual → 409
    r = client.post(
        f"/sources/{src['id']}/social/accounts",
        json={"handle": "https://instagram.com/utn.frba/"},
    )
    assert r.status_code == 409


def test_add_social_account_rejects_non_social_422(
    client: Any, seed_source: dict[str, Any]
) -> None:
    r = client.post(f"/sources/{seed_source['id']}/social/accounts", json={"handle": "x"})
    assert r.status_code == 422


def test_remove_social_account(client: Any) -> None:
    src = _make_social_source(client)
    client.post(f"/sources/{src['id']}/social/accounts", json={"handle": "a"})
    client.post(f"/sources/{src['id']}/social/accounts", json={"handle": "b"})
    r = client.delete(f"/sources/{src['id']}/social/accounts/a")
    assert r.status_code == 200
    assert [a["account"] for a in r.json()["config"]["accounts"]] == ["b"]


def test_remove_social_account_404_when_absent(client: Any) -> None:
    src = _make_social_source(client)
    assert client.delete(f"/sources/{src['id']}/social/accounts/ghost").status_code == 404


def test_social_account_cross_tenant_404(client: Any, seed_user2: int, conn: Any) -> None:
    other_id = conn.execute(
        text(
            "INSERT INTO sources (user_id, name, type) "
            "VALUES (:uid, 'u2-ig', 'instagram') RETURNING id"
        ),
        {"uid": seed_user2},
    ).scalar()
    assert (
        client.post(f"/sources/{other_id}/social/accounts", json={"handle": "x"}).status_code == 404
    )
