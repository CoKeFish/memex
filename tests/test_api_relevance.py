"""HTTP de /relevance: settings, intereses, reglas (dry run en la alta manual) y revisión."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.relevance import VerdictItem, insert_verdicts


def _seed_email(seed_source: dict[str, Any], ext: str, *, sender: str, subject: str) -> int:
    payload = {"from": {"email": sender}, "subject": subject, "body_text": f"cuerpo {ext}"}
    with connection() as c:
        iid = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (1, :sid, :eid, :occ, CAST(:p AS JSONB)) RETURNING id
                """
            ),
            {
                "sid": seed_source["id"],
                "eid": ext,
                "occ": datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
                "p": json.dumps(payload),
            },
        ).scalar()
        c.execute(
            text("INSERT INTO classifications (user_id, inbox_id, tier) VALUES (1, :iid, 'batch')"),
            {"iid": iid},
        )
    assert iid is not None
    return int(iid)


def test_settings_default_and_patch(client: Any) -> None:
    r = client.get("/relevance/settings")
    assert r.status_code == 200
    assert r.json() == {
        "enabled": False,
        "mode": "per_window",
        "model": "claude-opus-4-8",
        "mining_min_messages": 5,
        "provider": "anthropic",
        "codex_model": None,
    }

    p = client.patch("/relevance/settings", json={"enabled": True})
    assert p.status_code == 200 and p.json()["enabled"] is True
    p = client.patch("/relevance/settings", json={"mode": "per_message"})
    body = p.json()
    assert body["enabled"] is True and body["mode"] == "per_message"  # patch parcial
    p = client.patch("/relevance/settings", json={"mining_min_messages": 3})
    assert p.json()["mining_min_messages"] == 3 and p.json()["mode"] == "per_message"
    p = client.patch("/relevance/settings", json={"provider": "codex", "codex_model": "gpt-5.1"})
    assert p.json()["provider"] == "codex" and p.json()["codex_model"] == "gpt-5.1"
    p = client.patch("/relevance/settings", json={"codex_model": ""})
    assert p.json()["codex_model"] is None and p.json()["provider"] == "codex"  # "" limpia
    assert client.patch("/relevance/settings", json={"provider": "openai"}).status_code == 422
    assert client.patch("/relevance/settings", json={"mode": "nope"}).status_code == 422
    assert client.patch("/relevance/settings", json={"mining_min_messages": 0}).status_code == 422


def test_interests_crud(client: Any) -> None:
    r = client.post("/relevance/interests", json={"text": "descuentos de Steam"})
    assert r.status_code == 201
    iid = r.json()["id"]
    assert (
        client.post("/relevance/interests", json={"text": "descuentos de Steam"}).status_code == 409
    )
    assert client.post("/relevance/interests", json={"text": "  "}).status_code == 422

    items = client.get("/relevance/interests").json()["items"]
    assert [i["text"] for i in items] == ["descuentos de Steam"]

    p = client.patch(f"/relevance/interests/{iid}", json={"enabled": False})
    assert p.status_code == 200 and p.json()["enabled"] is False

    assert client.delete(f"/relevance/interests/{iid}").status_code == 204
    assert client.delete(f"/relevance/interests/{iid}").status_code == 404
    assert client.patch("/relevance/interests/999999", json={"enabled": True}).status_code == 404


def test_rules_manual_dry_run_and_toggle(client: Any, seed_source: dict[str, Any]) -> None:
    relevant = _seed_email(seed_source, "m1", sender="alertas@bank.com", subject="Pago")
    with connection() as c:
        insert_verdicts(c, 1, [VerdictItem(relevant, "relevant", "llm")])

    # Una regla que atraparía al relevante NO se persiste: 422 con el reporte
    bad = client.post("/relevance/rules", json={"kind": "sender_domain", "pattern": "bank.com"})
    assert bad.status_code == 422
    detail = bad.json()["detail"]
    assert detail["matched_relevant"] == 1 and detail["relevant_sample_ids"] == [relevant]
    assert client.get("/relevance/rules").json()["items"] == []

    # Una regla limpia se activa
    ok = client.post(
        "/relevance/rules",
        json={"kind": "sender_domain", "pattern": "spam.io", "rationale": "puro ruido"},
    )
    assert ok.status_code == 201
    rule = ok.json()
    assert rule["status"] == "active" and rule["proposed_by"] == "manual"
    assert rule["dry_run_report"]["passes"] is True
    assert (
        client.post(
            "/relevance/rules", json={"kind": "sender_domain", "pattern": "spam.io"}
        ).status_code
        == 409
    )

    # Toggle reversible
    rid = rule["id"]
    p = client.patch(f"/relevance/rules/{rid}", json={"status": "disabled"})
    assert p.status_code == 200 and p.json()["status"] == "disabled"
    assert client.patch(f"/relevance/rules/{rid}", json={"status": "active"}).status_code == 200
    assert client.patch("/relevance/rules/999999", json={"status": "disabled"}).status_code == 404

    # Filtro por status
    assert client.get("/relevance/rules?status=active").json()["items"][0]["id"] == rid
    assert client.get("/relevance/rules?status=rejected").json()["items"] == []


def test_review_queue_and_resolve(client: Any, seed_source: dict[str, Any]) -> None:
    iid = _seed_email(seed_source, "m1", sender="x@y.com", subject="¿impuestos?")
    with connection() as c:
        insert_verdicts(c, 1, [VerdictItem(iid, "insufficient", "llm", reason="ambiguo")])

    items = client.get("/relevance/review").json()["items"]
    assert len(items) == 1
    assert items[0]["inbox_id"] == iid and items[0]["subject"] == "¿impuestos?"
    assert items[0]["reason"] == "ambiguo"

    r = client.post(
        f"/relevance/review/{iid}/resolve", json={"is_relevant": True, "reason": "es del banco"}
    )
    assert r.status_code == 204
    assert client.get("/relevance/review").json()["items"] == []
    # mark + veredicto actualizados en la misma resolución
    with connection() as c:
        mark = c.execute(
            text("SELECT is_relevant FROM relevance_marks WHERE inbox_id = :i"), {"i": iid}
        ).scalar()
        verdict = c.execute(
            text("SELECT verdict, method FROM relevance_verdicts WHERE inbox_id = :i"), {"i": iid}
        ).first()
    assert mark is True
    assert verdict is not None and (verdict[0], verdict[1]) == ("relevant", "manual")
    # segunda resolución: ya no hay insufficient pendiente
    assert (
        client.post(f"/relevance/review/{iid}/resolve", json={"is_relevant": False}).status_code
        == 404
    )


def test_mine_requires_gate_on_and_noops_when_empty(client: Any) -> None:
    assert client.post("/relevance/rules/mine").status_code == 422  # gate apagado
    client.patch("/relevance/settings", json={"enabled": True})
    r = client.post("/relevance/rules/mine")
    assert r.status_code == 200
    body = r.json()
    assert body["senders"] == 0 and body["proposed"] == 0  # sin no-relevantes → no-op sin LLM
