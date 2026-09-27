"""`FinanceModule.dedup` contra la DB: inserta crudas, resuelve la fecha (recepción si falta), marca
pares FASE 1 (incl. auto-confirm procedimental) y el round-trip de `forget_inbox`.
"""

from __future__ import annotations

import asyncio
import json
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


def _seed_inbox(
    occurred_at: datetime,
    *,
    ext: str,
    source_type: str = "imap",
    from_email: str | None = None,
) -> int:
    """Crea source + inbox con `occurred_at` (recepción), commiteado para que el módulo lo vea.
    `source_type` fija la CATEGORÍA de fuente (imap → email; documentos → documentos) y
    `from_email` el remitente (observador `email:<dominio>`)."""
    payload = json.dumps({"from": {"email": from_email}}) if from_email else "{}"
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id"),
            {"n": ext, "t": source_type},
        ).scalar_one()
        iid = c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :sid, :ext, :occ, CAST(:payload AS JSONB)) RETURNING id"
            ),
            {"sid": sid, "ext": ext, "occ": occurred_at, "payload": payload},
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


def test_persist_counterparty_producto_se_ata(conn: Connection) -> None:
    # la contraparte resuelve a un PRODUCTO del directorio → se ata: un producto/plataforma puede
    # cobrarte (Steam, YouTube Premium). Antes se vetaba y obligaba a tipar productos como org.
    pid = conn.execute(
        text(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1,'producto','Steam') RETURNING id"
        )
    ).scalar_one()
    asyncio.run(
        FinanceModule().dedup(_ctx_with_identidades(conn, (1,)), [_item(counterparty="Steam")])
    )
    fk = conn.execute(
        text("SELECT counterparty_identity_id FROM mod_finance_transactions")
    ).scalar_one()
    assert fk == pid


def test_persist_counterparty_desconocido_vetado_fk_null(conn: Connection) -> None:
    # la contraparte resuelve a un DESCONOCIDO (tipo aún sin definir) → veto de finanzas: FK NULL
    # (la contraparte es válida solo si es persona u org, igual que se veta el producto).
    conn.execute(
        text(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1,'desconocido','Buzon X')"
        )
    )
    asyncio.run(
        FinanceModule().dedup(_ctx_with_identidades(conn, (1,)), [_item(counterparty="Buzon X")])
    )
    fk = conn.execute(
        text("SELECT counterparty_identity_id FROM mod_finance_transactions")
    ).scalar_one()
    assert fk is None


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


# ----- traza jerárquica (ctx.trace) ---------------------------------------------- #


def test_dedup_emits_trace_entities_and_dedup_decision(conn: Connection) -> None:
    """Con un tracer real inyectado, `dedup` emite una ENTIDAD por transacción + un paso 'dedup' con
    la comparación 'vs tx #other' del par FASE 1. No-op cuando el tracer está apagado."""
    from memex.core.trace import create_root, open_module_tracer

    iid = _seed_inbox(datetime(2026, 6, 3, 12, 0, tzinfo=UTC), ext="trace-msg")
    # Dos MENSAJES distintos: dos filas del mismo mensaje nunca forman par (veto de origen).
    iid2 = _seed_inbox(datetime(2026, 6, 3, 12, 0, tzinfo=UTC), ext="trace-msg-2")
    root = create_root(conn, user_id=1, inbox_id=iid, label="msg")
    tracer = open_module_tracer(
        conn, user_id=1, inbox_id=iid, root_id=root, slug="finance", label="finance", seq=0
    )
    ctx = ModuleContext(
        user_id=1,
        conn=conn,
        llm=_NoLLM(),
        deps={},
        summary_id=None,
        inbox_ids=(iid, iid2),
        trace=tracer,
    )
    common = {"occurred_on": date(2026, 6, 3), "occurred_time": time(14, 30), "place": "Calle 1"}
    asyncio.run(
        FinanceModule().dedup(
            ctx,
            [_item(source_inbox_ids=(iid,), **common), _item(source_inbox_ids=(iid2,), **common)],
        )
    )

    rows = (
        conn.execute(text("SELECT kind, label FROM trace_nodes WHERE inbox_id = :i"), {"i": iid})
        .mappings()
        .all()
    )
    kinds = [r["kind"] for r in rows]
    assert kinds.count("entity") == 2  # una por transacción
    assert "step" in kinds  # el paso 'dedup'
    assert any(r["kind"] == "decision" and str(r["label"]).startswith("vs tx #") for r in rows)


