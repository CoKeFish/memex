"""GET /inbox/{id}/window — el lote de procesamiento de un mensaje (sin LLM, contra DB sembrada).

Cubre los tres modos (`summary` / `prospective` / `none`), el corte por gap, el aislamiento
por fuente y por tenant, y el shape de fila de lista en `members`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from memex.db import connection

_BASE = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)


def _seed(
    source_id: int,
    ext: str,
    tier: str | None,
    *,
    minutes: int = 0,
    user_id: int = 1,
    payload: dict[str, Any] | None = None,
) -> int:
    """Inbox (+ clasificación si `tier`) en BASE+minutes. Devuelve el id."""
    with connection() as c:
        iid = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (:uid, :sid, :eid, :occ, CAST(:p AS JSONB)) RETURNING id
                """
            ),
            {
                "uid": user_id,
                "sid": source_id,
                "eid": ext,
                "occ": _BASE + timedelta(minutes=minutes),
                "p": json.dumps(payload or {"body_text": f"msg {ext}"}),
            },
        ).scalar_one()
        if tier is not None:
            c.execute(
                text(
                    "INSERT INTO classifications (user_id, inbox_id, tier) "
                    "VALUES (:uid, :iid, :tier)"
                ),
                {"uid": user_id, "iid": iid, "tier": tier},
            )
    return int(iid)


def _seed_summary(inbox_ids: list[int], *, tier: str = "batch") -> int:
    with connection() as c:
        sid = c.execute(
            text(
                "INSERT INTO summaries (user_id, tier, content) "
                "VALUES (1, :tier, 'RESUMEN DEL LOTE') RETURNING id"
            ),
            {"tier": tier},
        ).scalar_one()
        c.execute(
            text("INSERT INTO summary_inbox_links (summary_id, inbox_id) VALUES (:s, :i)"),
            [{"s": int(sid), "i": i} for i in inbox_ids],
        )
    return int(sid)


def _seed_source2() -> int:
    with connection() as c:
        sid = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type) "
                "VALUES (1, 'imap-test-2', 'imap') RETURNING id"
            )
        ).scalar_one()
    return int(sid)


def test_prospective_groups_contiguous_batch(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    a = _seed(sid, "a", "batch", minutes=0)
    b = _seed(sid, "b", "batch", minutes=5)
    c = _seed(sid, "c", "batch", minutes=10)

    r = client.get(f"/inbox/{b}/window")
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "prospective"
    assert data["summary_id"] is None
    assert [m["id"] for m in data["members"]] == [a, b, c]  # orden conversacional
    # Shape de fila de lista: tier + avance del pipeline presentes.
    first = data["members"][0]
    assert first["classification"]["tier"] == "batch"
    assert first["summarized"] is False
    assert "payload" in first


def test_prospective_gap_splits_windows(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    a = _seed(sid, "a", "batch", minutes=0)
    b = _seed(sid, "b", "batch", minutes=7 * 60)  # gap > 6h → ventana aparte

    ra = client.get(f"/inbox/{a}/window").json()
    rb = client.get(f"/inbox/{b}/window").json()
    assert [m["id"] for m in ra["members"]] == [a]
    assert [m["id"] for m in rb["members"]] == [b]


def test_summary_mode_returns_co_members(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    a = _seed(sid, "a", "batch", minutes=0)
    b = _seed(sid, "b", "batch", minutes=5)
    c = _seed(sid, "c", "batch", minutes=10)  # vecino SIN resumir: no debe colarse
    summary_id = _seed_summary([a, b])

    r = client.get(f"/inbox/{a}/window")
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "summary"
    assert data["summary_id"] == summary_id
    assert [m["id"] for m in data["members"]] == [a, b]
    assert all(m["summarized"] for m in data["members"])
    # El vecino sin resumir sigue viendo su ventana prospectiva (ya sin a/b, que salieron
    # del work-set al quedar resumidos).
    rc = client.get(f"/inbox/{c}/window").json()
    assert rc["mode"] == "prospective"
    assert [m["id"] for m in rc["members"]] == [c]


def test_individual_without_summary_is_window_of_one(
    client: Any, seed_source: dict[str, Any]
) -> None:
    iid = _seed(seed_source["id"], "solo", "individual")
    data = client.get(f"/inbox/{iid}/window").json()
    assert data["mode"] == "prospective"
    assert [m["id"] for m in data["members"]] == [iid]


def test_blacklist_and_unclassified_have_no_window(
    client: Any, seed_source: dict[str, Any]
) -> None:
    sid = seed_source["id"]
    bl = _seed(sid, "bl", "blacklist")
    raw = _seed(sid, "raw", None)
    for iid in (bl, raw):
        data = client.get(f"/inbox/{iid}/window").json()
        assert data["mode"] == "none"
        assert data["members"] == []
        assert data["summary_id"] is None


def test_prospective_scoped_to_own_source(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    sid2 = _seed_source2()
    a = _seed(sid, "a", "batch", minutes=0)
    _seed(sid2, "x", "batch", minutes=2)  # intercalado de OTRA fuente
    b = _seed(sid, "b", "batch", minutes=5)

    data = client.get(f"/inbox/{a}/window").json()
    assert [m["id"] for m in data["members"]] == [a, b]
    assert all(m["source_id"] == sid for m in data["members"])


def test_cross_tenant_and_missing_are_404(
    client: Any, seed_source: dict[str, Any], seed_user2: int
) -> None:
    foreign = _seed(seed_source["id"], "ajeno", "batch", user_id=seed_user2)
    assert client.get(f"/inbox/{foreign}/window").status_code == 404
    assert client.get("/inbox/999999/window").status_code == 404
