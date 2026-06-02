"""Tests del backfill segmentado (endpoints /sources/{id}/backfill[...]).

Usa una `Source` stub (monkeypatch de `memex.sources.resolve`) que rinde `SourceRecord` fake — sin
IMAP real. Cubre: alta (range_end exclusivo), restaurar estado, avance de la frontera + insert,
reanudar tras recargar, advance-rest completa, idempotencia por dedup, dry-run sin efectos, override
del tamaño, cap_hit, y los 422/404 de borde.
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
        occurred_at=datetime(2026, 1, 20, 10, 0, tzinfo=UTC),
        payload={"subject": eid, "body_text": "x"},
        dedupe_keys=[f"msgid:<{eid}>"],
    )


class _StubSource:
    """Mínimo para `run_ingestor`: ignora la ventana y rinde los records dados."""

    checkpoint_schema = StubCursor

    def __init__(self, records: list[SourceRecord]) -> None:
        self._records = records

    def fetch(self, checkpoint: StubCursor) -> Iterable[SourceRecord]:
        yield from self._records

    def advance_checkpoint(self, checkpoint: StubCursor, last: SourceRecord) -> StubCursor:
        return StubCursor(seen=[*checkpoint.seen, last.external_id])


@pytest.fixture
def patch_source(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Instala una Source stub con los external_ids dados (sin tocar IMAP)."""

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


_CFG: dict[str, Any] = {
    "range_start": "2026-01-01",
    "range_end": "2026-03-31",  # inclusivo
    "window_unit": "month",
    "window_count": 1,
}


def test_configure_persists_frontier_and_exclusive_end(
    client: Any, seed_source: dict[str, Any]
) -> None:
    sid = seed_source["id"]
    r = client.post(f"/sources/{sid}/backfill", json=_CFG)
    assert r.status_code == 200
    body = r.json()
    assert body["frontier"] == "2026-01-01"  # frontera = inicio del rango
    assert body["range_end"] == "2026-03-31"  # vuelve inclusivo a la UI
    assert body["status"] == "active"
    assert body["progress_pct"] == 0.0
    # La DB guarda range_end EXCLUSIVO (+1 día).
    with connection() as c:
        re_db = c.execute(
            text("SELECT range_end FROM backfill_jobs WHERE source_id = :sid"), {"sid": sid}
        ).scalar()
    assert str(re_db) == "2026-04-01"