def test_persist_interprets_receipt_time_as_local(conn: Connection) -> None:
    """La hora escrita en el recibo es hora LOCAL (Bogotá, UTC-5): 14:30 local = 19:30Z. Antes se
    guardaba 14:30Z y la UI (que convierte a Bogotá) mostraba 09:30 — bug 2026-09-04."""
    asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (1,)),
            [_item(occurred_on=date(2026, 6, 3), occurred_time=time(14, 30))],
        )
    )
    row = (
        conn.execute(
            text("SELECT occurred_at, occurred_at_precision FROM mod_finance_transactions")
        )
        .mappings()
        .one()
    )
    assert row["occurred_at_precision"] == "datetime"
    assert row["occurred_at"] == datetime(2026, 6, 3, 19, 30, tzinfo=UTC)


def test_persist_date_only_is_local_midnight(conn: Connection) -> None:
    """Solo fecha → medianoche LOCAL (05:00Z), no medianoche UTC (que en Bogotá es el día anterior
    a las 19:00 y corría la transacción un día)."""
    asyncio.run(FinanceModule().dedup(_ctx(conn, (1,)), [_item(occurred_on=date(2026, 6, 3))]))
    at = conn.execute(text("SELECT occurred_at FROM mod_finance_transactions")).scalar_one()
    assert at == datetime(2026, 6, 3, 5, 0, tzinfo=UTC)


def test_persist_honors_explicit_timezone(conn: Connection) -> None:
    """Si el recibo dice la zona («10:00 UTC»), esa gana sobre la local: se guarda 10:00Z, no
    10:00 Bogotá (15:00Z). Una zona no reconocible cae a local."""
    asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (1,)),
            [
                _item(
                    occurred_on=date(2026, 6, 3),
                    occurred_time=time(10, 0),
                    occurred_tz="UTC",
                    amount=Decimal("1"),
                ),
                _item(
                    occurred_on=date(2026, 6, 3),
                    occurred_time=time(10, 0),
                    occurred_tz="PT",
                    amount=Decimal("2"),
                ),
            ],
        )
    )
    rows = conn.execute(
        text("SELECT amount, occurred_at FROM mod_finance_transactions ORDER BY amount")
    ).all()
    assert rows[0][1] == datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
    assert rows[1][1] == datetime(2026, 6, 3, 15, 0, tzinfo=UTC)


# ----- status / kind (0090) ------------------------------------------------------- #


def test_persist_writes_status_and_kind(conn: Connection) -> None:
    asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (1,)),
            [
                _item(status="failed", description="cobro rechazado"),
                _item(direction="ingreso", kind="refund", description="devolución"),
            ],
        )
    )
    rows = (
        conn.execute(
            text(
                "SELECT direction, status, kind FROM mod_finance_transactions "
                "WHERE user_id = 1 ORDER BY id"
            )
        )
        .mappings()
        .all()
    )
    assert [(r["direction"], r["status"], r["kind"]) for r in rows] == [
        ("egreso", "fallida", "pago"),
        ("ingreso", "completada", "reembolso"),
    ]


def test_persist_failed_and_settled_same_charge_do_not_pair(conn: Connection) -> None:
    # Mismo monto, misma hora, misma contraparte y lugar: antes auto-confirmaba el par; con el
    # veto de naturaleza el intento fallido y el cobro real coexisten (dos hechos).
    when = (date(2026, 6, 3), time(14, 30))
    asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (1,)),
            [
                _item(occurred_on=when[0], occurred_time=when[1], place="web", status="failed"),
                _item(occurred_on=when[0], occurred_time=when[1], place="web"),
            ],
        )
    )
    n_pairs = conn.execute(text("SELECT count(*) FROM mod_finance_dedup_candidates")).scalar_one()
    assert n_pairs == 0
    outcomes = (
        conn.execute(text("SELECT processing_outcome FROM mod_finance_transactions ORDER BY id"))
        .scalars()
        .all()
    )
    assert outcomes == ["unique", "unique"]


