"""Tests del endpoint POST /sources/{id}/fetch (fetch a demanda in-process).

Usa una `Source` stub (monkeypatch de `memex.sources.resolve`) que rinde `SourceRecord`
fake — sin tocar IMAP real. Cubre: contadores reales vs dry-run, que el dry-run NO avanza
el checkpoint y la corrida real SÍ, dedup contra el inbox real, y los 422/404 de borde.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import text

from memex.core.source import SourceRecord
from memex.db import connection


class StubCursor(BaseModel):
    seen: list[str] = []


def _record(eid: str) -> SourceRecord:
    return SourceRecord(
        external_id=eid,
        occurred_at=datetime(2026, 5, 23, 10, 0, tzinfo=UTC),
        payload={"subject": eid, "body_text": "x"},
        dedupe_keys=[f"msgid:<{eid}>"],
    )


class _StubSource:
    """Mínimo para `run_ingestor`: checkpoint_schema + fetch + advance_checkpoint."""

    checkpoint_schema = StubCursor

    def __init__(self, records: list[SourceRecord]) -> None:
        self._records = records

    def fetch(self, checkpoint: StubCursor) -> Iterable[SourceRecord]:
        yield from self._records

    def advance_checkpoint(self, checkpoint: StubCursor, last: SourceRecord) -> StubCursor:
        return StubCursor(seen=[*checkpoint.seen, last.external_id])


@pytest.fixture
def patch_source(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Devuelve un helper que instala una Source stub con los external_ids dados."""

    def install(eids: list[str]) -> None:
        stub = _StubSource([_record(e) for e in eids])
        monkeypatch.setattr("memex.sources.resolve", lambda _t: lambda _cfg, env=None: stub)

    return install


def _checkpoint(source_id: int) -> dict[str, Any] | None:
    with connection() as c:
        row = c.execute(
            text("SELECT cursor FROM source_checkpoints WHERE source_id = :sid"),
            {"sid": source_id},
        ).first()
    return dict(row[0]) if row else None


def _inbox_count() -> int:
    with connection() as c:
        return int(c.execute(text("SELECT COUNT(*) FROM inbox")).scalar() or 0)


def test_fetch_real_inserts_and_advances_checkpoint(
    client: Any, seed_source: dict[str, Any], patch_source: Any
) -> None:
    sid = seed_source["id"]
    patch_source(["r1", "r2", "r3"])
    r = client.post(f"/sources/{sid}/fetch")
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is False
    assert (body["posted"], body["inserted"], body["duplicates"], body["filtered"]) == (3, 3, 0, 0)
    assert _inbox_count() == 3
    # La corrida real SÍ persiste el checkpoint.
    assert _checkpoint(sid) is not None
    # Quedó registrada en ingestion_runs como ok.
    with connection() as c:
        ok = c.execute(
            text("SELECT COUNT(*) FROM ingestion_runs WHERE source_id = :sid AND status = 'ok'"),
            {"sid": sid},
        ).scalar()
    assert ok and ok >= 1


def test_fetch_dry_run_counts_without_writing(
    client: Any, seed_source: dict[str, Any], patch_source: Any
) -> None:
    sid = seed_source["id"]
    # Pre-insertar r1 para que el dry-run lo reporte como duplicado.
    patch_source(["r1"])
    assert client.post(f"/sources/{sid}/fetch").json()["inserted"] == 1
    before = _inbox_count()
    ckpt_before = _checkpoint(sid)

    # Dry-run: r1 ya existe, r2/r3 son nuevos.
    patch_source(["r1", "r2", "r3"])
    r = client.post(f"/sources/{sid}/fetch", params={"dry_run": True})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert (body["posted"], body["inserted"], body["duplicates"], body["filtered"]) == (3, 2, 1, 0)
    # No escribió nada nuevo ni movió el checkpoint.
    assert _inbox_count() == before
    assert _checkpoint(sid) == ckpt_before


def test_fetch_real_then_repeat_is_all_duplicates(
    client: Any, seed_source: dict[str, Any], patch_source: Any
) -> None:
    sid = seed_source["id"]
    patch_source(["a", "b"])
    assert client.post(f"/sources/{sid}/fetch").json()["inserted"] == 2
    # Segunda corrida con los mismos records → dedup por UNIQUE(source_id, external_id).
    patch_source(["a", "b"])
    body = client.post(f"/sources/{sid}/fetch").json()
    assert (body["inserted"], body["duplicates"]) == (0, 2)
    assert _inbox_count() == 2


def test_fetch_invalid_mode_is_422(client: Any, seed_source: dict[str, Any]) -> None:
    r = client.post(f"/sources/{seed_source['id']}/fetch", params={"mode": "bogus"})
    assert r.status_code == 422


def test_fetch_range_requires_since(client: Any, seed_source: dict[str, Any]) -> None:
    r = client.post(f"/sources/{seed_source['id']}/fetch", params={"mode": "range"})
    assert r.status_code == 422


def test_fetch_last_inserts_without_advancing_checkpoint(
    client: Any, seed_source: dict[str, Any], patch_source: Any
) -> None:
    sid = seed_source["id"]
    patch_source(["x1", "x2"])
    r = client.post(f"/sources/{sid}/fetch", params={"mode": "last", "limit": 5})
    assert r.status_code == 200
    assert r.json()["inserted"] == 2
    assert _inbox_count() == 2
    # backfill ad-hoc: NO mueve el cursor incremental
    assert _checkpoint(sid) is None


def test_fetch_range_inserts_without_advancing_checkpoint(
    client: Any, seed_source: dict[str, Any], patch_source: Any
) -> None:
    sid = seed_source["id"]
    patch_source(["r1", "r2", "r3"])
    r = client.post(f"/sources/{sid}/fetch", params={"mode": "range", "since": "2026-01-01"})
    assert r.status_code == 200
    assert r.json()["inserted"] == 3
    assert _checkpoint(sid) is None


def test_fetch_unknown_source_type_is_422(client: Any) -> None:
    with connection() as c:
        sid = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type) VALUES (1, 'weird', 'nope') RETURNING id"
            )
        ).scalar()
    r = client.post(f"/sources/{sid}/fetch")
    assert r.status_code == 422


def test_fetch_cross_tenant_is_404(client: Any, seed_user2: int) -> None:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (:u, 's', 'imap') RETURNING id"),
            {"u": seed_user2},
        ).scalar()
    r = client.post(f"/sources/{sid}/fetch")
    assert r.status_code == 404