def test_get_restores_state(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    assert client.post(f"/sources/{sid}/backfill", json=_CFG).status_code == 200
    r = client.get(f"/sources/{sid}/backfill")
    assert r.status_code == 200
    assert r.json()["frontier"] == "2026-01-01"


def test_get_missing_is_404(client: Any, seed_source: dict[str, Any]) -> None:
    assert client.get(f"/sources/{seed_source['id']}/backfill").status_code == 404


def test_advance_moves_frontier_and_inserts(
    client: Any, seed_source: dict[str, Any], patch_source: Any
) -> None:
    sid = seed_source["id"]
    client.post(f"/sources/{sid}/backfill", json=_CFG)
    patch_source(["a", "b"])
    r = client.post(f"/sources/{sid}/backfill/advance")
    assert r.status_code == 200
    body = r.json()
    assert body["window"]["start"] == "2026-01-01"
    assert body["window"]["end"] == "2026-02-01"  # 1 mes, exclusivo
    assert body["window"]["inserted"] == 2
    assert body["state"]["frontier"] == "2026-02-01"
    assert len(body["state"]["history"]) == 1
    assert _inbox_count() == 2
    # Backfill NO mueve el cursor incremental.
    assert _checkpoint(sid) is None


def test_reload_after_advance_restores_frontier(
    client: Any, seed_source: dict[str, Any], patch_source: Any
) -> None:
    sid = seed_source["id"]
    client.post(f"/sources/{sid}/backfill", json=_CFG)
    patch_source(["a"])
    client.post(f"/sources/{sid}/backfill/advance")
    body = client.get(f"/sources/{sid}/backfill").json()  # "recargar"
    assert body["frontier"] == "2026-02-01"
    assert len(body["history"]) == 1


def test_advance_rest_completes(
    client: Any, seed_source: dict[str, Any], patch_source: Any
) -> None:
    sid = seed_source["id"]
    client.post(f"/sources/{sid}/backfill", json=_CFG)
    patch_source(["a", "b", "c"])
    r = client.post(f"/sources/{sid}/backfill/advance-rest")
    assert r.status_code == 200
    body = r.json()
    assert body["window"]["start"] == "2026-01-01"
    assert body["window"]["end"] == "2026-04-01"  # hasta el fin exclusivo
    assert body["state"]["status"] == "done"
    assert body["state"]["frontier"] == "2026-04-01"
    assert body["state"]["progress_pct"] == 100.0


def test_advance_when_done_is_noop(
    client: Any, seed_source: dict[str, Any], patch_source: Any
) -> None:
    sid = seed_source["id"]
    client.post(f"/sources/{sid}/backfill", json=_CFG)
    patch_source(["a"])
    client.post(f"/sources/{sid}/backfill/advance-rest")  # completa
    r = client.post(f"/sources/{sid}/backfill/advance")  # ya done → no-op
    assert r.status_code == 200
    body = r.json()
    assert body["window"] is None
    assert body["state"]["status"] == "done"


def test_advance_idempotent_reinsert(
    client: Any, seed_source: dict[str, Any], patch_source: Any
) -> None:
    sid = seed_source["id"]
    client.post(f"/sources/{sid}/backfill", json=_CFG)
    patch_source(["a", "b"])
    client.post(f"/sources/{sid}/backfill/advance")
    # Reconfigurar resetea la frontera; re-correr la misma ventana → dedup (todo duplicados).
    client.post(f"/sources/{sid}/backfill", json=_CFG)
    patch_source(["a", "b"])
    body = client.post(f"/sources/{sid}/backfill/advance").json()
    assert (body["window"]["inserted"], body["window"]["duplicates"]) == (0, 2)
    assert _inbox_count() == 2


def test_dry_run_does_not_move_frontier(
    client: Any, seed_source: dict[str, Any], patch_source: Any
) -> None:
    sid = seed_source["id"]
    client.post(f"/sources/{sid}/backfill", json=_CFG)
    patch_source(["a", "b"])
    r = client.post(f"/sources/{sid}/backfill/advance", params={"dry_run": True})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["window"]["inserted"] == 2  # contaría 2
    assert body["state"]["frontier"] == "2026-01-01"  # NO se movió
    assert body["state"]["history"] == []
    assert _inbox_count() == 0  # no escribió


def test_window_override_sticks(
    client: Any, seed_source: dict[str, Any], patch_source: Any
) -> None:
    sid = seed_source["id"]
    client.post(f"/sources/{sid}/backfill", json=_CFG)  # default 1 mes
    patch_source(["a"])
    body = client.post(
        f"/sources/{sid}/backfill/advance", json={"window_unit": "month", "window_count": 2}
    ).json()
    assert body["window"]["end"] == "2026-03-01"  # 2 meses
    assert (body["state"]["window_unit"], body["state"]["window_count"]) == ("month", 2)


def test_cap_hit_flag(client: Any, seed_source: dict[str, Any], patch_source: Any) -> None:
    sid = seed_source["id"]
    client.post(f"/sources/{sid}/backfill", json={**_CFG, "per_window_limit": 2})
    patch_source(["a", "b"])  # posted=2 >= limit 2 → cap_hit
    body = client.post(f"/sources/{sid}/backfill/advance").json()
    assert body["window"]["cap_hit"] is True


def test_non_imap_source_is_422(client: Any) -> None:
    with connection() as c:
        sid = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type) "
                "VALUES (1, 'tg', 'telegram') RETURNING id"
            )
        ).scalar()
    r = client.post(f"/sources/{sid}/backfill", json=_CFG)
    assert r.status_code == 422


def test_cross_tenant_is_404(client: Any, seed_user2: int) -> None:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (:u, 's', 'imap') RETURNING id"),
            {"u": seed_user2},
        ).scalar()
    r = client.post(f"/sources/{sid}/backfill", json=_CFG)
    assert r.status_code == 404


def test_invalid_range_is_422(client: Any, seed_source: dict[str, Any]) -> None:
    bad = {**_CFG, "range_start": "2026-03-31", "range_end": "2026-01-01"}
    r = client.post(f"/sources/{seed_source['id']}/backfill", json=bad)
    assert r.status_code == 422


def test_delete_removes_backfill(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    client.post(f"/sources/{sid}/backfill", json=_CFG)
    assert client.delete(f"/sources/{sid}/backfill").status_code == 204
    assert client.get(f"/sources/{sid}/backfill").status_code == 404
