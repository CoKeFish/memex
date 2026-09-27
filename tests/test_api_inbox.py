from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from memex.core.inbox import insert_record
from memex.core.source import SourceRecord
from memex.db import connection


def _seed_n(source_id: int, user_id: int, n: int, prefix: str = "r") -> None:
    with connection() as c:
        for i in range(n):
            insert_record(
                c,
                user_id=user_id,
                source_id=source_id,
                record=SourceRecord(
                    external_id=f"{prefix}{i}",
                    occurred_at=datetime(2026, 5, 23, 10, i, tzinfo=UTC),
                    payload={"i": i},
                    dedupe_keys=[],
                ),
            )


def test_list_inbox_returns_user_rows(client: Any, seed_source: dict[str, Any]) -> None:
    _seed_n(seed_source["id"], 1, 3)
    r = client.get("/inbox")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 3
    assert body["next_cursor"] is None


def test_list_inbox_filters_by_source(client: Any, seed_source: dict[str, Any]) -> None:
    with connection() as c:
        src2 = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type) "
                "VALUES (1, 'other-src', 'imap') RETURNING id"
            )
        ).scalar()
    assert isinstance(src2, int)
    _seed_n(seed_source["id"], 1, 2, prefix="a")
    _seed_n(src2, 1, 3, prefix="b")
    r = client.get(f"/inbox?source_id={seed_source['id']}")
    assert len(r.json()["items"]) == 2
    r = client.get(f"/inbox?source_id={src2}")
    assert len(r.json()["items"]) == 3


def test_list_inbox_pagination(client: Any, seed_source: dict[str, Any]) -> None:
    _seed_n(seed_source["id"], 1, 5)
    r1 = client.get("/inbox?limit=2")
    body1 = r1.json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"] is not None
    r2 = client.get(f"/inbox?limit=2&cursor={body1['next_cursor']}")
    body2 = r2.json()
    assert len(body2["items"]) == 2
    # ids strictly increasing
    assert body2["items"][0]["id"] > body1["items"][-1]["id"]


def test_list_inbox_processed_filter(client: Any, seed_source: dict[str, Any]) -> None:
    _seed_n(seed_source["id"], 1, 3)
    # Mark one as processed
    with connection() as c:
        c.execute(text("UPDATE inbox SET processed_at = NOW() WHERE external_id = 'r0'"))
    assert len(client.get("/inbox?processed=true").json()["items"]) == 1
    assert len(client.get("/inbox?processed=false").json()["items"]) == 2
    assert len(client.get("/inbox?processed=all").json()["items"]) == 3


def test_get_inbox_by_id(client: Any, seed_source: dict[str, Any]) -> None:
    _seed_n(seed_source["id"], 1, 1)
    list_resp = client.get("/inbox").json()
    rid = list_resp["items"][0]["id"]
    r = client.get(f"/inbox/{rid}")
    assert r.status_code == 200
    assert r.json()["external_id"] == "r0"


def test_get_inbox_cross_tenant_is_404(
    client: Any, seed_source: dict[str, Any], seed_user2: int
) -> None:
    with connection() as c:
        src2 = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (:u, 's', 'x') RETURNING id"),
            {"u": seed_user2},
        ).scalar()
    assert isinstance(src2, int)
    _seed_n(src2, seed_user2, 1, prefix="u2-")
    # Find user 2's row id
    with connection() as c:
        other_id = c.execute(
            text("SELECT id FROM inbox WHERE user_id = :u LIMIT 1"),
            {"u": seed_user2},
        ).scalar()
    r = client.get(f"/inbox/{other_id}")
    assert r.status_code == 404


def test_inbox_stats_scoped_to_user(
    client: Any, seed_source: dict[str, Any], seed_user2: int
) -> None:
    with connection() as c:
        src2 = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (:u, 's', 'x') RETURNING id"),
            {"u": seed_user2},
        ).scalar()
    assert isinstance(src2, int)
    _seed_n(seed_source["id"], 1, 2)
    _seed_n(src2, seed_user2, 5, prefix="u2-")
    r = client.get("/inbox/stats")
    sources = r.json()["sources"]
    assert str(seed_source["id"]) in sources
    assert sources[str(seed_source["id"])] == {"total": 2, "pending": 2, "errored": 0}
    assert str(src2) not in sources


