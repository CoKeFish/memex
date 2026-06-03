"""Worker `run_sync` contra la DB de test con un proveedor FALSO (sin red).

Cubre el corazón del slice 1: upsert idempotente por `provider_resource_name`, detección
created/modified/unchanged por `etag`, marca SUAVE de borrados, creación de org + asociación desde
`org_name`, paginación, y la observabilidad (`mod_identidades_sync_runs`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from sqlalchemy import text

from memex.core.source import HealthResult
from memex.db import connection
from memex.modules.identidades.providers.base import ProviderContact, ProviderContactsPage
from memex.modules.identidades.sync import run_sync


class FakeProvider:
    """Devuelve páginas precargadas en orden; repite la última si se agota (para re-runs)."""

    name: ClassVar[str] = "google"

    def __init__(self, *pages: ProviderContactsPage) -> None:
        self._pages = list(pages)
        self.calls = 0

    async def health_check(self) -> HealthResult:
        return HealthResult(status="healthy", detail="fake", checked_at=datetime.now(UTC))

    async def list_delta(
        self, *, sync_token: str | None = None, page_token: str | None = None
    ) -> ProviderContactsPage:
        idx = min(self.calls, len(self._pages) - 1)
        self.calls += 1
        return self._pages[idx]


def _pc(
    rn: str,
    display_name: str,
    *,
    etag: str = "e1",
    emails: Sequence[str] = (),
    phones: Sequence[str] = (),
    org_name: str | None = None,
    role: str | None = None,
    deleted: bool = False,
) -> ProviderContact:
    return ProviderContact(
        resource_name=rn,
        etag=etag,
        display_name=display_name,
        emails=tuple(emails),
        phones=tuple(phones),
        org_name=org_name,
        role=role,
        deleted=deleted,
    )


def _seed_account(provider: str = "google", label: str = "me@gmail.com") -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    """
                    INSERT INTO mod_identidades_provider_accounts (user_id, provider, account_label)
                    VALUES (1, :p, :l)
                    RETURNING id
                    """
                ),
                {"p": provider, "l": label},
            ).scalar_one()
        )


def _persons(account_id: int) -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(
                text(
                    "SELECT * FROM mod_identidades_persons "
                    "WHERE provider_account_id = :a ORDER BY id"
                ),
                {"a": account_id},
            )
            .mappings()
            .all()
        ]


def _orgs(user_id: int = 1) -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(
                text("SELECT * FROM mod_identidades_orgs WHERE user_id = :u ORDER BY id"),
                {"u": user_id},
            )
            .mappings()
            .all()
        ]


def _person_orgs(user_id: int = 1) -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(
                text("SELECT * FROM mod_identidades_person_orgs WHERE user_id = :u ORDER BY id"),
                {"u": user_id},
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
                    "SELECT * FROM mod_identidades_sync_runs "
                    "WHERE provider_account_id = :a ORDER BY id"
                ),
                {"a": account_id},
            )
            .mappings()
            .all()
        ]


def _sync_token(account_id: int) -> str | None:
    with connection() as c:
        val = c.execute(
            text("SELECT sync_token FROM mod_identidades_provider_accounts WHERE id = :a"),
            {"a": account_id},
        ).scalar_one()
    return str(val) if val is not None else None


@pytest.mark.asyncio
async def test_sync_inserts_persons_and_records_run() -> None:
    aid = _seed_account()
    fake = FakeProvider(
        ProviderContactsPage(
            contacts=(
                _pc("people/c1", "Ada Lovelace", emails=("ada@x.com",)),
                _pc("people/c2", "Alan Turing"),
            ),
            next_sync_token="T1",
        )
    )

    stats = await run_sync(1, aid, client=fake)

    assert (stats.pulled, stats.created, stats.modified, stats.unchanged) == (2, 2, 0, 0)
    rows = _persons(aid)
    assert len(rows) == 2
    assert all(r["source"] == "google_contacts" for r in rows)
    assert rows[0]["emails"] == ["ada@x.com"]
    assert _sync_token(aid) == "T1"

    runs = _sync_runs(aid)
    assert len(runs) == 1
    assert runs[0]["created"] == 2
    assert runs[0]["status"] == "ok"
    assert runs[0]["finished_at"] is not None


@pytest.mark.asyncio
async def test_sync_idempotent_on_rerun() -> None:
    aid = _seed_account()
    fake = FakeProvider(
        ProviderContactsPage(contacts=(_pc("people/c1", "Ada", etag="e1"),), next_sync_token="T1")
    )

    await run_sync(1, aid, client=fake)
    stats = await run_sync(1, aid, client=fake)  # mismo etag → unchanged

    assert (stats.created, stats.unchanged) == (0, 1)
    assert len(_persons(aid)) == 1  # no duplicó


@pytest.mark.asyncio
async def test_sync_updates_on_etag_change() -> None:
    aid = _seed_account()
    await run_sync(
        1,
        aid,
        client=FakeProvider(ProviderContactsPage(contacts=(_pc("people/c1", "Ada", etag="e1"),))),
    )
    stats = await run_sync(
        1,
        aid,
        client=FakeProvider(
            ProviderContactsPage(contacts=(_pc("people/c1", "Ada Byron", etag="e2"),))
        ),
    )

    assert (stats.created, stats.modified) == (0, 1)
    rows = _persons(aid)
    assert len(rows) == 1
    assert rows[0]["display_name"] == "Ada Byron"
    assert rows[0]["provider_etag"] == "e2"


@pytest.mark.asyncio
async def test_sync_creates_org_and_association() -> None:
    aid = _seed_account()
    fake = FakeProvider(
        ProviderContactsPage(
            contacts=(_pc("people/c1", "Dev Uno", org_name="Unity", role="Engineer"),),
            next_sync_token="T",
        )
    )

    await run_sync(1, aid, client=fake)

    orgs = _orgs()
    assert len(orgs) == 1
    assert orgs[0]["name"] == "Unity"
    assert orgs[0]["kind"] == "organizacion"
    assert orgs[0]["interest"] is False  # descubierta, no de la lista curada
    assert orgs[0]["source"] == "google_contacts"

    links = _person_orgs()
    assert len(links) == 1
    assert links[0]["org_id"] == orgs[0]["id"]
    assert links[0]["role"] == "Engineer"


@pytest.mark.asyncio
async def test_sync_marks_deleted_soft() -> None:
    aid = _seed_account()
    await run_sync(
        1,
        aid,
        client=FakeProvider(ProviderContactsPage(contacts=(_pc("people/c1", "Ada", etag="e1"),))),
    )
    stats = await run_sync(
        1,
        aid,
        client=FakeProvider(ProviderContactsPage(contacts=(_pc("people/c1", "", deleted=True),))),
    )

    assert stats.deleted == 1
    rows = _persons(aid)
    assert len(rows) == 1  # no se borra la fila, se marca
    assert rows[0]["metadata"].get("deleted") is True


@pytest.mark.asyncio
async def test_sync_paginates_and_accumulates() -> None:
    aid = _seed_account()
    fake = FakeProvider(
        ProviderContactsPage(contacts=(_pc("people/c1", "A"),), next_page_token="P2"),
        ProviderContactsPage(contacts=(_pc("people/c2", "B"),), next_sync_token="FINAL"),
    )

    stats = await run_sync(1, aid, client=fake)

    assert stats.created == 2
    assert fake.calls == 2  # dos páginas consumidas en una corrida
    assert _sync_token(aid) == "FINAL"
