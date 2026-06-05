"""`FinanceModule.dedup` contra la DB: inserta crudas, resuelve la fecha (recepción si falta), marca
pares FASE 1 (incl. auto-confirm procedimental) y el round-trip de `forget_inbox`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, ResponseFormat
from memex.modules.contract import ModuleContext
from memex.modules.finance.module import FinanceModule
from memex.modules.finance.schema import TransactionItem
from memex.modules.identidades.module import IdentidadesModule

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


class _NoLLM:
    """Satisface `LLMClient`; `dedup` no toca el LLM, así que `complete` nunca debe llamarse."""

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        raise AssertionError("dedup no debe llamar al LLM")


def _ctx(conn: Connection, inbox_ids: tuple[int, ...] = ()) -> ModuleContext:
    return ModuleContext(
        user_id=1, conn=conn, llm=_NoLLM(), deps={}, summary_id=None, inbox_ids=inbox_ids
    )


def _seed_inbox(occurred_at: datetime, *, ext: str) -> int:
    """Crea source + inbox con `occurred_at` (recepción), commiteado para que el módulo lo vea."""
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
            {"sid": sid, "ext": ext, "occ": occurred_at},
        ).scalar_one()
    return int(iid)


def _item(**over: object) -> TransactionItem:
    base: dict[str, object] = {
        "source_inbox_ids": (1,),
        "direction": "egreso",
        "amount": "100.00",
        "currency": "USD",
        "counterparty": "Rappi",
    }
    base.update(over)
    return TransactionItem(**base)


def test_persist_inserts_datetime_precision(conn: Connection) -> None:
    n = asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (1,)),
            [_item(occurred_on=date(2026, 6, 3), occurred_time=time(14, 30), place="Calle 1")],
        )
    )
    assert n == 1
    row = (
        conn.execute(
            text(
                "SELECT direction, amount, currency, counterparty, place, occurred_at_precision, "
                "processing_outcome FROM mod_finance_transactions WHERE user_id = 1"
            )
        )
        .mappings()
        .one()
    )
    assert row["direction"] == "egreso"
    assert row["amount"] == Decimal("100.00")
    assert row["counterparty"] == "Rappi"
    assert row["place"] == "Calle 1"
    assert row["occurred_at_precision"] == "datetime"
    assert row["processing_outcome"] == "unique"  # sin par


def test_persist_date_only_precision(conn: Connection) -> None:
    asyncio.run(FinanceModule().dedup(_ctx(conn, (1,)), [_item(occurred_on=date(2026, 6, 3))]))
    prec = conn.execute(
        text("SELECT occurred_at_precision FROM mod_finance_transactions")
    ).scalar_one()
    assert prec == "date"


def test_persist_infers_reception_date(conn: Connection) -> None:
    reception = datetime(2026, 5, 31, 9, 15, tzinfo=UTC)
    iid = _seed_inbox(reception, ext="recibo-sin-fecha")
    asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (iid,)), [_item(source_inbox_ids=(iid,), occurred_on=None)]
        )
    )
    row = (
        conn.execute(
            text("SELECT occurred_at, occurred_at_precision FROM mod_finance_transactions")
        )
        .mappings()
        .one()
    )
    assert row["occurred_at_precision"] == "inferred"
    assert row["occurred_at"] == reception


def test_persist_marks_procedural_confirmed_pair(conn: Connection) -> None:
    # dos copias del mismo cargo (misma hora + contraparte + lugar) → confirmado procedimentalmente.
    common = {"occurred_on": date(2026, 6, 3), "occurred_time": time(14, 30), "place": "Calle 1"}
    asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (1, 2)),
            [
                _item(source_inbox_ids=(1,), **common),
                _item(source_inbox_ids=(2,), **common),
            ],
        )
    )
    cand = (
        conn.execute(
            text("SELECT status, decided_by FROM mod_finance_dedup_candidates WHERE user_id = 1")
        )
        .mappings()
        .one()
    )
    assert cand["status"] == "confirmed"
    assert cand["decided_by"] == "procedural"
    outcomes = {
        r[0]
        for r in conn.execute(
            text("SELECT DISTINCT processing_outcome FROM mod_finance_transactions")
        ).all()
    }
    assert outcomes == {"pending"}  # ambos en un par → pending hasta consolidar


def test_forget_inbox_removes_orphan(conn: Connection) -> None:
    module = FinanceModule()
    asyncio.run(module.dedup(_ctx(conn, (5,)), [_item(source_inbox_ids=(5,))]))
    assert conn.execute(text("SELECT count(*) FROM mod_finance_transactions")).scalar_one() == 1
    deleted = module.forget_inbox(conn, 1, [5])
    assert deleted == 1
    assert conn.execute(text("SELECT count(*) FROM mod_finance_transactions")).scalar_one() == 0


def test_persist_empty_is_noop(conn: Connection) -> None:
    assert asyncio.run(FinanceModule().dedup(_ctx(conn), [])) == 0


# ----- seam de identidad (dependencia BLANDA via ctx.deps) ----------------------- #


def _ctx_with_identidades(conn: Connection, inbox_ids: tuple[int, ...]) -> ModuleContext:
    """ctx con el handle REAL del directorio inyectado (lo que pasaría el orquestador)."""
    handle = IdentidadesModule().provide_domain(conn, 1)
    return ModuleContext(
        user_id=1,
        conn=conn,
        llm=_NoLLM(),
        deps={"identidades": handle},
        summary_id=None,
        inbox_ids=inbox_ids,
    )


def test_persist_resolves_counterparty_identity(conn: Connection) -> None:
    # contraparte que matchea una identidad sembrada → se persiste el FK canónico.
    oid = conn.execute(
        text(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1,'organizacion','Rappi') RETURNING id"
        )
    ).scalar_one()
    asyncio.run(
        FinanceModule().dedup(_ctx_with_identidades(conn, (1,)), [_item(counterparty="Rappi")])
    )
    fk = conn.execute(
        text("SELECT counterparty_identity_id FROM mod_finance_transactions WHERE user_id = 1")
    ).scalar_one()
    assert fk == oid


def test_persist_without_identidades_leaves_fk_null(conn: Connection) -> None:
    # identidades apagado (deps vacío) → corre igual, FK NULL (best-effort).
    asyncio.run(FinanceModule().dedup(_ctx(conn, (1,)), [_item(counterparty="Rappi")]))
    fk = conn.execute(
        text("SELECT counterparty_identity_id FROM mod_finance_transactions")
    ).scalar_one()
    assert fk is None


def test_persist_unmatched_counterparty_fk_null(conn: Connection) -> None:
    # handle presente pero el directorio no tiene la contraparte → FK NULL (resolve no crea).
    asyncio.run(
        FinanceModule().dedup(
            _ctx_with_identidades(conn, (1,)), [_item(counterparty="Comercio Desconocido SAS")]
        )
    )
    fk = conn.execute(
        text("SELECT counterparty_identity_id FROM mod_finance_transactions")
    ).scalar_one()
    assert fk is None
