"""Endpoint del grafo (Fase 2): POST /graph/build (paso determinista) + GET /graph (lectura de
vértices + aristas, con filtro por status). inbox NO es vértice.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection


def _exec(sql: str, **p: Any) -> Any:
    with connection() as c:
        r = c.execute(text(sql), p)
        return r.scalar() if r.returns_rows else None


def _finance(merchant: str, inbox_ids: list[int]) -> int:
    return int(
        _exec(
            "INSERT INTO mod_finance_transactions "
            "(user_id, source_inbox_ids, direction, amount, currency, occurred_at, counterparty) "
            "VALUES (1, :ids, 'egreso', 100, 'COP', NOW(), :m) RETURNING id",
            ids=inbox_ids,
            m=merchant,
        )
    )


def _hack(name: str, inbox_ids: list[int]) -> int:
    return int(
        _exec(
            "INSERT INTO mod_hackathones_events (user_id, source_inbox_ids, name) "
            "VALUES (1, :ids, :n) RETURNING id",
            ids=inbox_ids,
            n=name,
        )
    )


def test_graph_vacio(client: Any) -> None:
    body = client.get("/graph").json()
    assert body == {"nodes": [], "edges": []}


def test_build_y_lectura(client: Any) -> None:
    _finance("Rappi", [5])
    _hack("HackBogota", [5])
    built = client.post("/graph/build").json()
    assert built["cooccurrence_pistas"] == 1
    assert built["afiliacion_reales"] == 0

    body = client.get("/graph").json()
    assert len(body["nodes"]) == 2
    assert {n["kind"] for n in body["nodes"]} == {"transaccion", "hackaton"}
    assert len(body["edges"]) == 1
    e = body["edges"][0]
    assert e["producer"] == "inbox"
    assert e["status"] == "pista"
    assert e["relation_type"] == "co-ocurrencia"


def test_build_idempotente(client: Any) -> None:
    _finance("Rappi", [5])
    _hack("Hack", [5])
    client.post("/graph/build")
    client.post("/graph/build")
    assert len(client.get("/graph").json()["edges"]) == 1


def test_status_filtra_aristas(client: Any) -> None:
    # una PISTA (co-ocurrencia) + una REAL (persona↔org)
    _finance("Rappi", [5])
    _hack("Hack", [5])
    p = int(
        _exec(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1, 'persona', 'Juan') RETURNING id"
        )
    )
    o = int(
        _exec(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1, 'organizacion', 'Acme') RETURNING id"
        )
    )
    _exec(
        "INSERT INTO mod_identidades_person_orgs (user_id, person_id, org_id) VALUES (1, :p, :o)",
        p=p,
        o=o,
    )
    client.post("/graph/build")

    confirmed = client.get("/graph?status=confirmed").json()["edges"]
    pistas = client.get("/graph?status=pista").json()["edges"]
    assert len(confirmed) == 1
    assert confirmed[0]["relation_type"] == "afiliado"
    assert len(pistas) == 1
    assert pistas[0]["relation_type"] == "co-ocurrencia"
