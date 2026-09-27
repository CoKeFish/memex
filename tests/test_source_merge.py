"""memex-sources merge: une dos fuentes del mismo tipo, conserva la copia vieja de cada mensaje
repetido, le suma los archivos de la nueva y borra el origen."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from memex.core.source_merge import MERGEABLE_TYPES, SourceMergeError, merge_sources
from memex.db import connection

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)


def _source(type_: str = "whatsapp", user_id: int = 1) -> int:
    with connection() as c:
        return int(
            c.execute(
                text("INSERT INTO sources (user_id, name, type) VALUES (:u, :n, :t) RETURNING id"),
                {"u": user_id, "n": f"merge-{uuid4().hex[:6]}", "t": type_},
            ).scalar_one()
        )


def _row(source_id: int, eid: str, *, processed: bool = False) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload, "
                    "processed_at) VALUES (1, :s, :e, :o, '{}'::jsonb, :p) RETURNING id"
                ),
                {"s": source_id, "e": eid, "o": NOW, "p": NOW if processed else None},
            ).scalar_one()
        )


def _asset(inbox_id: int, sha: str) -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO media_assets (user_id, inbox_id, sha256, object_key, bucket, "
                "content_type, size_bytes) VALUES (1, :i, :sha, :k, 'b', 'image/jpeg', 3)"
            ),
            {"i": inbox_id, "sha": sha, "k": f"k/{sha}"},
        )


def test_merge_keeps_old_copy_moves_media_and_deletes_source() -> None:
    old, new = _source(), _source()
    tag = uuid4().hex[:6]
    kept = _row(old, f"whatsapp:g@g.us:{tag}-A", processed=True)  # ya extraída, sin archivo
    dup = _row(new, f"whatsapp:g@g.us:{tag}-A")  # misma identidad, trae la foto
    _asset(dup, f"sha-{tag}-foto")
    only_old = _row(old, f"whatsapp:g@g.us:{tag}-B")
    only_new = _row(new, f"whatsapp:g@g.us:{tag}-C")
    same_both = _row(old, f"whatsapp:g@g.us:{tag}-D")
    dup2 = _row(new, f"whatsapp:g@g.us:{tag}-D")
    _asset(same_both, f"sha-{tag}-x")
    _asset(dup2, f"sha-{tag}-x")  # el mismo archivo en las dos copias: nada que sumar

    with connection() as c:
        dry = merge_sources(c, old, new, apply=False)
    assert (dry.duplicates, dry.media_moved, dry.requeued, dry.moved, dry.applied) == (
        2,
        1,
        1,
        1,
        False,
    )
    with connection() as c:  # dry-run no tocó nada
        assert (
            c.execute(text("SELECT count(*) FROM inbox WHERE source_id = :s"), {"s": old}).scalar()
            == 3
        )

    with connection() as c:
        stats = merge_sources(c, old, new, apply=True)
    assert (stats.duplicates, stats.media_moved, stats.requeued, stats.moved) == (2, 1, 1, 1)
    with connection() as c:
        assert c.execute(text("SELECT 1 FROM sources WHERE id = :s"), {"s": old}).first() is None
        rows = c.execute(
            text("SELECT id, source_id, processed_at FROM inbox WHERE id = ANY(:ids) ORDER BY id"),
            {"ids": [kept, dup, only_old, only_new, same_both, dup2]},
        ).all()
        by_id = {int(r[0]): (int(r[1]), r[2]) for r in rows}
        assert set(by_id) == {kept, only_old, only_new, same_both}  # las copias nuevas se borraron
        assert all(src == new for src, _ in by_id.values())
        assert by_id[kept][1] is None  # ganó la foto → de vuelta a la cola
        media = c.execute(
            text("SELECT inbox_id, sha256 FROM media_assets WHERE sha256 LIKE :p ORDER BY 1"),
            {"p": f"sha-{tag}-%"},
        ).all()
        assert [(int(i), s) for i, s in media] == [
            (kept, f"sha-{tag}-foto"),
            (same_both, f"sha-{tag}-x"),
        ]


def test_merge_refuses_bad_pairs() -> None:
    a = _source("whatsapp")
    with connection() as c:
        with pytest.raises(SourceMergeError):
            merge_sources(c, a, a, apply=False)
        with pytest.raises(SourceMergeError):
            merge_sources(c, a, _source("imap"), apply=False)
        with pytest.raises(SourceMergeError):
            merge_sources(c, _source("imap"), _source("imap"), apply=False)
        with pytest.raises(SourceMergeError):
            merge_sources(c, a, 999_999_999, apply=False)
    assert "imap" not in MERGEABLE_TYPES


def test_cli_main_reports_bad_pair_as_exit_2() -> None:
    from memex.core.source_merge import main

    a = _source("whatsapp")
    assert main(["merge", "--from", str(a), "--into", str(a)]) == 2
