"""Registros editables (notas): `insert_record(on_conflict="replace")` y el borde de ingesta.

Mismo external_id con contenido nuevo → payload reemplazado, `updated`, vuelve a la cola de
extracción (module_extractions borrado, processed_at NULL) y asuntos olvida su decisión. Contenido
idéntico → duplicate sin tocar nada. Fuentes que no son notas siguen con DO NOTHING.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Connection, text

from memex.api.ingest_service import ingest_records
from memex.api.schemas import IngestRequest
from memex.core.inbox import insert_record, invalidate_for_reprocess
from memex.core.source import SourceRecord


def _source(conn: Connection, type_: str) -> int:
    return int(
        conn.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id"),
            {"n": f"{type_}-test", "t": type_},
        ).scalar_one()
    )


def _note(version: str, edited: str) -> SourceRecord:
    return SourceRecord(
        external_id="notion:p1",
        occurred_at=datetime.fromisoformat(edited),
        payload={"provider": "notion", "page_id": "p1", "title": "Nota", "body_text": version},
        dedupe_keys=["notion:p1"],
    )


def test_replace_updates_payload_and_reports_updated(conn: Connection) -> None:
    sid = _source(conn, "notion")
    first = insert_record(
        conn, 1, sid, _note("v1", "2026-09-01T10:00:00+00:00"), on_conflict="replace"
    )
    assert first.inserted and first.id is not None
    same = insert_record(
        conn, 1, sid, _note("v1", "2026-09-01T10:00:00+00:00"), on_conflict="replace"
    )
    assert not same.inserted and same.reason == "duplicate"
    edited = insert_record(
        conn, 1, sid, _note("v2", "2026-09-02T10:00:00+00:00"), on_conflict="replace"
    )
    assert not edited.inserted and edited.reason == "updated" and edited.id == first.id
    row = conn.execute(
        text("SELECT payload->>'body_text' AS b, occurred_at FROM inbox WHERE id = :id"),
        {"id": first.id},
    ).one()
    assert row.b == "v2" and row.occurred_at == datetime(2026, 9, 2, 10, tzinfo=UTC)
    # Sin replace (correo etc.) el mismo caso es un duplicado y no toca nada.
    ignored = insert_record(conn, 1, sid, _note("v3", "2026-09-03T10:00:00+00:00"))
    assert ignored.reason == "duplicate"
    assert (
        conn.execute(
            text("SELECT payload->>'body_text' FROM inbox WHERE id = :id"), {"id": first.id}
        ).scalar()
        == "v2"
    )


def test_invalidate_for_reprocess_clears_stage_rows(conn: Connection) -> None:
    sid = _source(conn, "notion")
    r = insert_record(conn, 1, sid, _note("v1", "2026-09-01T10:00:00+00:00"))
    assert r.id is not None
    conn.execute(
        text(
            "INSERT INTO module_extractions (user_id, module_slug, inbox_id) "
            "VALUES (1, 'calendar', :i)"
        ),
        {"i": r.id},
    )
    conn.execute(
        text("UPDATE inbox SET processed_at = NOW(), attempts = 2 WHERE id = :i"), {"i": r.id}
    )
    assert invalidate_for_reprocess(conn, 1, [r.id]) == 1
    assert (
        conn.execute(
            text("SELECT COUNT(*) FROM module_extractions WHERE inbox_id = :i"), {"i": r.id}
        ).scalar()
        == 0
    )
    row = conn.execute(
        text("SELECT processed_at, attempts FROM inbox WHERE id = :i"), {"i": r.id}
    ).one()
    assert row.processed_at is None and row.attempts == 0


def test_ingest_records_replaces_only_for_notes(conn: Connection) -> None:
    notion = _source(conn, "notion")
    imap = _source(conn, "imap")
    req = IngestRequest(
        source_id=notion,
        external_id="notion:p9",
        occurred_at=datetime(2026, 9, 1, 10, tzinfo=UTC),
        payload={"provider": "notion", "page_id": "p9", "title": "T", "body_text": "v1"},
        dedupe_keys=["notion:p9"],
    )
    mail = IngestRequest(
        source_id=imap,
        external_id="imap:s:1:1",
        occurred_at=datetime(2026, 9, 1, 10, tzinfo=UTC),
        payload={"subject": "a", "body_text": "v1", "from": {"email": "x@y.z"}},
        dedupe_keys=[],
    )
    out = ingest_records(conn, 1, [req, mail])
    assert (out["inserted"], out["updated"], out["duplicates"]) == (2, 0, 0)
    inbox_id = conn.execute(
        text("SELECT id FROM inbox WHERE external_id = 'notion:p9'")
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO module_extractions (user_id, module_slug, inbox_id) "
            "VALUES (1, 'calendar', :i)"
        ),
        {"i": inbox_id},
    )
    conn.execute(
        text("INSERT INTO mod_asuntos_seen (user_id, inbox_id) VALUES (1, :i)"), {"i": inbox_id}
    )
    edited = req.model_copy(update={"payload": {**req.payload, "body_text": "v2"}})
    mail2 = mail.model_copy(update={"payload": {**mail.payload, "body_text": "v2"}})
    out = ingest_records(conn, 1, [edited, mail2])
    assert (out["inserted"], out["updated"], out["duplicates"]) == (0, 1, 1)
    assert (
        conn.execute(
            text("SELECT payload->>'body_text' FROM inbox WHERE id = :i"), {"i": inbox_id}
        ).scalar()
        == "v2"
    )
    assert (
        conn.execute(
            text("SELECT COUNT(*) FROM module_extractions WHERE inbox_id = :i"), {"i": inbox_id}
        ).scalar()
        == 0
    )
    assert (
        conn.execute(
            text("SELECT COUNT(*) FROM mod_asuntos_seen WHERE inbox_id = :i"), {"i": inbox_id}
        ).scalar()
        == 0
    )
    # El correo con cuerpo distinto NO se reemplaza (sigue v1).
    assert (
        conn.execute(
            text("SELECT payload->>'body_text' FROM inbox WHERE external_id = 'imap:s:1:1'")
        ).scalar()
        == "v1"
    )
