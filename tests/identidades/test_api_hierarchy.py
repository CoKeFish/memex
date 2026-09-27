"""API de la jerarquía evaluada contra el libro: GET /identidades/hierarchy, GET
/identidades/{id}/claims, POST /identidades/claims/{id}/reject, POST
/identidades/{id}/confirm-parent (pinea manual), POST /identidades/derive, POST
/identidades/seed-claims y validación de scope de /curate (codex no se invoca acá)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.identidades.links import METHOD_LLM, claim_and_derive_parent


def _mk(kind: str, name: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_identidades (user_id, kind, display_name) VALUES (1, :k, :n) "
                    "RETURNING id"
                ),
                {"k": kind, "n": name},
            ).scalar_one()
        )


def _claim(child: int, parent: int, inbox: int, conf: float = 0.85) -> None:
    with connection() as c:
        claim_and_derive_parent(
            c,
            1,
            child_id=child,
            target_id=parent,
            target_name="",
            confidence=conf,
            method=METHOD_LLM,
            inbox_id=inbox,
        )


def test_hierarchy_claims_reject_confirm_derive(client: Any) -> None:
    child = _mk("organizacion", "Semillero Z")
    uni = _mk("organizacion", "Universidad X")
    otra = _mk("organizacion", "Empresa Y")
    _claim(child, uni, 1)
    _claim(child, uni, 2)
    _claim(child, otra, 3, conf=0.95)

    r = client.get("/identidades/hierarchy")
    assert r.status_code == 200, r.text
    body = r.json()
    node = next(n for n in body["nodes"] if n["id"] == child)
    assert node["parent_id"] == uni and node["parent_source"] == "derived"
    assert node["messages"] == 2 and node["disputed"] is True and node["pinned"] is False
    assert {c["target_id"] for c in node["candidates"]} == {uni, otra}
    assert body["settings"]["parent_min_messages"] == 2.0

    r = client.get(f"/identidades/{child}/claims")
    assert r.status_code == 200
    claims = r.json()
    assert len(claims["outgoing"]) == 3 and claims["incoming"] == []
    assert {c["target_name"] for c in claims["outgoing"]} == {"Universidad X", "Empresa Y"}
    r = client.get(f"/identidades/{uni}/claims")
    assert len(r.json()["incoming"]) == 2

    # Rechazar el reclamo desviado → ya no hay disputa.
    desviado = next(c for c in claims["outgoing"] if c["target_id"] == otra)
    r = client.post(f"/identidades/claims/{desviado['id']}/reject")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    node = next(n for n in client.get("/identidades/hierarchy").json()["nodes"] if n["id"] == child)
    assert node["disputed"] is False
    assert len(client.get(f"/identidades/{child}/claims").json()["outgoing"]) == 2
    assert (
        len(client.get(f"/identidades/{child}/claims?include_rejected=true").json()["outgoing"])
        == 3
    )

    # Confirmar el padre actual → pineado (manual); la derivación no lo toca más.
    r = client.post(f"/identidades/{child}/confirm-parent")
    assert r.status_code == 200 and r.json()["parent_id"] == uni
    node = next(n for n in client.get("/identidades/hierarchy").json()["nodes"] if n["id"] == child)
    assert node["pinned"] is True and node["parent_source"] == "manual"
    for inbox in (10, 11, 12):
        _claim(child, otra, inbox, conf=0.95)
    r = client.post("/identidades/derive")
    assert r.status_code == 200 and r.json()["changed"] == []
    node = next(n for n in client.get("/identidades/hierarchy").json()["nodes"] if n["id"] == child)
    assert node["parent_id"] == uni

    # 404 / 422
    assert client.get("/identidades/999999/claims").status_code == 404
    assert client.post("/identidades/claims/999999/reject").status_code == 404
    assert client.post(f"/identidades/{uni}/confirm-parent").status_code == 422
    assert client.post("/identidades/curate", json={"scope": "personas"}).status_code == 422


def test_seed_claims_endpoint(client: Any) -> None:
    child = _mk("organizacion", "Carrera Q")
    uni = _mk("organizacion", "Universidad X")
    with connection() as c:
        c.execute(
            text(
                "UPDATE mod_identidades SET parent_identity_id = :p, "
                "metadata = jsonb_set(metadata, '{parent_source}', '\"llm\"') WHERE id = :c"
            ),
            {"p": uni, "c": child},
        )
    r = client.post("/identidades/seed-claims")
    assert r.status_code == 200
    assert r.json() == {"parents": 1, "affiliations": 0, "mention_affiliations": 0}
    node = next(n for n in client.get("/identidades/hierarchy").json()["nodes"] if n["id"] == child)
    assert node["methods"] == ["legacy"] and node["weak"] is True
