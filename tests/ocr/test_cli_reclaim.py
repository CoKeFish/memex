"""`memex-ocr reclaim-pdfs`: marca skipped→pending SOLO los PDFs (no toca ok ni no-PDFs)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.ocr.cli import main


def _new_source() -> int:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (1, 's', 'imap') RETURNING id")
        ).scalar()
    assert sid is not None
    return int(sid)


def _seed_media(source_id: int, ext: str, sha: str, content_type: str, ocr_status: str) -> int:
    with connection() as c:
        iid = c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :s, :e, NOW(), CAST('{}' AS JSONB)) RETURNING id"
            ),
            {"s": source_id, "e": ext},
        ).scalar()
        mid = c.execute(
            text(
                "INSERT INTO media_assets "
                "(user_id, inbox_id, sha256, object_key, bucket, content_type, size_bytes, "
                " filename, ocr_status) "
                "VALUES (1, :iid, :sha, :key, 'b', :ct, 100, 'f', :st) RETURNING id"
            ),
            {"iid": iid, "sha": sha, "key": f"media/1/{sha}", "ct": content_type, "st": ocr_status},
        ).scalar()
    assert mid is not None
    return int(mid)


def _status(mid: int) -> str:
    with connection() as c:
        return str(
            c.execute(
                text("SELECT ocr_status FROM media_assets WHERE id = :i"), {"i": mid}
            ).scalar()
        )


def test_reclaim_flips_only_skipped_pdfs(capsys: pytest.CaptureFixture[str]) -> None:
    sid = _new_source()
    pdf_skipped = _seed_media(sid, "a", "sha-a", "application/pdf", "skipped")
    pdf_ok = _seed_media(sid, "b", "sha-b", "application/pdf", "ok")  # filtro por estado
    img_skipped = _seed_media(sid, "c", "sha-c", "image/png", "skipped")  # filtro por content_type

    rc = main(["reclaim-pdfs", "--user", "1"])

    assert rc == 0
    assert _status(pdf_skipped) == "pending"  # único que cambia
    assert _status(pdf_ok) == "ok"  # no se toca un PDF ya terminal
    assert _status(img_skipped) == "skipped"  # no-PDF intacto
    assert "1 PDF" in capsys.readouterr().out