def test_get_inbox_returns_trace_tree(client: Any, seed_source: dict[str, Any]) -> None:
    """GET /inbox/{id} serializa el árbol de traza (TraceNodeDto[]): nodos persistidos + las
    llm_calls del inbox colgadas del root como hojas `llm`."""
    _seed_n(seed_source["id"], 1, 1)
    rid = client.get("/inbox").json()["items"][0]["id"]
    with connection() as c:
        root = c.execute(
            text(
                "INSERT INTO trace_nodes (user_id, inbox_id, kind, label) "
                "VALUES (1, :i, 'root', 'msg') RETURNING id"
            ),
            {"i": rid},
        ).scalar_one()
        mod = c.execute(
            text(
                "INSERT INTO trace_nodes (user_id, inbox_id, parent_id, kind, module_slug, label) "
                "VALUES (1, :i, :p, 'module', 'finance', 'finance') RETURNING id"
            ),
            {"i": rid, "p": root},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO trace_nodes (user_id, inbox_id, parent_id, kind, label, ref_table, "
                "ref_id) VALUES (1, :i, :p, 'entity', 'egreso', 'mod_finance_transactions', 5)"
            ),
            {"i": rid, "p": mod},
        )
        c.execute(
            text(
                "INSERT INTO llm_calls (user_id, inbox_id, purpose, model, prompt_tokens, "
                "completion_tokens, cost_usd, latency_ms, status, response_text) "
                "VALUES (1, :i, 'extract_finance', 'fake', 1, 1, 0.004, 1, 'ok', '{\"items\":[]}')"
            ),
            {"i": rid},
        )

    trace = client.get(f"/inbox/{rid}").json()["trace"]
    assert trace is not None
    assert {"root", "module", "entity", "llm"} <= {n["kind"] for n in trace}
    entity = next(n for n in trace if n["kind"] == "entity")
    assert entity["ref"] == {"table": "mod_finance_transactions", "id": 5}
    llm = next(n for n in trace if n["kind"] == "llm")
    assert llm["label"] == "Extracción · finance"
    assert llm["llm"]["responseText"] == '{"items":[]}'


def test_get_inbox_trace_null_without_nodes(client: Any, seed_source: dict[str, Any]) -> None:
    """Sin nodos persistidos → trace=None (el front cae al fallback)."""
    _seed_n(seed_source["id"], 1, 1)
    rid = client.get("/inbox").json()["items"][0]["id"]
    assert client.get(f"/inbox/{rid}").json()["trace"] is None


def test_get_inbox_returns_relevance_verdict(client: Any, seed_source: dict[str, Any]) -> None:
    """GET /inbox/{id} surfacea el veredicto del gate (relevance_verdicts): conclusión + método +
    razón + modo. method='rule' trae los predicados de la regla (join a relevance_gate_rules)."""
    _seed_n(seed_source["id"], 1, 2)
    ids = [it["id"] for it in client.get("/inbox").json()["items"]]
    rid_llm, rid_rule = ids[0], ids[1]
    with connection() as c:
        rule_id = c.execute(
            text(
                "INSERT INTO relevance_gate_rules "
                "(user_id, effect, sender_kind, sender_value, status, proposed_by) "
                "VALUES (1, 'block', 'sender_email', 'promos@x.com', 'active', 'manual') "
                "RETURNING id"
            )
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO relevance_verdicts (user_id, inbox_id, verdict, method, reason, mode) "
                "VALUES (1, :i, 'relevant', 'llm', 'toca un interes', 'per_window')"
            ),
            {"i": rid_llm},
        )
        c.execute(
            text(
                "INSERT INTO relevance_verdicts (user_id, inbox_id, verdict, method, rule_id) "
                "VALUES (1, :i, 'not_relevant', 'rule', :r)"
            ),
            {"i": rid_rule, "r": rule_id},
        )

    v = client.get(f"/inbox/{rid_llm}").json()["relevance_verdict"]
    assert v["verdict"] == "relevant"
    assert v["method"] == "llm"
    assert v["reason"] == "toca un interes"
    assert v["mode"] == "per_window"
    assert v["rule_id"] is None and v["rule_sender_kind"] is None

    vr = client.get(f"/inbox/{rid_rule}").json()["relevance_verdict"]
    assert vr["verdict"] == "not_relevant"
    assert vr["method"] == "rule"
    assert vr["rule_id"] == rule_id
    assert vr["rule_effect"] == "block"
    assert vr["rule_sender_kind"] == "sender_email"
    assert vr["rule_sender_value"] == "promos@x.com"


def test_get_inbox_relevance_verdict_null_without_row(
    client: Any, seed_source: dict[str, Any]
) -> None:
    """Sin veredicto persistido → relevance_verdict=None (pendiente-de-gate o gate apagado)."""
    _seed_n(seed_source["id"], 1, 1)
    rid = client.get("/inbox").json()["items"][0]["id"]
    assert client.get(f"/inbox/{rid}").json()["relevance_verdict"] is None


