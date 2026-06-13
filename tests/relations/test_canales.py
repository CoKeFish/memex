"""Canal de chat como vértice: sync desde payloads (idempotente, título más reciente), proyección,
aristas REALES `participa_en` persona→canal, co-ocurrencia canal↔contenido del mensaje, el canal NO
cuenta para el cap, y su participación (configurable) en el grafo a clusterizar."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from memex.config import settings
from memex.db import connection
from memex.relations.clustering import build_cluster_graph
from memex.relations.deterministic import build_relations
from memex.relations.edges import (
    PRODUCER_CANAL,
    PROVENANCE_EXTRACTED,
    RELTYPE_PARTICIPA_EN,
    VERDICT_CONFIRMED,
    Ref,
    list_edges,
    propose_edge,
)
from memex.relations.vertices import list_vertices

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


def _exec(sql: str, **params: Any) -> Any:
    with connection() as c:
        result = c.execute(text(sql), params)
        return result.scalar() if result.returns_rows else None


def _source(stype: str, name: str) -> int:
    return int(
        _exec(
            "INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id",
            n=name,
            t=stype,
        )
    )


def _inbox(source_id: int, ext: str, payload: dict[str, Any]) -> int:
    return int(
        _exec(
            "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
            "VALUES (1, :sid, :ext, NOW(), CAST(:p AS JSONB)) RETURNING id",
            sid=source_id,
            ext=ext,
            p=json.dumps(payload),
        )
    )


def _tg_payload(
    tg_id: int | None,
    *,
    chat_id: int = 900,
    chat_title: str = "Parche",
    username: str | None = None,
    display_name: str | None = None,
    is_bot: bool = False,
) -> dict[str, Any]:
    sender = (
        None
        if tg_id is None
        else {
            "user_id": tg_id,
            "username": username,
            "display_name": display_name,
            "is_bot": is_bot,
        }
    )
    return {
        "chat_id": chat_id,
        "chat_kind": "group",
        "chat_title": chat_title,
        "sender": sender,
        "message_id": 1,
        "text": "hola",
        "date": "2026-06-11T10:00:00Z",
    }


def _finance(merchant: str, inbox_ids: list[int]) -> int:
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


def _canales() -> list[tuple[str, str]]:
    with connection() as c:
        return [
            (str(r.external_id), str(r.display_name))
            for r in c.execute(
                text(
                    "SELECT external_id, display_name FROM mod_canales WHERE user_id = 1 "
                    "ORDER BY external_id"
                )
            ).all()
        ]


def _pair(e: Any) -> set[tuple[str, int]]:
    return {(e.src.slug, e.src.id), (e.dst.slug, e.dst.id)}


def test_sync_idempotente_y_titulo_mas_reciente() -> None:
    src = _source("telegram", "tg")
    _inbox(src, "m1", _tg_payload(None, chat_title="Parche"))
    _inbox(src, "m2", _tg_payload(None, chat_title="Parche 2.0"))  # renombrado
    with connection() as c:
        stats = build_relations(c, 1)
    assert stats.canales == 1
    assert _canales() == [("900", "Parche 2.0")]
    with connection() as c:
        build_relations(c, 1)  # re-correr no duplica
    assert _canales() == [("900", "Parche 2.0")]


def test_canal_es_vertice() -> None:
    src = _source("telegram", "tg")
    _inbox(src, "m1", _tg_payload(None, chat_title="Parche"))
    with connection() as c:
        build_relations(c, 1)
        verts = list_vertices(c, 1, slugs=("canal",))
    assert len(verts) == 1
    assert verts[0].kind == "canal"
    assert verts[0].label == "Parche"


def test_participa_en_confirmed() -> None:
    src = _source("telegram", "tg")
    _inbox(src, "m1", _tg_payload(111, display_name="Juan Niebla"))
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1, producer=PRODUCER_CANAL)
    assert stats.participa_reales == 1
    assert len(edges) == 1
    e = edges[0]
    assert e.verdict == VERDICT_CONFIRMED
    assert e.relation_type == RELTYPE_PARTICIPA_EN
    assert e.src.slug == "identidades:person"  # dirigida: quién → dónde
    assert e.dst.slug == "canal"


def test_bot_sin_participa_en() -> None:
    src = _source("telegram", "tg")
    _inbox(src, "m1", _tg_payload(500, username="robobot", is_bot=True))
    with connection() as c:
        stats = build_relations(c, 1)
    assert stats.participa_reales == 0
    assert stats.canales == 1  # el canal sí existe (el mensaje es del chat)


def test_canal_coocurre_y_participa_suprime_su_pista() -> None:
    # mensaje de chat con un gasto extraído: 3 vértices (canal, remitente, gasto). La pista
    # remitente↔canal se SUPRIME (ya hay participa_en confirmed del par); quedan canal↔gasto y
    # remitente↔gasto.
    src = _source("telegram", "tg")
    mid = _inbox(src, "m1", _tg_payload(111, display_name="Juan Niebla"))
    fin = _finance("Rappi", [mid])
    with connection() as c:
        stats = build_relations(c, 1)
        pistas = list_edges(c, 1, producer="inbox")
    assert stats.cooccurrence_pistas == 2
    canal_id = int(_exec("SELECT id FROM mod_canales WHERE user_id = 1"))
    pares = [_pair(e) for e in pistas]
    assert any(("canal", canal_id) in p and ("finance", fin) in p for p in pares)
    assert not any(
        ("canal", canal_id) in p and any(s == "identidades:person" for s, _ in p) for p in pares
    )


def test_canal_no_cuenta_para_el_cap() -> None:
    # 3 vértices de CONTENIDO (remitente + 2 gastos) + canal. Con cap=3 el mensaje NO se salta
    # (canal fuera del conteo, 4 vértices emiten pares); con cap=2 sí.
    src = _source("telegram", "tg")
    mid = _inbox(src, "m1", _tg_payload(111, display_name="Juan Niebla"))
    _finance("A", [mid])
    _finance("B", [mid])
    with connection() as c:
        stats = build_relations(c, 1, cooccurrence_cap=3)
    assert stats.high_fanout_skipped == 0
    # C(4,2)=6 pares - 1 suprimido (remitente↔canal ya confirmed por participa_en) = 5 pistas
    assert stats.cooccurrence_pistas == 5
    _exec("DELETE FROM relation_edges WHERE user_id = 1")
    with connection() as c:
        stats2 = build_relations(c, 1, cooccurrence_cap=2)
    assert stats2.high_fanout_skipped == 1
    assert stats2.cooccurrence_pistas == 0


def _canal_row(conn: Connection, external_id: str, name: str) -> Ref:
    cid = conn.execute(
        text(
            "INSERT INTO mod_canales (user_id, platform, external_id, display_name) "
            "VALUES (1, 'telegram', :e, :n) RETURNING id"
        ),
        {"e": external_id, "n": name},
    ).scalar_one()
    return Ref("canal", int(cid))


def _person_row(conn: Connection, name: str) -> Ref:
    pid = conn.execute(
        text(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1, 'persona', :n) RETURNING id"
        ),
        {"n": name},
    ).scalar_one()
    return Ref("identidades:person", int(pid))


def test_canal_clusteriza_por_default(conn: Connection) -> None:
    canal = _canal_row(conn, "900", "Parche")
    a = _person_row(conn, "A")
    b = _person_row(conn, "B")
    for p in (a, b):
        propose_edge(
            conn,
            1,
            p,
            canal,
            producer=PRODUCER_CANAL,
            relation_type=RELTYPE_PARTICIPA_EN,
            verdict=VERDICT_CONFIRMED,
            provenance=PROVENANCE_EXTRACTED,
        )
    g = build_cluster_graph(conn, 1)
    assert ("canal", canal.id) in g.nodes


def test_cluster_exclude_canal(conn: Connection) -> None:
    canal = _canal_row(conn, "900", "Parche")
    a = _person_row(conn, "A")
    propose_edge(
        conn,
        1,
        a,
        canal,
        producer=PRODUCER_CANAL,
        relation_type=RELTYPE_PARTICIPA_EN,
        verdict=VERDICT_CONFIRMED,
        provenance=PROVENANCE_EXTRACTED,
    )
    cfg = settings.model_copy(update={"cluster_exclude_canal": True})
    g = build_cluster_graph(conn, 1, cfg)
    assert ("canal", canal.id) not in g.nodes
    # la persona queda aislada sin su arista al canal → también fuera (no clusteriza sola)
    assert ("identidades:person", a.id) not in g.nodes
