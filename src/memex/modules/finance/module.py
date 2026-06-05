"""`FinanceModule` — extractor de TRANSACCIONES (ingresos/egresos) con dedup en dos fases.

Satisface `InterestModule` estructuralmente y calca el patrón de calendar (ADR-015 §4, §11): la
extracción inserta la fila CRUDA (coexistencia, nunca fusiona) y corre el dedup determinista FASE 1
dentro de `persist` —marca pares candidatos y auto-confirma los procedimentalmente seguros— en
`ctx.conn` (la tx que abre el orquestador), atómico con el cursor de extracción. La FASE 2 (LLM por
par ambiguo) y la consolidación (fusión por componentes conexos) son workers aparte
(`dedup_llm.py` / `consolidate.py`). `identity_fields=()`: la unicidad del vértice la da la
CONSOLIDACIÓN, no un UNIQUE sobre la fila cruda.

La FECHA del cobro es obligatoria: si el mensaje no la trae (`occurred_on=None`), el módulo infiere
la de RECEPCIÓN (el `inbox.occurred_at` del mensaje citado) — `occurred_at_precision` registra si la
hora es del cobro (`datetime`), solo la fecha (`date`) o inferida de la recepción (`inferred`). El
dedup usa esa precisión para comparar por hora o por día.

`consumes_kinds` excluye SOCIAL a propósito — los movimientos de plata viven en correos (banco,
recibos) y chats. La contraparte se guarda como TEXTO (`counterparty`, evidencia del LLM) Y como
referencia canónica `counterparty_identity_id`: en `persist` se resuelve contra el directorio de
identidades vía `ctx.deps['identidades']` (dependencia BLANDA `optional_deps`; None si identidades
está apagado). El dedup compara responsables por esa identidad cuando ambos lados la tienen
(`dedup._same_responsible`), cayendo al texto si no.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, cast

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.source import HealthResult, SourceKind
from memex.logging import get_logger
from memex.modules.contract import CAP_DEBUG_INBOX, CAP_EXTRACT, ExtractionItem, ModuleContext
from memex.modules.dedup import forget_inbox_rows
from memex.modules.finance.dedup import (
    PRECISION_DATE,
    PRECISION_DATETIME,
    PRECISION_INFERRED,
    DedupRow,
    mark_duplicates,
)
from memex.modules.finance.prompt import FINANCE_SYSTEM_PROMPT
from memex.modules.finance.schema import TransactionItem

if TYPE_CHECKING:
    from memex.modules.identidades.domain import IdentidadesDomain

_log = get_logger("memex.modules.finance")


def _resolve_identity(handle: object | None, name: str) -> int | None:
    """Resuelve la contraparte (`name`) contra el directorio de identidades usando el handle de
    `ctx.deps['identidades']` (dependencia BLANDA). Best-effort: sin handle (identidades apagado) o
    sin texto → None; sin match en el directorio → None. Determinista (el handle no usa LLM)."""
    if handle is None or not name.strip():
        return None
    res = cast("IdentidadesDomain", handle).resolve(name=name)
    return res.id if res is not None else None


#: Margen alrededor del lote para traer transacciones existentes comparables en el dedup. Mayor que
#: la ventana de día (`DEFAULT_DAY_WINDOW`, 30h) para no perder pares por el borde.
_EXISTING_MARGIN = timedelta(days=2)


def _resolve_instant(
    item: TransactionItem, reception_by_id: dict[int, datetime]
) -> tuple[datetime, str]:
    """Mejor instante conocido del cobro + su precisión. Fecha+hora → `datetime`; solo fecha →
    `date` (medianoche placeholder); sin fecha → la RECEPCIÓN más tardía de los mensajes citados
    (`inferred`). Todo tz-aware (UTC) para comparar sin chocar naive/aware con la DB."""
    if item.occurred_on is not None:
        if item.occurred_time is not None:
            return (
                datetime.combine(item.occurred_on, item.occurred_time, tzinfo=UTC),
                PRECISION_DATETIME,
            )
        return datetime.combine(item.occurred_on, time.min, tzinfo=UTC), PRECISION_DATE
    receptions = [reception_by_id[i] for i in item.source_inbox_ids if i in reception_by_id]
    if receptions:
        return max(receptions), PRECISION_INFERRED
    # Defensivo: no debería pasar (validate_item garantiza source_inbox_ids ⊆ lote). Loguea y now.
    _log.warning("finance.reception_missing", source_inbox_ids=list(item.source_inbox_ids))
    return datetime.now(UTC), PRECISION_INFERRED


def _reception_by_id(
    conn: Connection, user_id: int, inbox_ids: Sequence[int]
) -> dict[int, datetime]:
    """`inbox.occurred_at` (recepción, TIMESTAMPTZ) de los mensajes del lote — fallback de fecha."""
    ids = list(inbox_ids)
    if not ids:
        return {}
    rows = conn.execute(
        text("SELECT id, occurred_at FROM inbox WHERE user_id = :uid AND id = ANY(:ids)"),
        {"uid": user_id, "ids": ids},
    ).all()
    return {int(r[0]): r[1] for r in rows}


def _insert_transactions(
    conn: Connection,
    user_id: int,
    items: Sequence[TransactionItem],
    reception_by_id: dict[int, datetime],
    identidades: object | None,
) -> list[DedupRow]:
    """Inserta cada transacción y devuelve un `DedupRow` por cada una (con su `id` nuevo). Resuelve
    la contraparte contra el directorio de identidades (`identidades`, handle de `ctx.deps`) y
    persiste la referencia canónica `counterparty_identity_id` (None si no resolvió / apagado)."""
    rows: list[DedupRow] = []
    for it in items:
        occurred_at, precision = _resolve_instant(it, reception_by_id)
        identity_id = _resolve_identity(identidades, it.counterparty)
        tid = conn.execute(
            text(
                """
                INSERT INTO mod_finance_transactions
                  (user_id, source_inbox_ids, direction, amount, currency, category, counterparty,
                   counterparty_identity_id, place, occurred_at, occurred_at_precision, description,
                   evidence)
                VALUES
                  (:uid, :ids, :direction, :amount, :currency, :category, :counterparty,
                   :identity_id, :place, :occurred_at, :precision, :description, :evidence)
                RETURNING id
                """
            ),
            {
                "uid": user_id,
                "ids": list(it.source_inbox_ids),
                "direction": it.direction,
                "amount": it.amount,
                "currency": it.currency,
                "category": it.category,
                "counterparty": it.counterparty,
                "identity_id": identity_id,
                "place": it.place,
                "occurred_at": occurred_at,
                "precision": precision,
                "description": it.description,
                "evidence": it.evidence,
            },
        ).scalar_one()
        rows.append(
            DedupRow(
                transaction_id=int(tid),
                direction=it.direction,
                amount=it.amount,
                currency=it.currency,
                category=it.category,
                counterparty=it.counterparty,
                place=it.place,
                occurred_at=occurred_at,
                precision=precision,
                counterparty_identity_id=identity_id,
            )
        )
    return rows


def _existing_rows(conn: Connection, user_id: int, new_rows: list[DedupRow]) -> list[DedupRow]:
    """Transacciones ya persistidas del user comparables con el lote: dentro de la ventana temporal
    ± margen, excluyendo las recién insertadas. NO se prefiltra por monto/moneda exactos: un
    duplicado CROSS-CURRENCY tiene otro monto y otra moneda (se compara convertido en el dedup), así
    que ese prefiltro lo perdería; el monto real lo decide la compuerta `fx.approx_equal`. El
    volumen por ventana (± pocos días) es chico, así que traer todo el rango es barato."""
    new_ids = [r.transaction_id for r in new_rows]
    instants = [r.occurred_at for r in new_rows]
    lo = min(instants) - _EXISTING_MARGIN
    hi = max(instants) + _EXISTING_MARGIN
    rows = (
        conn.execute(
            text(
                """
                SELECT id, direction, amount, currency, category, counterparty,
                       counterparty_identity_id, place, occurred_at, occurred_at_precision
                FROM mod_finance_transactions
                WHERE user_id = :uid
                  AND occurred_at BETWEEN :lo AND :hi
                  AND NOT (id = ANY(CAST(:new_ids AS BIGINT[])))
                """
            ),
            {"uid": user_id, "lo": lo, "hi": hi, "new_ids": new_ids},
        )
        .mappings()
        .all()
    )
    return [
        DedupRow(
            transaction_id=int(r["id"]),
            direction=str(r["direction"]),
            amount=r["amount"],
            currency=str(r["currency"]),
            category=str(r["category"]),
            counterparty=str(r["counterparty"]),
            place=str(r["place"]),
            occurred_at=r["occurred_at"],
            precision=str(r["occurred_at_precision"]),
            counterparty_identity_id=(
                int(r["counterparty_identity_id"])
                if r["counterparty_identity_id"] is not None
                else None
            ),
        )
        for r in rows
    ]


def _mark_processed(conn: Connection, new_ids: list[int], in_pair: set[int]) -> None:
    """Tras la FASE 1, marca `processed_at` + `processing_outcome`. Los que quedaron en un par
    (nuevos o existentes) → `'pending'` (la consolidación da el outcome final unique/duplicate); los
    nuevos SIN par → `'unique'`."""
    pending = sorted(in_pair)
    unique = [i for i in new_ids if i not in in_pair]
    if pending:
        conn.execute(
            text(
                "UPDATE mod_finance_transactions SET processed_at = NOW(), "
                "processing_outcome = 'pending' WHERE id = ANY(:ids)"
            ),
            {"ids": pending},
        )
    if unique:
        conn.execute(
            text(
                "UPDATE mod_finance_transactions SET processed_at = NOW(), "
                "processing_outcome = 'unique' WHERE id = ANY(:ids)"
            ),
            {"ids": unique},
        )


def _mark_dedup(conn: Connection, user_id: int, new_rows: list[DedupRow]) -> int:
    """Corre la FASE 1 determinista, registra los pares (candidatos + auto-confirmados) y marca el
    estado de procesamiento. Devuelve cuántos pares marcó."""
    existing = _existing_rows(conn, user_id, new_rows)
    pairs = mark_duplicates(new_rows, existing)
    in_pair: set[int] = set()
    for p in pairs:
        in_pair.update((p.a_id, p.b_id))
    if pairs:
        conn.execute(
            text(
                """
                INSERT INTO mod_finance_dedup_candidates
                  (user_id, transaction_a_id, transaction_b_id, reason, score, status, decided_by,
                   decided_at)
                VALUES (:uid, :a, :b, :reason, :score, :status, :decided_by,
                        CASE WHEN CAST(:decided_by AS TEXT) IS NULL THEN NULL ELSE NOW() END)
                ON CONFLICT (transaction_a_id, transaction_b_id) DO NOTHING
                """
            ),
            [
                {
                    "uid": user_id,
                    "a": p.a_id,
                    "b": p.b_id,
                    "reason": p.reason,
                    "score": p.score,
                    "status": "confirmed" if p.decision == "confirmed" else "candidate",
                    # auto-confirmados procedimentalmente quedan auditados como 'procedural'.
                    "decided_by": "procedural" if p.decision == "confirmed" else None,
                }
                for p in pairs
            ],
        )
    _mark_processed(conn, [r.transaction_id for r in new_rows], in_pair)
    if pairs:
        _log.info(
            "finance.dedup.marked",
            pairs=len(pairs),
            confirmed=sum(1 for p in pairs if p.decision == "confirmed"),
        )
    return len(pairs)


class FinanceModule:
    """Extrae transacciones y marca duplicados FASE 1 (sin fusionar; mecanismo propio)."""

    slug: ClassVar[str] = "finance"
    interest: ClassVar[str] = (
        "Movimientos de plata de la persona (ingresos y egresos): dinero que pagó o le cobraron y "
        "dinero que recibió o le acreditaron — servicios (luz, agua, internet), compras, consumos "
        "de tarjeta, transferencias, sueldos, reembolsos, restaurantes, transporte. NO publicidad "
        "ni promociones."
    )
    extraction_schema: ClassVar[type[ExtractionItem]] = TransactionItem
    extraction_prompt: ClassVar[str] = FINANCE_SYSTEM_PROMPT
    capabilities: ClassVar[frozenset[str]] = frozenset({CAP_EXTRACT, CAP_DEBUG_INBOX})
    consumes_kinds: ClassVar[frozenset[SourceKind]] = frozenset({SourceKind.EMAIL, SourceKind.CHAT})
    depends_on: ClassVar[tuple[str, ...]] = ()
    #: Dependencia BLANDA de identidades: si está activa, su handle resuelve la contraparte
    #: (`counterparty` → `counterparty_identity_id`) en `persist`; si está apagada, finanzas corre
    #: igual y el dedup cae a comparar contraparte por texto. NO es `depends_on` duro a propósito
    #: (no apagar finanzas cuando identidades esté off).
    optional_deps: ClassVar[tuple[str, ...]] = ("identidades",)
    #: `()` = dedup por MECANISMO PROPIO: la unicidad del vértice-transacción la da la CONSOLIDACIÓN
    #: (`mod_finance_consolidated`), no un UNIQUE sobre la fila cruda — las crudas coexisten; la
    #: FASE 1 solo marca pares (candidatos para el LLM o auto-confirmados procedimentalmente).
    identity_fields: ClassVar[tuple[str, ...]] = ()

    async def persist(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        """Entrypoint del orquestador; delega la unicidad a `self.dedup`."""
        return await self.dedup(ctx, items)

    async def dedup(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        """Mecanismo propio (`()` en `identity_fields`): inserta las transacciones validadas (con la
        fecha de cobro resuelta: la del mensaje o, si falta, la de recepción) y marca pares de
        duplicado FASE 1 (candidatos + auto-confirmados), todo en `ctx.conn` (atómico con el cursor
        de extracción). La unicidad la da la consolidación. Devuelve cuántas insertó."""
        txs = [i for i in items if isinstance(i, TransactionItem)]
        if not txs:
            return 0
        reception_by_id = _reception_by_id(ctx.conn, ctx.user_id, ctx.inbox_ids)
        # Dependencia BLANDA: el handle del directorio si identidades está activo, None si no.
        identidades = ctx.deps.get("identidades")
        new_rows = _insert_transactions(ctx.conn, ctx.user_id, txs, reception_by_id, identidades)
        _mark_dedup(ctx.conn, ctx.user_id, new_rows)
        return len(txs)

    async def health_check(self) -> HealthResult:
        return HealthResult(
            status="healthy", detail="finance module ready", checked_at=datetime.now(UTC)
        )

    def read_for_inbox(
        self, conn: Connection, user_id: int, inbox_ids: Sequence[int]
    ) -> list[dict[str, Any]]:
        """Transacciones públicas (fila cruda) atribuidas a `inbox_ids` (reverse overlap); NO expone
        el estado interno de dedup (candidatos, columnas de control)."""
        rows = (
            conn.execute(
                text(
                    """
                    SELECT direction, amount, currency, category, counterparty, place,
                           occurred_at, occurred_at_precision, description, evidence
                    FROM mod_finance_transactions
                    WHERE user_id = :uid AND CAST(:ids AS BIGINT[]) && source_inbox_ids
                    ORDER BY id
                    """
                ),
                {"uid": user_id, "ids": list(inbox_ids)},
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]

    def debug_for_inbox(
        self, conn: Connection, user_id: int, inbox_ids: Sequence[int]
    ) -> list[dict[str, Any]]:
        """Estado INTERNO por transacción materializada desde `inbox_ids` (capacidad `debug_inbox`,
        vista `/datos/:id`): la identidad de contraparte resuelta (seam), el outcome de dedup + su
        consolidado, y los pares candidatos que la tocan (decisión proc/LLM, score, cuándo)."""
        rows = (
            conn.execute(
                text(
                    """
                    SELECT t.id, t.direction, t.amount, t.currency, t.counterparty,
                           t.counterparty_identity_id,
                           ci.display_name AS counterparty_identity_name,
                           t.occurred_at, t.processing_outcome, t.processed_at, t.created_at,
                           link.consolidated_id,
                           (cons.winner_transaction_id = t.id) AS is_winner
                    FROM mod_finance_transactions t
                    LEFT JOIN mod_identidades ci ON ci.id = t.counterparty_identity_id
                    LEFT JOIN mod_finance_transaction_links link ON link.transaction_id = t.id
                    LEFT JOIN mod_finance_consolidated cons ON cons.id = link.consolidated_id
                    WHERE t.user_id = :uid AND CAST(:ids AS BIGINT[]) && t.source_inbox_ids
                    ORDER BY t.id
                    """
                ),
                {"uid": user_id, "ids": list(inbox_ids)},
            )
            .mappings()
            .all()
        )
        if not rows:
            return []
        tx_ids = [int(r["id"]) for r in rows]
        cands = (
            conn.execute(
                text(
                    """
                    SELECT transaction_a_id, transaction_b_id, reason, score, status,
                           decided_by, confidence, rationale, created_at, decided_at
                    FROM mod_finance_dedup_candidates
                    WHERE user_id = :uid AND (
                        transaction_a_id = ANY(CAST(:ids AS BIGINT[]))
                        OR transaction_b_id = ANY(CAST(:ids AS BIGINT[]))
                    )
                    ORDER BY id
                    """
                ),
                {"uid": user_id, "ids": tx_ids},
            )
            .mappings()
            .all()
        )
        by_tx: dict[int, list[dict[str, Any]]] = {tid: [] for tid in tx_ids}
        for c in cands:
            a, b = int(c["transaction_a_id"]), int(c["transaction_b_id"])
            pair = {
                "reason": c["reason"],
                "score": float(c["score"]) if c["score"] is not None else None,
                "status": c["status"],
                "decided_by": c["decided_by"],
                "confidence": float(c["confidence"]) if c["confidence"] is not None else None,
                "rationale": c["rationale"],
                "created_at": c["created_at"],
                "decided_at": c["decided_at"],
            }
            if a in by_tx:
                by_tx[a].append({**pair, "other_transaction_id": b})
            if b in by_tx:
                by_tx[b].append({**pair, "other_transaction_id": a})
        return [
            {
                "transaction_id": int(r["id"]),
                "direction": r["direction"],
                "amount": float(r["amount"]),
                "currency": r["currency"],
                "counterparty": r["counterparty"],
                "counterparty_identity_id": r["counterparty_identity_id"],
                "counterparty_identity_name": r["counterparty_identity_name"],
                "occurred_at": r["occurred_at"],
                "processing_outcome": r["processing_outcome"],
                "processed_at": r["processed_at"],
                "consolidated_id": r["consolidated_id"],
                "is_winner": r["is_winner"],
                "dedup_candidates": by_tx[int(r["id"])],
            }
            for r in rows
        ]

    def forget_inbox(self, conn: Connection, user_id: int, inbox_ids: Sequence[int]) -> int:
        """Olvida lo aportado por `inbox_ids` a las transacciones (fila cruda): les saca la
        referencia y borra solo la fila huérfana (los candidatos/links/consolidado cuelgan por FK
        CASCADE). NO toca el consolidado de las que sobreviven (es idempotente, se reconstruye)."""
        return forget_inbox_rows(
            conn, "mod_finance_transactions", user_id=user_id, inbox_ids=inbox_ids
        )
