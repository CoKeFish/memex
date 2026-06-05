"""Invariante de cobertura de la traza: todo módulo de extracción que MATERIALIZA filas debe
emitir ≥1 nodo `entity` (la traza refleja qué le pasó a cada fila del mensaje).

Parametrizado sobre los módulos con `CAP_EXTRACT` del registro (`known_modules`). Un módulo
`CAP_EXTRACT` sin item de muestra acá hace FALLAR el test a propósito: agregar un extractor obliga
a cubrir su traza. La aserción es `≥1` (no "exactamente N") porque identidades emite una entidad
por MENCIÓN resuelta —que puede mapear a una identidad preexistente—, no 1:1 con filas insertadas
(ver el docstring de `TraceNode.entity`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from memex.core.trace import create_root, open_module_tracer
from memex.db import connection
from memex.llm import ChatMessage, LLMResult, ResponseFormat
from memex.modules import known_modules, resolve
from memex.modules.calendar.schema import CalendarEventItem
from memex.modules.contract import CAP_EXTRACT, ExtractionItem, ModuleContext
from memex.modules.finance.schema import TransactionItem
from memex.modules.hackathones.schema import HackathonItem
from memex.modules.identidades.schema import IdentityItem

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


class _NoLLM:
    """Satisface `LLMClient`; `persist`/`dedup` no tocan el LLM (la FASE 2 corre aparte)."""

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        raise AssertionError("persist/dedup no debe llamar al LLM")


def _seed_inbox(ext: str) -> int:
    """Crea source + inbox (commiteado) para que el módulo y la traza los vean."""
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, 'imap') RETURNING id"),
            {"n": ext},
        ).scalar_one()
        iid = c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :sid, :ext, :occ, CAST('{}' AS JSONB)) RETURNING id"
            ),
            {"sid": sid, "ext": ext, "occ": datetime(2026, 6, 3, 12, 0, tzinfo=UTC)},
        ).scalar_one()
    return int(iid)


#: Un item válido de muestra por módulo de extracción. La clave es el `slug`. Agregar un extractor
#: nuevo y NO registrarlo acá rompe `test_extract_module_with_sample_exists` a propósito.
_SAMPLE_ITEMS: dict[str, Callable[[int], ExtractionItem]] = {
    "finance": lambda iid: TransactionItem(
        source_inbox_ids=(iid,),
        amount=Decimal("100.00"),
        currency="USD",
        occurred_on=date(2026, 6, 3),
        evidence="pago de 100 USD",
    ),
    "calendar": lambda iid: CalendarEventItem(
        source_inbox_ids=(iid,),
        title="Examen de Análisis",
        starts_on=date(2026, 6, 3),
        evidence="el examen es el 3/6",
    ),
    "hackathones": lambda iid: HackathonItem(
        source_inbox_ids=(iid,),
        name="HackBogotá 2026",
        starts_on=date(2026, 9, 12),
        evidence="HackBogotá 2026",
    ),
    "identidades": lambda iid: IdentityItem(
        source_inbox_ids=(iid,),
        name="Acme Inc",
        kind="organizacion",
        evidence="Acme Inc nos escribió",
    ),
}


def _extract_slugs() -> list[str]:
    """Slugs de los módulos registrados que declaran `CAP_EXTRACT`."""
    return [s for s in known_modules() if CAP_EXTRACT in resolve(s)().capabilities]


def test_extract_module_with_sample_exists() -> None:
    """Todo módulo `CAP_EXTRACT` del registro debe tener un item de muestra en este invariante."""
    missing = [s for s in _extract_slugs() if s not in _SAMPLE_ITEMS]
    assert not missing, f"módulos CAP_EXTRACT sin item de muestra en el invariante: {missing}"


@pytest.mark.parametrize("slug", _extract_slugs())
def test_extract_module_emits_entity_when_persisting(conn: Connection, slug: str) -> None:
    """Tras `persist` con un item válido, si el módulo materializó filas (`>0`) debe haber al menos
    un nodo `entity` en la traza del mensaje. Detecta módulos que persisten sin instrumentar."""
    make_item = _SAMPLE_ITEMS.get(slug)
    assert make_item is not None, f"falta item de muestra para {slug}"

    iid = _seed_inbox(ext=f"inv-{slug}")
    root = create_root(conn, user_id=1, inbox_id=iid, label="msg")
    tracer = open_module_tracer(
        conn, user_id=1, inbox_id=iid, root_id=root, slug=slug, label=slug, seq=0
    )
    ctx = ModuleContext(
        user_id=1,
        conn=conn,
        llm=_NoLLM(),
        deps={},
        summary_id=None,
        inbox_ids=(iid,),
        trace=tracer,
    )
    persisted = asyncio.run(resolve(slug)().persist(ctx, [make_item(iid)]))
    assert persisted > 0, f"{slug}.persist no materializó filas con un item válido"

    entity_count = conn.execute(
        text("SELECT count(*) FROM trace_nodes WHERE inbox_id = :i AND kind = 'entity'"),
        {"i": iid},
    ).scalar_one()
    assert entity_count >= 1, f"{slug} persistió filas pero no emitió ningún nodo entity"
