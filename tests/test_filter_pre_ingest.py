"""Integration: filter pre-ingest aplica en /ingest/batch + /gateway/.../ingest.

Inserta una filter_rule en la DB de test, dispara records al API, verifica
que los matcheados NO aparecen en `inbox` y los que pasan SÍ.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from memex.db import connection


def _insert_rule(
    *,
    user_id: int = 1,
    source_type: str | None = "imap",
    source_id: int | None = None,
    scope: dict[str, Any] | None = None,
    action: str = "ignore",
    priority: int = 100,
    enabled: bool = True,
) -> int:
    """Insert a filter_rule with its own committed transaction so the API sees it."""
    with connection() as c:
        new_id = c.execute(
            text(
                """
                INSERT INTO filter_rules
                    (user_id, source_type, source_id, scope, action, priority, enabled)
                VALUES (:uid, :stype, :sid, CAST(:scope AS JSONB), :action, :prio, :enabled)
                RETURNING id
                """
            ),
            {
                "uid": user_id,
                "stype": source_type,
                "sid": source_id,
                "scope": json.dumps(scope or {}),
                "action": action,
                "prio": priority,
                "enabled": enabled,
            },
        ).scalar()
    assert new_id is not None
    return int(new_id)


def _disable_rule(rule_id: int) -> None:
    with connection() as c:
        c.execute(
            text("UPDATE filter_rules SET enabled = FALSE WHERE id = :id"),
            {"id": rule_id},
        )


def _ingest_record(source_id: int, external_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "external_id": external_id,
        "occurred_at": "2026-05-28T00:00:00+00:00",
        "payload": payload,
        "dedupe_keys": [],
    }


# ---------- /ingest/batch ---------- #


def test_ingest_batch_drops_records_matching_ignore_rule(
    client: Any, conn: Any, seed_source: dict[str, Any]
) -> None:
    sid = seed_source["id"]
    _insert_rule(
        source_type="imap",
        scope={"from": {"equals": "spam@x.com"}},
        action="ignore",
        priority=200,
    )
    resp = client.post(
        "/ingest/batch",
        json={
            "records": [
                _ingest_record(sid, "e1", {"from": "spam@x.com", "subject": "junk"}),
                _ingest_record(sid, "e2", {"from": "ok@x.com", "subject": "fine"}),
                _ingest_record(sid, "e3", {"from": "spam@x.com", "subject": "junk2"}),
            ]
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["inserted"] == 1
    assert data["duplicates"] == 0
    assert data["errors"] == 0

    # Verify only the non-spam record landed in inbox.
    rows = (
        conn.execute(
            text("SELECT external_id FROM inbox WHERE source_id = :sid ORDER BY external_id"),
            {"sid": sid},
        )
        .scalars()
        .all()
    )
    assert list(rows) == ["e2"]


def test_ingest_batch_keep_rule_does_not_drop(
    client: Any, conn: Any, seed_source: dict[str, Any]
) -> None:
    sid = seed_source["id"]
    _insert_rule(
        scope={"from": {"equals": "vip@x.com"}},
        action="keep",
        priority=300,
    )
    _insert_rule(
        scope={"from": {"equals": "vip@x.com"}},
        action="ignore",
        priority=100,
    )
    resp = client.post(
        "/ingest/batch",
        json={"records": [_ingest_record(sid, "e1", {"from": "vip@x.com"})]},
    )
    assert resp.status_code == 200
    assert resp.json()["inserted"] == 1


def test_ingest_batch_disabled_rule_is_skipped(
    client: Any, conn: Any, seed_source: dict[str, Any]
) -> None:
    sid = seed_source["id"]
    rule_id = _insert_rule(
        scope={"from": {"equals": "spam@x.com"}},
        action="ignore",
    )
    _disable_rule(rule_id)
    resp = client.post(
        "/ingest/batch",
        json={"records": [_ingest_record(sid, "e1", {"from": "spam@x.com"})]},
    )
    assert resp.status_code == 200
    assert resp.json()["inserted"] == 1


def test_ingest_batch_source_type_null_rule_applies_to_any_source(
    client: Any, conn: Any, seed_source: dict[str, Any]
) -> None:
    """A rule with source_type=NULL applies to all source_types for that user."""
    sid = seed_source["id"]
    _insert_rule(
        source_type=None,  # global per-user
        scope={"from": {"equals": "spam@x.com"}},
        action="ignore",
    )
    resp = client.post(
        "/ingest/batch",
        json={"records": [_ingest_record(sid, "e1", {"from": "spam@x.com"})]},
    )
    assert resp.status_code == 200
    assert resp.json()["inserted"] == 0


# ---------- /gateway/plugins/.../ingest ---------- #


def _ensure_gateway_plugin(client: Any, plugin: str, source_type: str = "imap") -> int:
    """Creates the source via /gateway/plugins/<plugin>/state and returns source_id."""
    resp = client.post(
        f"/gateway/plugins/{plugin}/state",
        json={"source_type": source_type},
    )
    assert resp.status_code == 200
    return int(resp.json()["source_id"])


def test_gateway_ingest_drops_records_matching_ignore_rule(client: Any, conn: Any) -> None:
    sid = _ensure_gateway_plugin(client, "p-filter-test")
    _insert_rule(
        source_id=sid,
        scope={"from": {"equals": "spam@x.com"}},
        action="ignore",
    )
    resp = client.post(
        "/gateway/plugins/p-filter-test/ingest",
        json={
            "records": [
                {
                    "external_id": "e1",
                    "occurred_at": "2026-05-28T00:00:00+00:00",
                    "payload": {"from": "spam@x.com"},
                    "dedupe_keys": [],
                },
                {
                    "external_id": "e2",
                    "occurred_at": "2026-05-28T00:00:00+00:00",
                    "payload": {"from": "ok@x.com"},
                    "dedupe_keys": [],
                },
            ]
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["inserted"] == 1
    assert data["duplicates"] == 0
    assert data["errors"] == 0

    rows = (
        conn.execute(
            text("SELECT external_id FROM inbox WHERE source_id = :sid"),
            {"sid": sid},
        )
        .scalars()
        .all()
    )
    assert list(rows) == ["e2"]


# ---------- /ingest (single) ---------- #


def test_ingest_one_returns_filtered_when_dropped(
    client: Any, conn: Any, seed_source: dict[str, Any]
) -> None:
    sid = seed_source["id"]
    _insert_rule(
        scope={"from": {"equals": "spam@x.com"}},
        action="ignore",
    )
    resp = client.post(
        "/ingest",
        json=_ingest_record(sid, "single", {"from": "spam@x.com"}),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["inserted"] is False
    assert data["reason"] == "filtered"
    # And no row in inbox.
    n = conn.execute(
        text("SELECT count(*) FROM inbox WHERE source_id = :sid"),
        {"sid": sid},
    ).scalar()
    assert n == 0
