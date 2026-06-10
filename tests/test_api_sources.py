from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection


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
    client: Any, seed_source: dict[str, Any], seed_user2: int
) -> None:
    with connection() as c:
        c.execute(
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


def test_ensure_source_isolates_per_user(client: Any, seed_user2: int) -> None:
    with connection() as c:
        c.execute(
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


def test_checkpoint_cross_tenant_is_404(client: Any, seed_user2: int) -> None:
    with connection() as c:
        other_id = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type) "
                "VALUES (:uid, 'u2-src', 'imap') RETURNING id"
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


def test_social_account_cross_tenant_404(client: Any, seed_user2: int) -> None:
    with connection() as c:
        other_id = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type) "
                "VALUES (:uid, 'u2-ig', 'instagram') RETURNING id"
            ),
            {"uid": seed_user2},
        ).scalar()
    assert (
        client.post(f"/sources/{other_id}/social/accounts", json={"handle": "x"}).status_code == 404
    )


# ----- editar / borrar source + reset de checkpoint ------------------------ #


def test_patch_source_edits_config_and_name(client: Any) -> None:
    src = client.post(
        "/sources", json={"name": "imap-mal", "type": "imap", "config": {"server": "viejo"}}
    ).json()
    r = client.patch(
        f"/sources/{src['id']}", json={"name": "imap-bien", "config": {"server": "nuevo"}}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "imap-bien"
    assert body["config"] == {"server": "nuevo"}


def test_patch_source_duplicate_name_409(client: Any) -> None:
    client.post("/sources", json={"name": "ocupado", "type": "imap"})
    src = client.post("/sources", json={"name": "movible", "type": "imap"}).json()
    assert client.patch(f"/sources/{src['id']}", json={"name": "ocupado"}).status_code == 409


def test_delete_source_guards_inbox_then_cascade(client: Any) -> None:
    # sin inbox: borra directo (204).
    empty = client.post("/sources", json={"name": "vacia", "type": "imap"}).json()
    assert client.delete(f"/sources/{empty['id']}").status_code == 204
    # con inbox: 409 sin cascade, 204 con cascade, y queda inaccesible. La fila se siembra
    # COMMITEADA (bloque propio): vía el fixture `conn` (txn abierta) el guard no la vería y
    # el DELETE del endpoint quedaría bloqueado para siempre en el lock FK de esa txn.
    src = client.post("/sources", json={"name": "con-historial", "type": "imap"}).json()
    sid = src["id"]
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :sid, 'e1', NOW(), '{}'::jsonb)"
            ),
            {"sid": sid},
        )
    assert client.delete(f"/sources/{sid}").status_code == 409
    assert client.delete(f"/sources/{sid}?cascade=true").status_code == 204
    assert client.get(f"/sources/{sid}").status_code == 404


def test_get_source_returns_one_and_404(client: Any, seed_source: dict[str, Any]) -> None:
    r = client.get(f"/sources/{seed_source['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == seed_source["id"]
    assert client.get("/sources/999999").status_code == 404


def test_delete_checkpoint_resets(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    client.put(f"/sources/{sid}/checkpoint", json={"cursor": {"last_uid": 7}})
    assert client.delete(f"/sources/{sid}/checkpoint").status_code == 204
    assert client.get(f"/sources/{sid}/checkpoint").json() == {"cursor": None}


def test_patch_source_rejects_sub_minimum_schedule(
    client: Any, seed_source: dict[str, Any]
) -> None:
    """fetch_schedule por debajo del piso (60s) → 422; un intervalo razonable se acepta."""
    r = client.patch(f"/sources/{seed_source['id']}", json={"fetch_schedule": "PT5S"})
    assert r.status_code == 422
    # Un intervalo razonable sigue aceptándose.
    assert (
        client.patch(f"/sources/{seed_source['id']}", json={"fetch_schedule": "PT15M"}).status_code
        == 200
    )


# --- token_source: de dónde resuelve el token de Apify cada fuente social -------------------


def test_token_source_env_and_missing(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _make_social_source(client)
    monkeypatch.delenv("MEMEX_APIFY_TOKEN", raising=False)
    assert client.get(f"/sources/{src['id']}").json()["token_source"] == "missing"
    monkeypatch.setenv("MEMEX_APIFY_TOKEN", "tok-123")
    assert client.get(f"/sources/{src['id']}").json()["token_source"] == "env"
    # En la lista viene calculado igual.
    listed = {s["id"]: s for s in client.get("/sources").json()}
    assert listed[src["id"]]["token_source"] == "env"


def test_token_source_none_for_non_social(client: Any, seed_source: dict[str, Any]) -> None:
    assert client.get(f"/sources/{seed_source['id']}").json()["token_source"] is None


def test_source_row_exposes_fetch_modes_and_caveats(client: Any) -> None:
    """La UI habilita modos por SourceRow.fetch_modes (server-driven, no hardcodea tipos)."""
    row = client.post(
        "/sources", json={"name": "ig-caps", "type": "instagram", "config": {}}
    ).json()
    assert row["fetch_modes"] == ["incremental", "range", "last"]
    assert "range" in row["mode_caveats"]  # aviso del rango de IG (sin techo nativo)
    listed = {s["id"]: s for s in client.get("/sources").json()}
    assert listed[row["id"]]["fetch_modes"] == ["incremental", "range", "last"]
    # un tipo sin ingestor traíble no ofrece modos
    weird = client.post("/sources", json={"name": "w", "type": "dummy", "config": {}}).json()
    assert weird["fetch_modes"] == []