def test_persist_rows_from_same_message_never_pair(conn: Connection) -> None:
    """Dos movimientos extraídos del MISMO documento (un extracto con dos giros iguales en días
    seguidos) son hechos distintos por construcción: sin par, ambos 'unique'."""
    iid = _seed_inbox(
        datetime(2026, 1, 9, 12, 0, tzinfo=UTC), ext="extracto-enero", source_type="documentos"
    )
    common = {"amount": "1000000.00", "currency": "COP", "counterparty": "Banco X"}
    asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (iid,)),
            [
                _item(source_inbox_ids=(iid,), occurred_on=date(2026, 1, 9), **common),
                _item(source_inbox_ids=(iid,), occurred_on=date(2026, 1, 10), **common),
            ],
        )
    )
    pairs = conn.execute(
        text(
            "SELECT count(*) FROM mod_finance_dedup_candidates c "
            "JOIN mod_finance_transactions t ON t.id = c.transaction_a_id "
            "WHERE t.source_inbox_ids @> CAST(ARRAY[:i] AS BIGINT[])"
        ),
        {"i": iid},
    ).scalar_one()
    assert pairs == 0
    outcomes = (
        conn.execute(
            text(
                "SELECT processing_outcome FROM mod_finance_transactions "
                "WHERE source_inbox_ids @> CAST(ARRAY[:i] AS BIGINT[]) ORDER BY id"
            ),
            {"i": iid},
        )
        .scalars()
        .all()
    )
    assert list(outcomes) == ["unique", "unique"]


def test_persist_statement_and_mail_same_day_pair_despite_counterparty(conn: Connection) -> None:
    """Extracto (fuente tipo documentos) y correo (imap) del mismo cobro: el banco nombra a la red
    de recaudo y el correo al comercio. Mismo monto + mismo día entre fuentes INDEPENDIENTES → par
    candidato para el juez (antes coexistían y el gasto se contaba dos veces)."""
    day = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
    mail = _seed_inbox(day, ext="correo-operador", source_type="imap")
    statement = _seed_inbox(day, ext="extracto-enero-2", source_type="documentos")
    common = {"amount": "13000.00", "currency": "COP", "occurred_on": date(2026, 1, 31)}
    asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (mail,)),
            [_item(source_inbox_ids=(mail,), counterparty="Operador móvil", **common)],
        )
    )
    asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (statement,)),
            [_item(source_inbox_ids=(statement,), counterparty="Red de recaudo", **common)],
        )
    )
    row = (
        conn.execute(
            text(
                """
                SELECT c.status, c.reason FROM mod_finance_dedup_candidates c
                JOIN mod_finance_transactions ta ON ta.id = c.transaction_a_id
                JOIN mod_finance_transactions tb ON tb.id = c.transaction_b_id
                WHERE ta.source_inbox_ids @> CAST(ARRAY[:m] AS BIGINT[])
                  AND tb.source_inbox_ids @> CAST(ARRAY[:s] AS BIGINT[])
                """
            ),
            {"m": mail, "s": statement},
        )
        .mappings()
        .one()
    )
    assert row["status"] == "candidate"
    assert "fuentes" in row["reason"]


def _pair_between(conn: Connection, inbox_a: int, inbox_b: int) -> list[tuple[str, str]]:
    rows = conn.execute(
        text(
            """
            SELECT c.status, c.reason FROM mod_finance_dedup_candidates c
            JOIN mod_finance_transactions ta ON ta.id = c.transaction_a_id
            JOIN mod_finance_transactions tb ON tb.id = c.transaction_b_id
            WHERE ta.source_inbox_ids @> CAST(ARRAY[:a] AS BIGINT[])
              AND tb.source_inbox_ids @> CAST(ARRAY[:b] AS BIGINT[])
            """
        ),
        {"a": inbox_a, "b": inbox_b},
    ).all()
    return [(str(s), str(r)) for s, r in rows]