def test_list_inbox_order_recent_is_by_occurred_at_desc_with_keyset(
    client: Any, seed_source: dict[str, Any]
) -> None:
    """`order=recent`: lo más NUEVO primero por occurred_at (no por id: un backfill de historia
    vieja entra con ids altos) y paginación keyset sin huecos ni repetidos. Es lo que usa el feed
    de /datos; el bug que lo motiva: cargar por id ASC con tope dejaba afuera justo lo nuevo."""
    sid = seed_source["id"]
    _seed_n(sid, 1, 5)  # 10:00 .. 10:04
    with connection() as c:  # backfill "viejo" ingresado DESPUÉS: ids más altos, fechas anteriores
        for i in range(2):
            insert_record(
                c,
                user_id=1,
                source_id=sid,
                record=SourceRecord(
                    external_id=f"old{i}",
                    occurred_at=datetime(2026, 5, 22, 9, i, tzinfo=UTC),
                    payload={},
                    dedupe_keys=[],
                ),
            )

    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, str] = {"order": "recent", "limit": "3"}
        if cursor is not None:
            params["cursor"] = cursor
        body = client.get("/inbox", params=params).json()
        seen.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == 7 and len({r["id"] for r in seen}) == 7
    times = [r["occurred_at"] for r in seen]
    assert times == sorted(times, reverse=True)
    assert seen[0]["external_id"] == "r4" and seen[-1]["external_id"] == "old0"
    assert client.get("/inbox", params={"order": "recent", "cursor": "basura"}).status_code == 422
    assert client.get("/inbox", params={"cursor": "basura"}).status_code == 422


def test_get_inbox_exposes_work_item_failures(client: Any, seed_source: dict[str, Any]) -> None:
    """El detalle de un mensaje trae sus fallas de procesamiento (cualquier status): el camino de
    procesamiento debe poder decir «la extracción falló y por qué» sin ir a la cola de revisión."""
    from memex.core.deadletter import record_failures

    _seed_n(seed_source["id"], 1, 1)
    iid = client.get("/inbox").json()["items"][0]["id"]
    assert client.get(f"/inbox/{iid}").json()["failures"] == []

    record_failures(1, "extract", [iid], "ForeignKeyViolation: org_id=9 no existe")
    failures = client.get(f"/inbox/{iid}").json()["failures"]
    assert len(failures) == 1
    assert failures[0]["stage"] == "extract"
    assert failures[0]["status"] == "failing"
    assert failures[0]["attempts"] == 1
    assert "org_id=9" in failures[0]["last_error"]


def test_inbox_neighbors_follow_feed_order_and_source_scope(
    client: Any, seed_source: dict[str, Any]
) -> None:
    """Anterior = el inmediatamente MÁS NUEVO, siguiente = el más antiguo (orden del feed:
    occurred_at DESC); en los extremos devuelve null; `source_id` acota a la fuente del filtro."""
    sid = seed_source["id"]
    _seed_n(sid, 1, 3)  # r0 10:00 < r1 10:01 < r2 10:02
    with connection() as c:
        other = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type) VALUES (1, 'otra', 'imap') RETURNING id"
            )
        ).scalar()
    assert isinstance(other, int)
    _seed_n(other, 1, 1, prefix="o")  # o0 10:00 en OTRA fuente (misma hora que r0)

    by_ext = {r["external_id"]: r["id"] for r in client.get("/inbox?limit=50").json()["items"]}
    # Sin filtro: el más antiguo de r1 es o0, no r0 — misma hora (10:00) y el desempate es por id
    # DESC (o0 se insertó después). Con el filtro de fuente, es r0.
    mid = client.get(f"/inbox/{by_ext['r1']}/neighbors").json()
    assert mid["prev"]["id"] == by_ext["r2"] and mid["next"]["id"] == by_ext["o0"]
    assert mid["prev"]["label"] == ""  # payload sin asunto/texto → rótulo vacío, no error
    mid_scoped = client.get(f"/inbox/{by_ext['r1']}/neighbors?source_id={sid}").json()
    assert mid_scoped["prev"]["id"] == by_ext["r2"] and mid_scoped["next"]["id"] == by_ext["r0"]

    newest = client.get(f"/inbox/{by_ext['r2']}/neighbors").json()
    assert newest["prev"] is None and newest["next"]["id"] == by_ext["r1"]

    # Sin filtro, el más antiguo de r0 puede ser o0 (misma hora, desempate por id); con el filtro de
    # la fuente del feed, no hay nada más antiguo en esa fuente.
    scoped = client.get(f"/inbox/{by_ext['r0']}/neighbors?source_id={sid}").json()
    assert scoped["next"] is None and scoped["prev"]["id"] == by_ext["r1"]

    assert client.get("/inbox/999999/neighbors").status_code == 404


def test_get_inbox_exposes_version_stamps(client: Any, seed_source: dict[str, Any]) -> None:
    """El detalle dice con qué versión del código se hizo cada etapa; lo no hecho (o anterior al
    sello) va en null."""
    from memex.core.inbox import insert_record
    from memex.core.source import SourceRecord
    from memex.version import code_version

    with connection() as c:
        r = insert_record(
            c,
            user_id=1,
            source_id=seed_source["id"],
            record=SourceRecord(
                external_id="ver-api",
                occurred_at=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
                payload={"subject": "sello"},
                dedupe_keys=[],
            ),
        )
    body = client.get(f"/inbox/{r.id}").json()
    assert body["versions"] == {
        "ingested": code_version(),
        "classified": None,
        "relevance": None,
        "extracted": None,
    }
