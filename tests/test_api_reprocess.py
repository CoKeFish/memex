"""POST /inbox/{id}/reprocess — re-aplica etapas a un mensaje.

Se prueba con la etapa `classify` (determinista, sin LLM/IMAP/OCR): ejercita el endpoint + el
orquestador + el delegado de clasificación end-to-end. Las etapas externas (media/ocr/summarize/
extract) van con fakes en sus tests unitarios. Cubre: classify crea la fila; force re-clasifica;
stage inválida → 422; id ajeno/inexistente → 404.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from memex.core.inbox import insert_record
from memex.core.source import SourceRecord
from memex.db import connection


def _seed_inbox(source_id: int, ext: str = "r0") -> int:
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


def test_reprocess_classify(client: Any, seed_source: dict[str, Any]) -> None:
    iid = _seed_inbox(seed_source["id"])
    r = client.post(f"/inbox/{iid}/reprocess", json={"stages": ["classify"], "force": False})
    assert r.status_code == 200
    body = r.json()
    assert body["targets"] == 1
    assert body["stages"] == ["classify"]
    assert body["results"]["classify"]["classified"] == 1
    # Paridad con el worker standalone: el desglose por tier viaja en los resultados.
    by_tier = body["results"]["classify"]["by_tier"]
    assert sum(by_tier.values()) == 1

    # 2da vez sin force: ya estaba clasificado.
    again = client.post(f"/inbox/{iid}/reprocess", json={"stages": ["classify"]})
    assert again.json()["results"]["classify"]["already"] == 1

    # Con force: re-clasifica (borra la fila previa y vuelve a insertar).
    forced = client.post(f"/inbox/{iid}/reprocess", json={"stages": ["classify"], "force": True})
    assert forced.json()["results"]["classify"]["classified"] == 1


def test_reprocess_classify_emits_tier_breakdown_event(
    client: Any, seed_source: dict[str, Any], sink_capture: Any
) -> None:
    """`reprocess.classify.done`: el camino de lote también cuenta en /logs cuántos mensajes
    cayeron a cada tier (antes solo quedaba el total dentro de reprocess.done)."""
    import json

    iid = _seed_inbox(seed_source["id"], ext="r-tiers")
    r = client.post(f"/inbox/{iid}/reprocess", json={"stages": ["classify"], "force": False})
    assert r.status_code == 200

    records = []
    while not sink_capture.empty():
        records.append(sink_capture.get_nowait())
    done = [rec for rec in records if rec["event"] == "reprocess.classify.done"]
    assert len(done) == 1
    fields = json.loads(done[0]["fields"])
    assert fields["n"] == 1
    assert fields["classified"] == 1
    tier_counts = {k: v for k, v in fields.items() if k.startswith("tier_")}
    assert sum(tier_counts.values()) == 1  # un (1) mensaje cayó a exactamente un tier
    assert done[0]["inbox_id"] == iid  # heredado del bind del endpoint individual


def test_reprocess_invalid_stage_is_422(client: Any, seed_source: dict[str, Any]) -> None:
    iid = _seed_inbox(seed_source["id"])
    r = client.post(f"/inbox/{iid}/reprocess", json={"stages": ["nope"]})
    assert r.status_code == 422


def test_reprocess_unknown_inbox_is_404(client: Any) -> None:
    r = client.post("/inbox/999999/reprocess", json={"stages": ["classify"]})
    assert r.status_code == 404
