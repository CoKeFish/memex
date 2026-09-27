"""Gate R3 en el work-set de extracción de módulos (load_module_workset).

Un mensaje con OCR no-terminal (pending / error reintentable) NO entra al work-set de extracción
hasta que su media esté en estado terminal (ok / skipped). Espeja el gate de relations/summary.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.finance.module import FinanceModule
from memex.modules.workset import load_module_workset


def _seed_classified(source_id: int, ext: str) -> int:
    with connection() as c:
        iid = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (1, :sid, :eid, :occ, CAST(:p AS JSONB)) RETURNING id
                """
            ),
            {
                "sid": source_id,
                "eid": ext,
                "occ": datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
                "p": '{"body_text": "cuerpo"}',
            },
        ).scalar()
        c.execute(
            text(
                "INSERT INTO classifications (user_id, inbox_id, tier) VALUES (1, :i, 'individual')"
            ),
            {"i": iid},
        )
    assert iid is not None
    return int(iid)


def _seed_media(inbox_id: int, *, status: str, sha: str = "s1") -> None:
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO media_assets
                  (user_id, inbox_id, sha256, object_key, bucket, content_type, size_bytes,
                   filename, ocr_status, ocr_text)
                VALUES (1, :i, :sha, :k, 'b', 'image/png', 10, 'f', :st, :txt)
                """
            ),
            {
                "i": inbox_id,
                "sha": sha,
                "k": f"media/1/{sha}",
                "st": status,
                "txt": "GASTO $50" if status == "ok" else None,
            },
        )


def test_pending_ocr_gates_module_workset(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]  # 'imap' = SourceKind.EMAIL, que finance consume
    iid = _seed_classified(sid, "m1")
    _seed_media(iid, status="pending")

    with connection() as c:
        rows = load_module_workset(c, 1, source_id=None, modules=[FinanceModule()], limit=100)
    assert rows == []  # gateado por OCR pendiente

    with connection() as c:
        c.execute(
            text("UPDATE media_assets SET ocr_status='ok', ocr_text='GASTO $50' WHERE inbox_id=:i"),
            {"i": iid},
        )
        rows2 = load_module_workset(c, 1, source_id=None, modules=[FinanceModule()], limit=100)
    assert len(rows2) == 1
    assert rows2[0].ocr_text == "GASTO $50"  # el texto OCR viaja al WorkRow


def test_skipped_media_does_not_gate_module_workset(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    iid = _seed_classified(sid, "m1")
    _seed_media(iid, status="skipped")
    with connection() as c:
        rows = load_module_workset(c, 1, source_id=None, modules=[FinanceModule()], limit=100)
    assert len(rows) == 1  # 'skipped' es terminal → no bloquea


def test_outlook_source_enters_module_workset() -> None:
    """Regresión: los correos de Outlook (type='outlook') entran al work-set como email, igual
    que imap. Antes 'outlook' no tenía SourceKind registrado y se salteaban en silencio."""
    with connection() as c:
        sid = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type) "
                "VALUES (1, 'outlook-test', 'outlook') RETURNING id"
            )
        ).scalar()
    assert sid is not None
    iid = _seed_classified(int(sid), "ol1")
    with connection() as c:
        rows = load_module_workset(c, 1, source_id=None, modules=[FinanceModule()], limit=100)
    assert any(r.inbox_id == iid for r in rows)
