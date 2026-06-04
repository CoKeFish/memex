"""API /identidades (modelo unificado) con el TestClient: CRUD de identidades, identificadores,
sedes, afiliación, listados, cola de merge + merge manual, y sync."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection


def _create(client: Any, **body: Any) -> dict[str, Any]:
    r = client.post("/identidades", json=body)
    assert r.status_code == 200, r.text
    data: dict[str, Any] = r.json()
    return data


def test_identity_crud_and_detail(client: Any) -> None:
    org = _create(client, kind="organizacion", display_name="Unity", aliases=["Unity3D"])
    assert org["kind"] == "organizacion" and org["interest"] is True
    oid = org["id"]

    listed = client.get("/identidades?kind=organizacion").json()["items"]
    assert any(o["id"] == oid for o in listed)
    assert client.get("/identidades?kind=persona").json()["items"] == []

    r = client.patch(f"/identidades/{oid}", json={"interest": False, "notes": "game engine"})
    assert r.status_code == 200
    assert r.json()["interest"] is False and r.json()["notes"] == "game engine"

    detail = client.get(f"/identidades/{oid}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["identity"]["id"] == oid
    assert body["identifiers"] == [] and body["affiliations"] == []

    assert client.delete(f"/identidades/{oid}").status_code == 200
    assert client.get(f"/identidades/{oid}").status_code == 404


def test_create_invalid_kind(client: Any) -> None:
    r = client.post("/identidades", json={"kind": "nope", "display_name": "X"})
    assert r.status_code == 422


def test_empty_listings(client: Any) -> None:
    assert client.get("/identidades").json() == {"items": [], "next_cursor": None}
    assert client.get("/identidades/mentions").json() == {"items": [], "next_cursor": None}
    assert client.get("/identidades/provider-accounts").json() == {"items": []}
    assert client.get("/identidades/sync-runs").json() == {"items": [], "next_cursor": None}
    assert client.get("/identidades/merge-candidates").json() == {"items": []}


def test_identifiers_add_and_delete(client: Any) -> None:
    oid = _create(client, kind="organizacion", display_name="Unity")["id"]
    r = client.post(
        f"/identidades/{oid}/identifiers",
        json={"platform": "domain", "kind": "domain", "value": "Unity.com"},
    )
    assert r.status_code == 200, r.text
    idf = r.json()
    assert idf["kind"] == "domain"

    detail = client.get(f"/identidades/{oid}").json()
    assert [i["value"] for i in detail["identifiers"]] == ["Unity.com"]

    assert client.delete(f"/identidades/{oid}/identifiers/{idf['id']}").status_code == 200
    assert client.get(f"/identidades/{oid}").json()["identifiers"] == []


def test_identifier_invalid_kind(client: Any) -> None:
    oid = _create(client, kind="organizacion", display_name="X")["id"]
    r = client.post(
        f"/identidades/{oid}/identifiers", json={"platform": "x", "kind": "nope", "value": "v"}
    )
    assert r.status_code == 422


def test_sites_only_for_orgs(client: Any) -> None:
    oid = _create(client, kind="organizacion", display_name="Acme")["id"]
    r = client.post(
        f"/identidades/{oid}/sites", json={"label": "HQ", "address": "Calle 1", "country": "CO"}
    )
    assert r.status_code == 200, r.text
    assert client.get(f"/identidades/{oid}").json()["sites"][0]["country"] == "CO"

    pid = _create(client, kind="persona", display_name="Ada")["id"]
    assert client.post(f"/identidades/{pid}/sites", json={"address": "x"}).status_code == 422


def test_affiliation_persona_org(client: Any) -> None:
    pid = _create(client, kind="persona", display_name="Ada")["id"]
    oid = _create(client, kind="organizacion", display_name="Anthropic")["id"]

    r = client.post(f"/identidades/{pid}/orgs", json={"org_id": oid, "role": "Researcher"})
    assert r.status_code == 200, r.text
    affs = r.json()["affiliations"]
    assert [a["id"] for a in affs] == [oid] and affs[0]["role"] == "Researcher"

    # la afiliación es bidireccional en el detalle de la org
    org_detail = client.get(f"/identidades/{oid}").json()
    assert [a["id"] for a in org_detail["affiliations"]] == [pid]


def test_affiliation_rejects_wrong_kinds(client: Any) -> None:
    p1 = _create(client, kind="persona", display_name="A")["id"]
    p2 = _create(client, kind="persona", display_name="B")["id"]
    assert client.post(f"/identidades/{p1}/orgs", json={"org_id": p2}).status_code == 422


def test_manual_merge(client: Any) -> None:
    a = _create(client, kind="organizacion", display_name="Globex")["id"]
    b = _create(client, kind="organizacion", display_name="Globex Corp")["id"]
    r = client.post("/identidades/merge", json={"survivor_id": a, "absorbed_id": b})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == a
    assert client.get(f"/identidades/{b}").status_code == 404
    assert "Globex Corp" in client.get(f"/identidades/{a}").json()["identity"]["aliases"]


def test_merge_candidate_confirm_and_reject(client: Any) -> None:
    # sembramos dos pares candidatos a mano
    with connection() as c:
        ids = [
            int(
                c.execute(
                    text(
                        "INSERT INTO mod_identidades (user_id, kind, display_name) "
                        "VALUES (1, 'persona', :n) RETURNING id"
                    ),
                    {"n": n},
                ).scalar_one()
            )
            for n in ("Ada Lovelace", "Ada L.", "Ana Pérez", "Ana Gómez")
        ]

        def _cand(x: int, y: int) -> int:
            lo, hi = min(x, y), max(x, y)
            return int(
                c.execute(
                    text(
                        "INSERT INTO mod_identidades_merge_candidates "
                        "(user_id, identity_a_id, identity_b_id, reason, score) "
                        "VALUES (1, :a, :b, 'trgm_name', 0.7) RETURNING id"
                    ),
                    {"a": lo, "b": hi},
                ).scalar_one()
            )

        c1 = _cand(ids[0], ids[1])
        c2 = _cand(ids[2], ids[3])

    assert len(client.get("/identidades/merge-candidates").json()["items"]) == 2

    # confirmar el primero → fusiona (la absorbida desaparece)
    assert client.post(f"/identidades/merge-candidates/{c1}/confirm").status_code == 200
    survivor, absorbed = sorted((ids[0], ids[1]))
    assert client.get(f"/identidades/{absorbed}").status_code == 404
    assert client.get(f"/identidades/{survivor}").status_code == 200

    # rechazar el segundo → coexisten
    assert client.post(f"/identidades/merge-candidates/{c2}/reject").status_code == 200
    assert client.get(f"/identidades/{ids[2]}").status_code == 200
    assert client.get(f"/identidades/{ids[3]}").status_code == 200
    # ya no quedan candidatos pendientes
    assert client.get("/identidades/merge-candidates").json()["items"] == []


def test_sync_missing_account_returns_errors(client: Any) -> None:
    r = client.post("/identidades/sync", json={"account_id": 9999})
    assert r.status_code == 200
    assert r.json()["errors"] >= 1
