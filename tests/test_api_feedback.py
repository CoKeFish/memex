"""Feedback rápido por mensaje + override manual de tier.

`POST /inbox/{id}/feedback` (captura), `GET /inbox/{id}` lo incluye, `GET /feedback` lista; y
`POST /inbox/{id}/classification` (override de tier aplicado ya).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from memex.core.inbox import insert_record
from memex.core.source import SourceRecord
from memex.db import connection


def _seed(source_id: int, ext: str = "r0") -> int:
    with connection() as c:
        insert_record(
            c,
            user_id=1,
            source_id=source_id,
            record=SourceRecord(
                external_id=ext,
                occurred_at=datetime(2026, 5, 23, 10, 0, tzinfo=UTC),
                payload={"subject": "factura", "body_text": "total $100"},
                dedupe_keys=[],
            ),
        )
        return int(
            c.execute(
                text("SELECT id FROM inbox WHERE external_id = :e AND user_id = 1"), {"e": ext}
            ).scalar_one()
        )


def test_feedback_capture_and_surface(client: Any, seed_source: dict[str, Any]) -> None:
    iid = _seed(seed_source["id"])
    r = client.post(
        f"/inbox/{iid}/feedback",
        json={"kinds": ["missing_data", "bad_ocr"], "note": "faltó el total"},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body["kinds"]) == {"missing_data", "bad_ocr"}
    assert body["note"] == "faltó el total"
    assert body["metadata"]["has_media"] is False  # snapshot de lo observado

    # aparece en el detalle del mensaje
    got = client.get(f"/inbox/{iid}").json()
    assert set(got["feedback"]["kinds"]) == {"missing_data", "bad_ocr"}

    # aparece en el listado
    items = client.get("/feedback").json()["items"]
    assert any(i["inbox_id"] == iid for i in items)

    # re-reportar reemplaza (upsert por inbox_id)
    r2 = client.post(f"/inbox/{iid}/feedback", json={"kinds": ["other"]})
    assert r2.json()["kinds"] == ["other"]


def test_feedback_invalid_kind_is_422(client: Any, seed_source: dict[str, Any]) -> None:
    iid = _seed(seed_source["id"])
    assert client.post(f"/inbox/{iid}/feedback", json={"kinds": ["nope"]}).status_code == 422
    assert client.post(f"/inbox/{iid}/feedback", json={"kinds": []}).status_code == 422


def test_feedback_unknown_inbox_is_404(client: Any) -> None:
    assert client.post("/inbox/999999/feedback", json={"kinds": ["other"]}).status_code == 404


def test_classification_manual_override(client: Any, seed_source: dict[str, Any]) -> None:
    iid = _seed(seed_source["id"])
    r = client.post(f"/inbox/{iid}/classification", json={"tier": "individual"})
    assert r.status_code == 200
    assert r.json()["tier"] == "individual" and r.json()["metadata"]["manual"] is True
    # reflejado en el detalle
    assert client.get(f"/inbox/{iid}").json()["classification"]["tier"] == "individual"


def test_classification_invalid_tier_is_422(client: Any, seed_source: dict[str, Any]) -> None:
    iid = _seed(seed_source["id"])
    assert client.post(f"/inbox/{iid}/classification", json={"tier": "nope"}).status_code == 422


def test_classification_unknown_inbox_is_404(client: Any) -> None:
    assert client.post("/inbox/999999/classification", json={"tier": "batch"}).status_code == 404