def test_persist_two_senders_same_day_pair_despite_counterparty(conn: Connection) -> None:
    """Dos CORREOS de remitentes distintos (aviso de la pasarela, recibo del comercio) del mismo
    pago: observadores distintos → candidato aunque la contraparte difiera."""
    day = datetime(2026, 3, 19, 12, 0, tzinfo=UTC)
    gateway = _seed_inbox(day, ext="aviso-pasarela", from_email="pagos@pasarela.com")
    shop = _seed_inbox(day, ext="recibo-tienda", from_email="ventas@tienda.com")
    common = {"amount": "70.00", "currency": "USD", "occurred_on": date(2026, 3, 19)}
    asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (gateway,)),
            [_item(source_inbox_ids=(gateway,), counterparty="Pasarela Pagos", **common)],
        )
    )
    asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (shop,)), [_item(source_inbox_ids=(shop,), counterparty="Tienda", **common)]
        )
    )
    ((status, reason),) = _pair_between(conn, gateway, shop)
    assert status == "candidate" and "fuentes" in reason


def test_persist_same_sender_same_day_different_counterparty_coexists(conn: Connection) -> None:
    """Dos alertas del MISMO banco con contrapartes distintas: dos cobros, sin par."""
    day = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)
    a = _seed_inbox(day, ext="alerta-banco-1", from_email="alertas@banco.com")
    b = _seed_inbox(day, ext="alerta-banco-2", from_email="alertas@banco.com")
    common = {"amount": "70.00", "currency": "USD", "occurred_on": date(2026, 3, 20)}
    asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (a,)), [_item(source_inbox_ids=(a,), counterparty="Café A", **common)]
        )
    )
    asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (b,)), [_item(source_inbox_ids=(b,), counterparty="Café B", **common)]
        )
    )
    assert _pair_between(conn, a, b) == []


def test_persist_document_row_without_date_uses_the_statement_period(conn: Connection) -> None:
    """Una fila de EXTRACTO sin fecha no puede fecharse el día de la carga: toma la fecha más
    tardía de los movimientos fechados del mismo documento (su período), precisión `inferred`."""
    subida = datetime(2026, 9, 12, 20, 0, tzinfo=UTC)  # día en que se subió el PDF
    iid = _seed_inbox(subida, ext="extracto-julio", source_type="documentos")
    asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (iid,)),
            [
                _item(source_inbox_ids=(iid,), amount="1.00", occurred_on=date(2025, 7, 3)),
                _item(source_inbox_ids=(iid,), amount="2.00", occurred_on=date(2025, 7, 31)),
                _item(source_inbox_ids=(iid,), amount="3.00", occurred_on=None),
            ],
        )
    )
    row = conn.execute(
        text(
            "SELECT occurred_at, occurred_at_precision FROM mod_finance_transactions "
            "WHERE source_inbox_ids @> CAST(ARRAY[:i] AS BIGINT[]) AND amount = 3.00"
        ),
        {"i": iid},
    ).one()
    assert row.occurred_at_precision == "inferred"
    assert row.occurred_at.astimezone(UTC).date() in (date(2025, 7, 31), date(2025, 8, 1))


def test_persist_mail_row_without_date_still_uses_reception(conn: Connection) -> None:
    """Para un correo la recepción sigue siendo el respaldo (ahí sí aproxima al cobro)."""
    recibido = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)
    iid = _seed_inbox(recibido, ext="correo-sin-fecha", source_type="imap")
    asyncio.run(
        FinanceModule().dedup(
            _ctx(conn, (iid,)),
            [
                _item(source_inbox_ids=(iid,), amount="4.00", occurred_on=date(2026, 9, 1)),
                _item(source_inbox_ids=(iid,), amount="5.00", occurred_on=None),
            ],
        )
    )
    row = conn.execute(
        text(
            "SELECT occurred_at FROM mod_finance_transactions "
            "WHERE source_inbox_ids @> CAST(ARRAY[:i] AS BIGINT[]) AND amount = 5.00"
        ),
        {"i": iid},
    ).one()
    assert row.occurred_at == recibido
