"""Worker `run_pull` contra la DB de test con un proveedor FALSO (sin red).

Cubre el corazón del slice 1: upsert idempotente por `provider_event_id`, detección
created/modified/unchanged por `etag`, marca de cancelados, regla manual=alta-prioridad vs eco,
dedup FASE 1 + estado de procesamiento, paginación, y la observabilidad
(`mod_calendar_sync_runs` + `mod_calendar_event_changes`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any, ClassVar

import pytest
from sqlalchemy import text

from memex.core.source import HealthResult
from memex.db import connection
from memex.modules.calendar.providers.base import (
    ProviderEvent,
    ProviderEventRef,
    ProviderEventWrite,
    ProviderPage,
)
from memex.modules.calendar.sync import run_pull


class FakeProvider:
    """Devuelve páginas precargadas en orden; repite la última si se agota (para re-runs)."""

    name: ClassVar[str] = "google"

    def __init__(self, *pages: ProviderPage) -> None:
        self._pages = list(pages)
        self.calls = 0

    async def health_check(self) -> HealthResult:
        return HealthResult(status="healthy", detail="fake", checked_at=datetime.now(UTC))

    async def list_delta(
        self, *, sync_token: str | None = None, page_token: str | None = None
    ) -> ProviderPage:
        idx = min(self.calls, len(self._pages) - 1)
        self.calls += 1
        return self._pages[idx]

    async def create_event(self, ev: ProviderEventWrite) -> ProviderEventRef:
        raise NotImplementedError

    async def update_event(
        self, *, provider_event_id: str, etag: str | None, ev: ProviderEventWrite
    ) -> ProviderEventRef:
        raise NotImplementedError

    async def delete_event(self, *, provider_event_id: str, etag: str | None) -> None:
        raise NotImplementedError


def _ev(
    pid: str,
    title: str,
    *,
    starts_on: date = date(2026, 6, 3),
    start_time: time | None = None,
    etag: str = "e1",
    location: str = "",
    memex_consolidated_id: str | None = None,
    recurring_event_id: str | None = None,
) -> ProviderEvent:
    return ProviderEvent(
        provider_event_id=pid,
        title=title,
        starts_on=starts_on,
        start_time=start_time,
        etag=etag,
        location=location,
        memex_consolidated_id=memex_consolidated_id,
        recurring_event_id=recurring_event_id,
    )


def _seed_account(provider: str = "google", label: str = "me@gmail.com") -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    """
                    INSERT INTO mod_calendar_provider_accounts
                      (user_id, provider, account_label, calendar_id, token_path_env)
                    VALUES (1, :p, :l, 'primary', 'CAL_TOKEN_PATH')
                    RETURNING id
                    """
                ),
                {"p": provider, "l": label},
            ).scalar_one()
        )


def _events(account_id: int) -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(
                text(
                    "SELECT * FROM mod_calendar_events WHERE provider_account_id = :a ORDER BY id"
                ),
                {"a": account_id},
            )
            .mappings()
            .all()
        ]


def _sync_runs(account_id: int) -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(
                text(
                    "SELECT * FROM mod_calendar_sync_runs "
                    "WHERE provider_account_id = :a ORDER BY id"
                ),
                {"a": account_id},
            )
            .mappings()
            .all()
        ]


def _change_actions() -> list[str]:
    with connection() as c:
        return [
            str(r[0])
            for r in c.execute(
                text("SELECT action FROM mod_calendar_event_changes WHERE user_id = 1 ORDER BY id")
            ).all()
        ]


def _sync_token(account_id: int) -> str | None:
    with connection() as c:
        val = c.execute(
            text("SELECT sync_token FROM mod_calendar_provider_accounts WHERE id = :a"),
            {"a": account_id},
        ).scalar_one()
    return str(val) if val is not None else None


@pytest.mark.asyncio
async def test_pull_inserts_provider_events_and_records_run() -> None:
    aid = _seed_account()
    fake = FakeProvider(
        ProviderPage(events=(_ev("a", "Dentista"), _ev("b", "Gimnasio")), next_sync_token="T1")
    )

    stats = await run_pull(1, aid, client=fake)

    assert (stats.pulled, stats.created, stats.modified, stats.unchanged) == (2, 2, 0, 0)
    rows = _events(aid)
    assert len(rows) == 2
    assert all(r["origin"] == "provider" for r in rows)
    assert all(r["manual"] is True and r["priority_rank"] == 100 for r in rows)
    assert all(r["source_inbox_ids"] == [] for r in rows)
    assert _sync_token(aid) == "T1"

    runs = _sync_runs(aid)
    assert len(runs) == 1
    assert runs[0]["direction"] == "ingress"
    assert runs[0]["created"] == 2
    assert runs[0]["status"] == "ok"
    assert runs[0]["finished_at"] is not None
    assert _change_actions() == ["created", "created"]


@pytest.mark.asyncio
async def test_pull_is_idempotent_on_rerun() -> None:
    aid = _seed_account()
    fake = FakeProvider(
        ProviderPage(events=(_ev("a", "Dentista", etag="e1"),), next_sync_token="T1")
    )

    await run_pull(1, aid, client=fake)
    stats = await run_pull(1, aid, client=fake)  # mismo etag → unchanged

    assert (stats.created, stats.unchanged) == (0, 1)
    assert len(_events(aid)) == 1  # no duplicó


@pytest.mark.asyncio
async def test_pull_updates_on_etag_change() -> None:
    aid = _seed_account()
    await run_pull(1, aid, client=FakeProvider(ProviderPage(events=(_ev("a", "Cita", etag="e1"),))))
    stats = await run_pull(
        1, aid, client=FakeProvider(ProviderPage(events=(_ev("a", "Cita movida", etag="e2"),)))
    )

    assert (stats.created, stats.modified) == (0, 1)
    rows = _events(aid)
    assert len(rows) == 1
    assert rows[0]["title"] == "Cita movida"
    assert rows[0]["provider_etag"] == "e2"
    assert "modified" in _change_actions()


@pytest.mark.asyncio
async def test_pull_runs_dedup_and_marks_processing_outcome() -> None:
    aid = _seed_account()
    fake = FakeProvider(
        ProviderPage(
            events=(
                _ev("a", "Reunión equipo", start_time=time(10, 0), etag="a"),
                _ev("b", "Reunion equipo", start_time=time(10, 0), etag="b"),  # dup candidato
                _ev("c", "Almuerzo", starts_on=date(2026, 7, 1), etag="c"),  # único
            ),
            next_sync_token="T",
        )
    )

    stats = await run_pull(1, aid, client=fake)

    assert stats.dedup_pairs == 1
    by_pid = {r["provider_event_id"]: r for r in _events(aid)}
    assert by_pid["a"]["processing_outcome"] == "pending"
    assert by_pid["b"]["processing_outcome"] == "pending"
    assert by_pid["c"]["processing_outcome"] == "unique"
    assert all(r["processed_at"] is not None for r in by_pid.values())


@pytest.mark.asyncio
async def test_pull_marks_cancelled_as_deleted() -> None:
    aid = _seed_account()
    await run_pull(1, aid, client=FakeProvider(ProviderPage(events=(_ev("a", "X", etag="e1"),))))
    stats = await run_pull(1, aid, client=FakeProvider(ProviderPage(deleted_ids=("a",))))

    assert stats.deleted == 1
    rows = _events(aid)
    assert len(rows) == 1  # no se borra la fila, se marca
    assert rows[0]["provider_status"] == "cancelled"
    assert "deleted" in _change_actions()


@pytest.mark.asyncio
async def test_pull_echo_event_is_not_manual() -> None:
    aid = _seed_account()
    fake = FakeProvider(
        ProviderPage(events=(_ev("a", "Creado por memex", memex_consolidated_id="cons-1"),))
    )
    await run_pull(1, aid, client=fake)

    row = _events(aid)[0]
    assert row["manual"] is False
    assert row["priority_rank"] == 0


@pytest.mark.asyncio
async def test_pull_captures_recurring_event_id() -> None:
    aid = _seed_account()
    fake = FakeProvider(
        ProviderPage(
            events=(
                _ev("series-1_20260603T", "Cortar cabello", recurring_event_id="series-1"),
                _ev("oneoff", "Cita puntual"),  # no recurrente → NULL
            ),
            next_sync_token="T",
        )
    )

    await run_pull(1, aid, client=fake)

    by_pid = {r["provider_event_id"]: r for r in _events(aid)}
    assert by_pid["series-1_20260603T"]["recurring_event_id"] == "series-1"
    assert by_pid["oneoff"]["recurring_event_id"] is None


@pytest.mark.asyncio
async def test_pull_backfills_recurring_event_id_on_resync() -> None:
    # Evento ya en la DB con recurring_event_id NULL (sincronizado antes del feature). Un resync con
    # el MISMO etag pero recurringEventId presente debe ACTUALIZAR (backfill), no quedar unchanged.
    aid = _seed_account()
    await run_pull(
        1, aid, client=FakeProvider(ProviderPage(events=(_ev("a", "Clase", etag="e1"),)))
    )
    assert _events(aid)[0]["recurring_event_id"] is None

    stats = await run_pull(
        1,
        aid,
        client=FakeProvider(
            ProviderPage(events=(_ev("a", "Clase", etag="e1", recurring_event_id="serie-clase"),))
        ),
    )

    assert (stats.created, stats.modified, stats.unchanged) == (0, 1, 0)
    assert _events(aid)[0]["recurring_event_id"] == "serie-clase"


@pytest.mark.asyncio
async def test_pull_paginates_and_accumulates() -> None:
    aid = _seed_account()
    fake = FakeProvider(
        ProviderPage(events=(_ev("a", "A"),), next_page_token="P2"),
        ProviderPage(events=(_ev("b", "B"),), next_sync_token="FINAL"),
    )

    stats = await run_pull(1, aid, client=fake)

    assert stats.created == 2
    assert fake.calls == 2  # dos páginas consumidas en una corrida
    assert _sync_token(aid) == "FINAL"
