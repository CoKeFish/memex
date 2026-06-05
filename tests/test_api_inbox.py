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
