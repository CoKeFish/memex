"""Endpoint del grafo (Fase 2): POST /graph/build (paso determinista) + GET /graph (lectura de
vértices + aristas, con filtro por status). inbox NO es vértice.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.bienestar.habits import add_habit
from memex.modules.bienestar.module import register
from memex.relations.clustering import cluster_signature
from memex.relations.edges import (
    PRODUCER_IDENTIDADES,
    STATUS_CONFIRMED,
    Ref,
    list_edges,
    propose_edge,
)


def _exec(sql: str, **p: Any) -> Any:
    with connection() as c:
        r = c.execute(text(sql), p)
        return r.scalar() if r.returns_rows else None


def _source(stype: str, name: str) -> int:
    """Una fuente del tipo dado; `name` único por test (UNIQUE sources_user_id_name_key)."""
    return int(
        _exec(
            "INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id",
            n=name,
            t=stype,
        )
    )


def _inbox(source_id: int, ext: str) -> int:
    """Un mensaje real en `inbox` (para `inbox_kinds`); `received_at` tiene DEFAULT NOW()."""
    return int(
        _exec(
            "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
            "VALUES (1, :sid, :ext, NOW(), CAST('{}' AS JSONB)) RETURNING id",
            sid=source_id,
            ext=ext,
        )
    )


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


def _registro(activity: str, category: str = "otros") -> int:
    """Un registro de bienestar (el vértice `registro`). `occurred_at`=NOW()."""
    return int(
        _exec(
            "INSERT INTO mod_bienestar_registros (user_id, category, activity, occurred_at) "
            "VALUES (1, :cat, :act, NOW()) RETURNING id",
            cat=category,
            act=activity,
        )
    )


def _habito(
    name: str, *, activity: str = "", category: str | None = None, cadence: str = "daily"
) -> int:
    """Un hábito de bienestar (el vértice `bienestar:habito`)."""
    return int(
        _exec(
            "INSERT INTO mod_bienestar_habits (user_id, name, activity, category, cadence) "
            "VALUES (1, :n, :a, :c, :cad) RETURNING id",
            n=name,
            a=activity,
            c=category,
            cad=cadence,
        )
    )


def test_graph_vacio(client: Any) -> None:
    body = client.get("/graph").json()
    assert body == {"nodes": [], "edges": [], "inbox_kinds": {}}


def test_build_y_lectura(client: Any) -> None:
    _finance("Rappi", [5])
    _hack("HackBogota", [5])
    built = client.post("/graph/build").json()
    assert built["cooccurrence_pistas"] == 1
    assert built["afiliacion_reales"] == 0
    # los campos de canal/remitente siempre vienen (0 sin datos de chat)
    assert built["participa_reales"] == 0
    assert built["canales"] == 0
    assert built["chat_senders"] == 0

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

    assert client.get("/graph?source_inbox_id=999").json() == {
        "nodes": [],
        "edges": [],
        "inbox_kinds": {},
    }


def test_inbox_kinds_por_medio(client: Any) -> None:
    """`inbox_kinds` mapea cada mensaje REFERENCIADO a su medio real (telegram→chat, imap→email);
    tipos sin SourceKind y mensajes no referenciados por ningún vértice se omiten (JSON serializa
    las llaves int como strings)."""
    tg = _inbox(_source("telegram", "tg"), "t1")
    im = _inbox(_source("imap", "mail"), "m1")
    wh = _inbox(_source("webhook", "hook"), "w1")  # tipo sin SourceKind registrada → omitido
    _inbox(_source("imap", "mail2"), "m2")  # no referenciado por ningún vértice → omitido
    _finance("Rappi", [tg, im, wh])
    _hack("HackBogota", [tg])
    client.post("/graph/build")

    body = client.get("/graph").json()
    assert body["inbox_kinds"] == {str(tg): "chat", str(im): "email"}


def test_inbox_kinds_en_foco(client: Any) -> None:
    tg = _inbox(_source("telegram", "tg"), "t1")
    _finance("Rappi", [tg])
    _hack("Hack", [tg])
    client.post("/graph/build")

    body = client.get(f"/graph?source_inbox_id={tg}").json()
    assert body["inbox_kinds"] == {str(tg): "chat"}


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


def test_cumple_por_actividad(client: Any) -> None:
    # registro "correr" ↔ hábito "Correr" (match de actividad, insensible a mayúsculas).
    _registro("correr", "ejercicio")
    _habito("Correr", activity="Correr")
    built = client.post("/graph/build").json()
    assert built["cumple_reales"] == 1

    body = client.get("/graph").json()
    assert {"registro", "habito"} <= {n["kind"] for n in body["nodes"]}
    cumple = [e for e in body["edges"] if e["relation_type"] == "cumple"]
    assert len(cumple) == 1
    e = cumple[0]
    assert e["src_slug"] == "bienestar"  # registro → hábito (dirigida)
    assert e["dst_slug"] == "bienestar:habito"
    assert e["producer"] == "bienestar"
    assert e["status"] == "confirmed"


def test_cumple_por_categoria(client: Any) -> None:
    # hábito sin actividad → matchea por categoría.
    _registro("", "ejercicio")
    _habito("Ejercicio", category="ejercicio")
    built = client.post("/graph/build").json()
    assert built["cumple_reales"] == 1


def test_cumple_sin_match(client: Any) -> None:
    _registro("nadar", "ejercicio")
    _habito("Correr", activity="correr")
    built = client.post("/graph/build").json()
    assert built["cumple_reales"] == 0
    assert all(e["relation_type"] != "cumple" for e in client.get("/graph").json()["edges"])


def test_cumple_idempotente(client: Any) -> None:
    _registro("correr", "ejercicio")
    _habito("Correr", activity="correr")
    client.post("/graph/build")
    client.post("/graph/build")  # 2da corrida: no duplica la arista
    edges = client.get("/graph?status=confirmed").json()["edges"]
    assert len([e for e in edges if e["relation_type"] == "cumple"]) == 1


def test_cumple_multiples_habitos(client: Any) -> None:
    # un registro puede cumplir varios hábitos (por actividad Y por categoría) → varias aristas.
    _registro("correr", "ejercicio")
    _habito("Correr", activity="correr")
    _habito("Hacer ejercicio", category="ejercicio")
    built = client.post("/graph/build").json()
    assert built["cumple_reales"] == 2


def test_weave_cumple_incremental_registro_despues() -> None:
    # hábito primero, luego el registro: el weave de `register` teje la arista SIN correr build.
    with connection() as c:
        add_habit(c, 1, name="Correr", cadence="daily", activity="correr")
        register(c, 1, category="ejercicio", activity="Correr")
    with connection() as c:
        cumple = [e for e in list_edges(c, 1, status="confirmed") if e.relation_type == "cumple"]
    assert len(cumple) == 1
    assert cumple[0].src.slug == "bienestar"
    assert cumple[0].dst.slug == "bienestar:habito"


def test_weave_cumple_incremental_habito_despues() -> None:
    # un hábito NUEVO teje «cumple» contra los registros que ya lo cumplen, SIN correr build. (El
    # registro ya existía porque cumplía OTRO hábito — registrar exige un hábito activo.)
    with connection() as c:
        add_habit(c, 1, name="Correr", cadence="daily", activity="correr")
        register(c, 1, category="ejercicio", activity="correr")  # cumple el hábito de actividad
        add_habit(c, 1, name="Ejercicio", cadence="daily", category="ejercicio")  # por categoría
    with connection() as c:
        cumple = [e for e in list_edges(c, 1, status="confirmed") if e.relation_type == "cumple"]
    # el registro cumple AMBOS: actividad (al registrar) y categoría (al crear el hábito)
    assert len(cumple) == 2


# --- cúmulos: POST /graph/cluster, /graph/cluster/validate, GET /graph/clusters ----- #


def _person(name: str) -> int:
    return int(
        _exec(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1, 'persona', :n) RETURNING id",
            n=name,
        )
    )


def _confirmed_edge(a: int, b: int) -> None:
    with connection() as c:
        propose_edge(
            c,
            1,
            Ref("identidades:person", a),
            Ref("identidades:person", b),
            producer=PRODUCER_IDENTIDADES,
            relation_type="afiliado",
            status=STATUS_CONFIRMED,
        )


def _triangulo() -> None:
    a, b, c = _person("A"), _person("B"), _person("C")
    _confirmed_edge(a, b)
    _confirmed_edge(b, c)
    _confirmed_edge(a, c)


def test_cluster_endpoint_detecta_y_lista(client: Any) -> None:
    _triangulo()
    res = client.post("/graph/cluster").json()
    assert res["detected"] == 1
    assert res["new_candidates"] == 1
    clusters = client.get("/graph/clusters").json()["clusters"]
    assert len(clusters) == 1
    assert clusters[0]["status"] == "candidate"
    assert clusters[0]["member_count"] == 3


def test_clusters_filtra_por_status(client: Any) -> None:
    _triangulo()
    client.post("/graph/cluster")
    assert len(client.get("/graph/clusters?status=candidate").json()["clusters"]) == 1
    assert client.get("/graph/clusters?status=confirmed").json()["clusters"] == []


def _confirmed_cluster(members: list[tuple[str, int]]) -> int:
    """Cúmulo CONFIRMADO con sus miembros (mismo seed que tests/relations/test_timeline.py pero
    vía `connection()` propia: el test llama al API y el fixture `conn` dejaría la txn abierta)."""
    sig = cluster_signature([Ref(s, i) for s, i in members])
    cid = int(
        _exec(
            "INSERT INTO relation_clusters (user_id, status, name, description, confidence, "
            "member_count, signature, blob_signature, validated_signature) "
            "VALUES (1, 'confirmed', 'Mi contexto', 'sinopsis', 0.9, :mc, :sig, :sig, :sig) "
            "RETURNING id",
            mc=len(members),
            sig=sig,
        )
    )
    for s, i in members:
        _exec(
            "INSERT INTO relation_cluster_members (user_id, cluster_id, member_slug, member_id) "
            "VALUES (1, :c, :s, :i)",
            c=cid,
            s=s,
            i=i,
        )
    return cid


def test_timeline_inbox_kinds(client: Any) -> None:
    """La cronología también trae `inbox_kinds` (acá por la rama del ELENCO: hackatón sin fecha)."""
    tg = _inbox(_source("telegram", "tg"), "t1")
    hk = _hack("HackX", [tg])
    cid = _confirmed_cluster([("hackathones", hk)])

    body = client.get(f"/graph/clusters/{cid}/timeline").json()
    assert body["actors"][0]["source_inbox_ids"] == [tg]
    assert body["inbox_kinds"] == {str(tg): "chat"}


def test_validate_endpoint_mapea_stats(client: Any, monkeypatch: Any) -> None:
    # el partidor real usa LLM; se mockea para probar el wiring + mapeo de la respuesta.
    from memex.api.routers import graph as graph_router
    from memex.relations.clusters_llm import ClusterPartitionStats

    async def _fake(user_id: int, *, limit: int | None = None) -> ClusterPartitionStats:
        return ClusterPartitionStats(blobs=2, groups=3, created=2, synced=1, rejected=1, promoted=4)

    monkeypatch.setattr(graph_router, "run_cluster_partition", _fake)
    body = client.post("/graph/cluster/validate").json()
    assert body["blobs"] == 2
    assert body["groups"] == 3
    assert body["created"] == 2
    assert body["promoted"] == 4
    assert body["cost_usd"] == 0.0
