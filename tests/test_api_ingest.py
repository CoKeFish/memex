from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection


def _body(source_id: int, eid: str = "e1") -> dict[str, Any]:
    return {
        "source_id": source_id,
        "external_id": eid,
        "occurred_at": "2026-05-23T10:00:00Z",
        "payload": {"hello": "world"},
        "dedupe_keys": [f"msgid:<{eid}@host>"],
    }


def test_ingest_dry_run_writes_nothing(client: Any, seed_source: dict[str, Any]) -> None:
    r = client.post("/ingest", headers={"X-Dry-Run": "1"}, json=_body(seed_source["id"]))
    assert r.status_code == 200
    body = r.json()
    assert body["would_insert"] is True
    assert body["validations"]["source_ownership"] == "ok"
    with connection() as c:
        count = c.execute(text("SELECT COUNT(*) FROM inbox")).scalar()
    assert count == 0


def test_ingest_dry_run_detects_duplicate(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    body = _body(sid, "dup1")
    assert client.post("/ingest", json=body).json()["inserted"] is True
    # Mismo external_id ya existe → el dry-run lo anticipa sin escribir.
    r = client.post("/ingest", headers={"X-Dry-Run": "1"}, json=body)
    assert r.status_code == 200
    out = r.json()
    assert out["would_insert"] is False
    assert out["reason"] == "duplicate"


def test_ingest_real_then_duplicate(client: Any, seed_source: dict[str, Any]) -> None:
    body = _body(seed_source["id"], "d1")
    r1 = client.post("/ingest", json=body)
    assert r1.status_code == 200
    assert r1.json()["inserted"] is True
    assert isinstance(r1.json()["id"], int)
    r2 = client.post("/ingest", json=body)
    assert r2.status_code == 200
    assert r2.json()["inserted"] is False
    assert r2.json()["reason"] == "duplicate"


def test_ingest_missing_source_is_404(client: Any) -> None:
    r = client.post("/ingest", json=_body(9999, "e"))
    assert r.status_code == 404


def test_ingest_cross_tenant_source_is_404(client: Any, seed_user2: int) -> None:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (:u, 's', 'x') RETURNING id"),
            {"u": seed_user2},
        ).scalar()
    assert isinstance(sid, int)
    r = client.post("/ingest", json=_body(sid, "e"))
    assert r.status_code == 404


def test_ingest_batch_mixed_outcomes(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    records = [
        _body(sid, "b1"),
        _body(sid, "b2"),
        _body(sid, "b1"),  # duplicate of first
        _body(99999, "x"),  # unknown source → error
    ]
    r = client.post("/ingest/batch", json={"records": records})
    assert r.status_code == 200
    assert r.json() == {"inserted": 2, "duplicates": 1, "errors": 1, "filtered": 0}


def test_ingest_push_de_correo_quita_el_relleno_invisible(
    client: Any, seed_source: dict[str, Any]
) -> None:
    # El plugin de Outlook manda el texto ya extraído, con las tiras de U+200C del preheader del
    # boletín; el parser IMAP las limpiaba y el push no (2026-09-15: 200 caracteres invisibles).
    sid = seed_source["id"]
    padded = "Preheader " + (chr(0x200C) + " ") * 50 + "Cuerpo real del correo"  # ZWNJ
    one = {**_body(sid, "pad1"), "payload": {"subject": "s", "body_text": padded}}
    batch = {**_body(sid, "pad2"), "payload": {"subject": "s", "body_text": padded}}
    assert client.post("/ingest", json=one).json()["inserted"] is True
    assert client.post("/ingest/batch", json={"records": [batch]}).json()["inserted"] == 1
    with connection() as c:
        bodies = (
            c.execute(
                text("SELECT payload->>'body_text' FROM inbox WHERE source_id = :s ORDER BY id"),
                {"s": sid},
            )
            .scalars()
            .all()
        )
    assert bodies == ["Preheader Cuerpo real del correo"] * 2
