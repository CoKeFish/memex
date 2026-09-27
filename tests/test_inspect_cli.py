"""`memex-inspect`: la inspección de solo lectura debe reflejar lo persistido para un mensaje
(tier, veredicto, extracción pendiente, fallas) y listar lo más nuevo primero."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from memex.core.deadletter import record_failures
from memex.core.inbox import insert_record
from memex.core.source import SourceRecord
from memex.db import connection
from memex.inspect_cli import (
    fetch_recent,
    main,
    message_report,
    render_message,
    render_recent,
    run_report,
)


def _seed(source_id: int, ext: str, when: datetime, subject: str) -> None:
    with connection() as c:
        insert_record(
            c,
            user_id=1,
            source_id=source_id,
            record=SourceRecord(
                external_id=ext,
                occurred_at=when,
                payload={"subject": subject, "from": {"email": "a@x.io"}, "body_text": "hola"},
                dedupe_keys=[],
            ),
        )


def test_recent_and_message_reflect_persisted_state(
    client: Any, seed_source: dict[str, Any], capsys: Any
) -> None:
    sid = seed_source["id"]
    _seed(sid, "old", datetime(2026, 8, 1, 10, 0, tzinfo=UTC), "viejo")
    _seed(sid, "new", datetime(2026, 9, 1, 10, 0, tzinfo=UTC), "Factura de septiembre")
    by_ext = {r["external_id"]: r["id"] for r in client.get("/inbox").json()["items"]}
    new_id = by_ext["new"]

    # Clasificación determinista vía API (sin LLM) + una falla registrada de extracción.
    assert client.post(f"/inbox/{new_id}/process").status_code == 200
    record_failures(1, "extract", [new_id], "boom: FK org_id=9")

    rows = fetch_recent(1, 5)
    assert [r["id"] for r in rows] == [new_id, by_ext["old"]]  # más nuevo primero
    assert rows[0]["tier"] in ("batch", "individual", "blacklist")
    assert rows[0]["failures"] == 1 and rows[0]["extracted"] is False
    assert "Factura de septiembre" in render_recent(rows)

    rep = message_report(1, new_id)
    assert rep is not None
    assert rep["verdict"] is None and rep["extraction"]["done"] is False
    assert rep["failures"][0]["stage"] == "extract"
    txt = render_message(rep)
    assert "gate: sin veredicto" in txt and "extracción: pendiente" in txt
    assert "FALLA extract" in txt and "FK org_id=9" in txt

    assert message_report(1, 999_999) is None
    assert run_report(1, 999_999) is None

    # Punto de entrada: `message` con --json deja el objeto como ÚLTIMA línea.
    assert main(["--json", "message", str(new_id)]) == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("{") and '"trace_nodes"' in last
    assert main(["message", "999999"]) == 1
