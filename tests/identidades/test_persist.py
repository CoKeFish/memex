"""`IdentidadesModule.persist`: dedup contra el directorio. Lo conocido se ata; lo NUEVO entra al
directorio como no-interés (source='extraction', method='created') con su evidencia."""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import LLMClient
from memex.modules.contract import ModuleContext
from memex.modules.identidades.module import IdentidadesModule
from memex.modules.identidades.schema import IdentityItem


def _seed_known() -> tuple[int, int]:
    """Ada (persona conocida, interés) + Unity (org de interés). Devuelve (person_id, org_id)."""
    with connection() as c:
        pid = c.execute(
            text(
                "INSERT INTO mod_identidades_persons "
                "(user_id, display_name, emails, source, interest) VALUES "
                "(1, 'Ada Lovelace', ARRAY['ada@x.com'], 'google_contacts', TRUE) RETURNING id"
            )
        ).scalar_one()
        oid = c.execute(
            text(
                "INSERT INTO mod_identidades_orgs (user_id, name, source, interest) "
                "VALUES (1, 'Unity', 'manual', TRUE) RETURNING id"
            )
        ).scalar_one()
    return int(pid), int(oid)


def _mentions() -> dict[str, dict[str, Any]]:
    with connection() as c:
        rows = (
            c.execute(text("SELECT * FROM mod_identidades_mentions WHERE user_id=1 ORDER BY id"))
            .mappings()
            .all()
        )
    return {str(r["mentioned_name"]): dict(r) for r in rows}


def _persons() -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(
                text("SELECT * FROM mod_identidades_persons WHERE user_id=1 ORDER BY id")
            )
            .mappings()
            .all()
        ]


def _orgs() -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(
                text("SELECT * FROM mod_identidades_orgs WHERE user_id=1 ORDER BY id")
            )
            .mappings()
            .all()
        ]


async def _persist(items: list[IdentityItem]) -> int:
    mod = IdentidadesModule()
    with connection() as conn:
        ctx = ModuleContext(
            user_id=1,
            conn=conn,
            llm=cast(LLMClient, None),  # persist no usa el LLM (dedup determinista)
            deps={},
            summary_id=None,
            inbox_ids=(5, 6),
        )
        return await mod.persist(ctx, items)


@pytest.mark.asyncio
async def test_known_resolves_unknown_enters_directory() -> None:
    pid, oid = _seed_known()
    items = [
        IdentityItem(source_inbox_ids=(5,), name="Ada L.", email="ada@x.com", evidence="con Ada"),
        IdentityItem(source_inbox_ids=(5,), name="Unity", kind="organizacion"),
        IdentityItem(source_inbox_ids=(6,), name="Globex S.A.", kind="organizacion"),
        IdentityItem(source_inbox_ids=(6,), name="Juan Perez", kind="persona"),
    ]
    assert await _persist(items) == 4

    m = _mentions()
    # conocidas → atadas
    assert (m["Ada L."]["resolved_kind"], m["Ada L."]["resolved_person_id"]) == ("person", pid)
    assert m["Ada L."]["resolution_method"] == "email"
    assert (m["Unity"]["resolved_kind"], m["Unity"]["resolved_org_id"]) == ("org", oid)
    # nuevas → creadas (method='created') y atadas a su propia ficha
    assert m["Globex S.A."]["resolution_method"] == "created"
    assert m["Globex S.A."]["resolved_kind"] == "org"
    assert m["Juan Perez"]["resolution_method"] == "created"
    assert m["Juan Perez"]["resolved_kind"] == "person"

    # el directorio CRECIÓ con lo detectado, en no-interés / source=extraction
    new_org = next(o for o in _orgs() if o["name"] == "Globex S.A.")
    assert new_org["interest"] is False and new_org["source"] == "extraction"
    new_person = next(p for p in _persons() if p["display_name"] == "Juan Perez")
    assert new_person["interest"] is False and new_person["source"] == "extraction"
    assert m["Juan Perez"]["resolved_person_id"] == new_person["id"]
    # las conocidas NO se duplicaron
    assert sum(1 for p in _persons() if p["display_name"] == "Ada Lovelace") == 1


@pytest.mark.asyncio
async def test_dedup_within_batch_creates_once() -> None:
    items = [
        IdentityItem(source_inbox_ids=(5,), name="Globex", kind="organizacion"),
        IdentityItem(source_inbox_ids=(6,), name="globex", kind="organizacion"),  # otra grafía
    ]
    await _persist(items)
    assert sum(1 for o in _orgs() if o["name"].lower() == "globex") == 1  # una sola ficha


@pytest.mark.asyncio
async def test_persist_empty_is_noop() -> None:
    assert await _persist([]) == 0
    assert _mentions() == {}
