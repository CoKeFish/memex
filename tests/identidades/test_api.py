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


def test_delete_guard_bloquea_si_participa(client: Any) -> None:
    # M7: borrar una identidad con menciones la dejaría huérfana → 409, salvo ?force=true.
    oid = _create(client, kind="organizacion", display_name="Rappi")["id"]
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_identidades_mentions "
                "(user_id, source_inbox_ids, mentioned_name, resolved_identity_id, "
                " resolution_method) VALUES (1, ARRAY[5], 'Rappi', :id, 'created')"
            ),
            {"id": oid},
        )
    blocked = client.delete(f"/identidades/{oid}")
    assert blocked.status_code == 409
    assert "merge" in blocked.json()["detail"].lower()
    assert client.delete(f"/identidades/{oid}?force=true").status_code == 200  # force borra igual
    assert client.get(f"/identidades/{oid}").status_code == 404


def test_create_invalid_kind(client: Any) -> None:
    r = client.post("/identidades", json={"kind": "nope", "display_name": "X"})
    assert r.status_code == 422


def test_producto_crud_filtro_y_limites(client: Any) -> None:
    # producto es kind canónico: POST/PATCH/filtro funcionan; sedes y afiliación lo rechazan
    prod = _create(client, kind="producto", display_name="Steam")
    assert prod["kind"] == "producto"
    pid_prod = prod["id"]

    listed = client.get("/identidades?kind=producto").json()["items"]
    assert [p["id"] for p in listed] == [pid_prod]
    assert client.get("/identidades?kind=organizacion").json()["items"] == []

    r = client.patch(f"/identidades/{pid_prod}", json={"kind": "producto", "notes": "tienda"})
    assert r.status_code == 200 and r.json()["notes"] == "tienda"

    # sedes: solo organizaciones
    assert client.post(f"/identidades/{pid_prod}/sites", json={"address": "x"}).status_code == 422
    # afiliación: sigue siendo persona → organización (producto en cualquier lado → 422)
    persona = _create(client, kind="persona", display_name="Ada")["id"]
    assert client.post(f"/identidades/{persona}/orgs", json={"org_id": pid_prod}).status_code == 422
    assert client.post(f"/identidades/{pid_prod}/orgs", json={"org_id": persona}).status_code == 422


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


def test_parent_set_clear_and_children(client: Any) -> None:
    parent = _create(client, kind="organizacion", display_name="Universidad del Norte")["id"]
    child = _create(client, kind="organizacion", display_name="Ingeniería Mecánica")["id"]

    # setear el padre
    r = client.patch(f"/identidades/{child}", json={"parent_id": parent})
    assert r.status_code == 200, r.text

    cd = client.get(f"/identidades/{child}").json()
    assert cd["identity"]["parent_id"] == parent
    assert cd["identity"]["parent_name"] == "Universidad del Norte"

    # el padre lista al hijo en "children"
    pd = client.get(f"/identidades/{parent}").json()
    assert [c["id"] for c in pd["children"]] == [child]

    # la lista trae parent_name del hijo
    listed = client.get("/identidades?kind=organizacion").json()["items"]
    row = next(o for o in listed if o["id"] == child)
    assert row["parent_id"] == parent and row["parent_name"] == "Universidad del Norte"

    # limpiar el padre (null explícito)
    assert client.patch(f"/identidades/{child}", json={"parent_id": None}).status_code == 200
    assert client.get(f"/identidades/{child}").json()["identity"]["parent_id"] is None


def test_parent_validations(client: Any) -> None:
    a = _create(client, kind="organizacion", display_name="A")["id"]
    b = _create(client, kind="organizacion", display_name="B")["id"]

    # propio padre
    assert client.patch(f"/identidades/{a}", json={"parent_id": a}).status_code == 422
    # padre inexistente
    assert client.patch(f"/identidades/{a}", json={"parent_id": 99999}).status_code == 422
    # ciclo: B cuelga de A, luego intentar colgar A de B
    assert client.patch(f"/identidades/{b}", json={"parent_id": a}).status_code == 200
    assert client.patch(f"/identidades/{a}", json={"parent_id": b}).status_code == 422


def test_mention_count(client: Any) -> None:
    oid = _create(client, kind="organizacion", display_name="Acme")["id"]
    with connection() as c:
        for _ in range(3):
            c.execute(
                text(
                    "INSERT INTO mod_identidades_mentions "
                    "(user_id, source_inbox_ids, mentioned_name, resolved_kind, "
                    " resolved_identity_id) VALUES (1, ARRAY[1], 'Acme', 'organizacion', :o)"
                ),
                {"o": oid},
            )
    assert client.get(f"/identidades/{oid}").json()["identity"]["mention_count"] == 3
    listed = client.get("/identidades").json()["items"]
    assert next(o for o in listed if o["id"] == oid)["mention_count"] == 3


def test_organize_endpoint(client: Any, monkeypatch: Any) -> None:
    from memex.modules.identidades.hierarchy import OrganizeStats

    async def fake_organize(user_id: int, **kwargs: Any) -> OrganizeStats:
        return OrganizeStats(orgs=3, linked=2, created=1, cleaned=0, skipped=0)

    monkeypatch.setattr("memex.api.routers.identidades.run_organize", fake_organize)
    r = client.post("/identidades/organize")
    assert r.status_code == 200, r.text
    assert r.json() == {"orgs": 3, "linked": 2, "created": 1, "cleaned": 0, "skipped": 0}


def test_sync_missing_account_returns_errors(client: Any) -> None:
    r = client.post("/identidades/sync", json={"account_id": 9999})
    assert r.status_code == 200
    assert r.json()["errors"] >= 1
