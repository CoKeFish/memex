"""Tests del módulo hackathones: schema HackathonItem, registry, Protocol y persist (DB)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from memex.core.source import SourceKind
from memex.llm import ChatMessage, LLMResult, ResponseFormat
from memex.modules import known_modules, resolve
from memex.modules.contract import CAP_EXTRACT, ExtractionItem, InterestModule, ModuleContext
from memex.modules.hackathones.module import HackathonModule
from memex.modules.hackathones.schema import HackathonItem

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


# ----- schema -------------------------------------------------------------------- #


def test_hackathon_item_valid_with_coercion() -> None:
    h = HackathonItem(
        source_inbox_ids=[7],  # coerción list → tuple
        name="NASA Space Apps 2026",
        starts_on="2026-10-03",  # coerción str → date
        modality="Online",  # se normaliza a 'online'
        prizes="USD 5000",
        evidence="Inscribite al NASA Space Apps",
    )
    assert h.source_inbox_ids == (7,)
    assert h.starts_on == date(2026, 10, 3)
    assert h.modality == "online"


def test_hackathon_item_dates_optional() -> None:
    h = HackathonItem(source_inbox_ids=(1,), name="Hack X")
    assert h.starts_on is None
    assert h.registration_deadline is None
    assert h.modality == "desconocido"
    assert h.location == ""


def test_hackathon_modality_out_of_list_defaults() -> None:
    h = HackathonItem(source_inbox_ids=(1,), name="Hack", modality="remoto")
    assert h.modality == "desconocido"


def test_hackathon_item_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        HackathonItem(
            source_inbox_ids=(1,),
            name="Hack",
            premio="1000",  # type: ignore[call-arg]  # extra prohibido
        )


def test_hackathon_item_is_extraction_item() -> None:
    assert issubclass(HackathonItem, ExtractionItem)


# ----- registry ------------------------------------------------------------------ #


def test_known_modules_includes_hackathones() -> None:
    assert "hackathones" in known_modules()


def test_resolve_hackathones_builds_module() -> None:
    module = resolve("hackathones")()
    assert isinstance(module, HackathonModule)


# ----- disciplina de Protocol ---------------------------------------------------- #


def test_hackathones_satisfies_interest_module() -> None:
    """HackathonModule debe satisfacer estructuralmente el Protocol InterestModule."""
    assert isinstance(HackathonModule(), InterestModule)


def test_hackathones_capabilities_and_kinds() -> None:
    assert CAP_EXTRACT in HackathonModule.capabilities
    assert HackathonModule.consumes_kinds == frozenset(
        {SourceKind.EMAIL, SourceKind.CHAT, SourceKind.SOCIAL}
    )
    assert HackathonModule.depends_on == ()


# ----- persist (DB) -------------------------------------------------------------- #


class _NoLLM:
    """Satisface `LLMClient`; `persist` no toca el LLM, así que `complete` nunca debe llamarse."""

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        raise AssertionError("persist no debe llamar al LLM")


def test_persist_inserts_rows(conn: Connection) -> None:
    module = HackathonModule()
    ctx = ModuleContext(
        user_id=1, conn=conn, llm=_NoLLM(), deps={}, summary_id=None, inbox_ids=(1,)
    )
    item = HackathonItem(
        source_inbox_ids=(1,),
        name="HackBogotá 2026",
        starts_on=date(2026, 9, 12),
        ends_on=date(2026, 9, 14),
        modality="presencial",
        location="Bogotá",
        prizes="COP 5.000.000",
        technologies="IA, web",
        evidence="HackBogotá 2026 en la Javeriana",
    )
    n = asyncio.run(module.persist(ctx, [item]))
    assert n == 1
    row = (
        conn.execute(
            text(
                "SELECT name, starts_on, ends_on, modality, location, prizes, technologies, "
                "source_inbox_ids FROM mod_hackathones_events WHERE user_id = 1"
            )
        )
        .mappings()
        .one()
    )
    assert row["name"] == "HackBogotá 2026"
    assert row["modality"] == "presencial"
    assert row["source_inbox_ids"] == [1]


def test_persist_empty_is_noop(conn: Connection) -> None:
    module = HackathonModule()
    ctx = ModuleContext(user_id=1, conn=conn, llm=_NoLLM(), deps={}, summary_id=None, inbox_ids=())
    assert asyncio.run(module.persist(ctx, [])) == 0
