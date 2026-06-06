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
    """Cobro CONSOLIDADO (el vértice de finanzas) + su crudo + el link (provenance de inbox)."""
    crudo = int(
        _exec(
            "INSERT INTO mod_finance_transactions "
            "(user_id, source_inbox_ids, direction, amount, currency, occurred_at, counterparty) "
            "VALUES (1, :ids, 'egreso', 100, 'COP', NOW(), :m) RETURNING id",
            ids=inbox_ids,
            m=merchant,
        )
    )
    cons = int(
        _exec(
            "INSERT INTO mod_finance_consolidated (user_id, direction, amount, currency, "
            "occurred_at, counterparty) VALUES (1, 'egreso', 100, 'COP', NOW(), :m) RETURNING id",
            m=merchant,
        )
    )
    _exec(
        "INSERT INTO mod_finance_transaction_links (user_id, consolidated_id, transaction_id) "
        "VALUES (1, :c, :t)",
        c=cons,
        t=crudo,
    )
    return cons


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


def test_source_inbox_id_enfoca_subgrafo(client: Any) -> None:
    """`?source_inbox_id=` enfoca el grafo en lo que produjo ese correo (sus vértices + vecinos a un
    salto); el sentido inverso del drill-down nodo→correo. Un correo sin nada → grafo vacío."""
    _finance("Rappi", [5])
    _hack("HackBogota", [5])
    _finance("Netflix", [9])  # otro correo, sin relación con los del correo 5
    client.post("/graph/build")

    focado = client.get("/graph?source_inbox_id=5").json()
    assert len(focado["nodes"]) == 2  # solo los 2 del correo 5 (Netflix del 9 queda fuera)
    assert all(5 in n["source_inbox_ids"] for n in focado["nodes"])
    assert len(focado["edges"]) == 1  # la co-ocurrencia entre ellos sí aparece

    assert client.get("/graph?source_inbox_id=999").json() == {"nodes": [], "edges": []}


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


def test_get_graph_poda_aristas_huerfanas(client: Any) -> None:
    # build crea 1 pista; tombstoneamos el consolidado SIN re-build → la poda de LECTURA descarta la
    # arista colgante (aísla el filtro de get_graph del GC de build, que no corre acá).
    fin = _finance("Rappi", [5])
    _hack("HackBogota", [5])
    client.post("/graph/build")
    assert len(client.get("/graph").json()["edges"]) == 1
    _exec("UPDATE mod_finance_consolidated SET deleted = TRUE WHERE id = :i", i=fin)
    body = client.get("/graph").json()
    assert len(body["nodes"]) == 1  # solo el hackatón sobrevive como vértice
    assert body["edges"] == []  # la arista huérfana se poda en lectura
    # también en modo foco: el correo 5 ya no debe mostrar la arista colgante
    focado = client.get("/graph?source_inbox_id=5").json()
    assert focado["edges"] == []
