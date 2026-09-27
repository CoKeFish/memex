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

`status`/`kind` (0090): la fila cruda conserva si la plata se MOVIÓ (`completada` | `fallida` |
`pendiente`) y si es un `pago` o un `reembolso`. Se persisten tal cual los declaró el LLM (ya
normalizados por el schema); quién CUENTA lo decide la capa de lectura (API/dashboard: solo
`completada`; un reembolso descuenta del gasto). La FASE 1 nunca empareja naturalezas distintas.

`consumes_kinds` excluye SOCIAL a propósito — los movimientos de plata viven en correos (banco,
recibos) y chats. La contraparte se guarda como TEXTO (`counterparty`, evidencia del LLM) Y como
referencia canónica `counterparty_identity_id`: en `persist` se resuelve contra el directorio de
identidades vía `ctx.deps['identidades']` (dependencia BLANDA `optional_deps`; None si identidades
está apagado). El dedup compara responsables por esa identidad cuando ambos lados la tienen
(`dedup._same_responsible`), cayendo al texto si no.

Cada `DedupRow` lleva su ORIGEN (`origen.py`): los mensajes de los que salió y quién los
reportó (observador: remitente del correo, chat, documento, agente). Dos filas del mismo
mensaje/documento nunca son par (un extracto no repite movimientos); dos filas de observadores
distintos (extracto vs correo, pasarela vs comercio) son fuentes independientes del mismo cobro
y el dedup lo pondera así. `remark.py` re-marca lo ya cargado cuando cambian estas reglas.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, cast

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.localtime import local_instant, parse_tz
from memex.core.source import HealthResult, SourceKind
from memex.logging import get_logger
from memex.modules.contract import CAP_EXTRACT, ExtractionItem, ModuleContext
from memex.modules.dedup import forget_inbox_rows
from memex.modules.finance import rubros
from memex.modules.finance.consolidate import ensure_consolidated
from memex.modules.finance.dedup import (
    KIND_AGENT,
    PRECISION_DATE,
    PRECISION_DATETIME,
    PRECISION_INFERRED,
    DedupPair,
    DedupRow,
    mark_duplicates,
)
from memex.modules.finance.origen import (
    OBSERVER_HINT_SQL,
    kind_of,
    observers_by_inbox,
    observers_for_origin,
)
from memex.modules.finance.prompt import FINANCE_SYSTEM_PROMPT
from memex.modules.finance.schema import (
    FINANCE_CATEGORIES,
    TransactionItem,
    normalize_kind,
    normalize_status,
)
from memex.relations.deterministic import weave_finance_consolidated

if TYPE_CHECKING:
    from memex.core.trace import TraceNode
    from memex.modules.identidades.domain import IdentidadesDomain

_log = get_logger("memex.modules.finance")


def _resolve_identity(handle: object | None, name: str) -> int | None:
    """Resuelve la contraparte (`name`) contra el directorio de identidades usando el handle de
    `ctx.deps['identidades']` (dependencia BLANDA). Best-effort: sin handle (identidades apagado) o
    sin texto → None; sin match en el directorio → None. Determinista (el handle no usa LLM).
    VETO de kind (política de finanzas, allowlist): la contraparte es una persona, una organización
    o un PRODUCTO (una plataforma/servicio que te cobra: Steam, YouTube Premium, Google One, Cursor
    — modelarlos como organización solo para poder atarles el recibo desfiguraba la jerarquía).
    Un `desconocido` (tipo aún sin definir) NO se ata (None) en vez de atar mal."""
    if handle is None or not name.strip():
        return None
    res = cast("IdentidadesDomain", handle).resolve(name=name)
    if res is None or res.kind not in ("persona", "organizacion", "producto"):
        return None
    return res.id


#: Margen alrededor del lote para traer transacciones existentes comparables en el dedup. Mayor que
#: la ventana de día (`DEFAULT_DAY_WINDOW`, 30h) para no perder pares por el borde.
_EXISTING_MARGIN = timedelta(days=2)


def _resolve_instant(
    item: TransactionItem, reception_by_id: dict[int, datetime]
) -> tuple[datetime, str]:
    """Mejor instante conocido del cobro + su precisión. Fecha+hora → `datetime`; solo fecha →
    `date` (medianoche LOCAL placeholder); sin fecha → la RECEPCIÓN más tardía de los mensajes
    citados (`inferred`). Todo tz-aware para comparar sin chocar naive/aware con la DB.

    La fecha/hora que extrae el LLM es la que está ESCRITA en el recibo, es decir hora local del
    usuario (`memex.core.localtime`): combinarla con UTC corría cada cobro 5 h hacia atrás en la
    UI y en el cruce con GPS (bug 2026-09-04)."""
    if item.occurred_on is not None:
        tz = parse_tz(item.occurred_tz)  # zona EXPLÍCITA del mensaje si la dijo; si no, local
        if item.occurred_time is not None:
            return local_instant(item.occurred_on, item.occurred_time, tz), PRECISION_DATETIME
        return local_instant(item.occurred_on, tz=tz), PRECISION_DATE
    receptions = [reception_by_id[i] for i in item.source_inbox_ids if i in reception_by_id]
    if receptions:
        return max(receptions), PRECISION_INFERRED
    # Defensivo: no debería pasar (validate_item garantiza source_inbox_ids ⊆ lote). Loguea y now.
    _log.warning("finance.reception_missing", source_inbox_ids=list(item.source_inbox_ids))
    return datetime.now(UTC), PRECISION_INFERRED


def _document_date_fallback(
    items: Sequence[TransactionItem],
    reception_by_id: dict[int, datetime],
    observer_by_inbox: dict[int, str],
) -> dict[int, datetime]:
    """Para un DOCUMENTO aportado (extracto), la «recepción» es el día en que se subió el archivo
    y no dice nada del movimiento: una fila sin fecha fechada así aparece como un movimiento de
    HOY. El respaldo correcto es el período del propio documento: la fecha más tardía entre los
    movimientos fechados que salieron de ese mismo documento. Los mensajes (correo, chat) siguen
    con la recepción, que ahí sí es una buena aproximación."""
    latest: dict[int, date] = {}
    for it in items:
        if it.occurred_on is None:
            continue
        for iid in it.source_inbox_ids:
            if kind_of(observer_by_inbox.get(iid, "")) == SourceKind.DOCUMENTOS.value:
                prev = latest.get(iid)
                if prev is None or it.occurred_on > prev:
                    latest[iid] = it.occurred_on
    if not latest:
        return reception_by_id
    out = dict(reception_by_id)
    for iid, day in latest.items():
        out[iid] = local_instant(day)
    return out


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
    observer_by_inbox: dict[int, str],
    identidades: object | None,
) -> list[DedupRow]:
    """Inserta cada transacción y devuelve un `DedupRow` por cada una (con su `id` nuevo). Resuelve
    la contraparte contra el directorio de identidades (`identidades`, handle de `ctx.deps`) y
    persiste la referencia canónica `counterparty_identity_id` (None si no resolvió / apagado).
    `observer_by_inbox` (mensaje → observador) da el origen de cada fila para el dedup."""
    rows: list[DedupRow] = []
    # La regla de la contraparte («Nacanisaje es un restaurante») manda sobre lo que dijo
    # el modelo: se carga una vez por corrida.
    rules = rubros.rules_map(conn, user_id)
    for it in items:
        occurred_at, precision = _resolve_instant(it, reception_by_id)
        identity_id = _resolve_identity(identidades, it.counterparty)
        category = rubros.with_rule(rules, it.counterparty, it.category)
        tid = conn.execute(
            text(
                """
                INSERT INTO mod_finance_transactions
                  (user_id, source_inbox_ids, direction, status, kind, amount, currency, category,
                   counterparty, counterparty_identity_id, place, occurred_at,
                   occurred_at_precision, description, evidence)
                VALUES
                  (:uid, :ids, :direction, :status, :kind, :amount, :currency, :category,
                   :counterparty, :identity_id, :place, :occurred_at, :precision, :description,
                   :evidence)
                RETURNING id
                """
            ),
            {
                "uid": user_id,
                "ids": list(it.source_inbox_ids),
                "direction": it.direction,
                "status": it.status,
                "kind": it.kind,
                "amount": it.amount,
                "currency": it.currency,
                "category": category,
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
                category=category,
                counterparty=it.counterparty,
                place=it.place,
                occurred_at=occurred_at,
                precision=precision,
                counterparty_identity_id=identity_id,
                status=it.status,
                kind=it.kind,
                source_inbox_ids=frozenset(it.source_inbox_ids),
                observers=frozenset(
                    observer_by_inbox[i] for i in it.source_inbox_ids if i in observer_by_inbox
                ),
            )
        )
    return rows


def load_dedup_rows(
    conn: Connection,
    user_id: int,
    *,
    lo: datetime | None = None,
    hi: datetime | None = None,
    exclude_ids: Sequence[int] = (),
) -> list[DedupRow]:
    """Transacciones persistidas del user como `DedupRow`, con su ORIGEN resuelto (mensajes y
    categorías de fuente: veto de origen + fuentes independientes). Acotable por ventana `[lo, hi]`
    y excluyendo ids. Sin filtro por monto/moneda a propósito: un duplicado CROSS-CURRENCY tiene
    otro monto y otra moneda (se compara convertido en el dedup); el monto real lo decide la
    compuerta `fx.approx_equal`."""
    rows = (
        conn.execute(
            text(
                f"""
                SELECT t.id, t.direction, t.status, t.kind, t.amount, t.currency, t.category,
                       t.counterparty, t.counterparty_identity_id, t.place, t.occurred_at,
                       t.occurred_at_precision, t.source_inbox_ids,
                       (SELECT array_agg(DISTINCT s.type || chr(9) || {OBSERVER_HINT_SQL})
                          FROM inbox i JOIN sources s ON s.id = i.source_id
                         WHERE i.id = ANY(t.source_inbox_ids)) AS source_hints
                FROM mod_finance_transactions t
                WHERE t.user_id = :uid
                  AND (CAST(:lo AS TIMESTAMPTZ) IS NULL
                       OR t.occurred_at >= CAST(:lo AS TIMESTAMPTZ))
                  AND (CAST(:hi AS TIMESTAMPTZ) IS NULL
                       OR t.occurred_at <= CAST(:hi AS TIMESTAMPTZ))
                  AND NOT (t.id = ANY(CAST(:exclude AS BIGINT[])))
                ORDER BY t.id
                """
            ),
            {"uid": user_id, "lo": lo, "hi": hi, "exclude": list(exclude_ids)},
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
            status=str(r["status"]),
            kind=str(r["kind"]),
            source_inbox_ids=frozenset(int(i) for i in (r["source_inbox_ids"] or ())),
            observers=observers_for_origin(r["source_hints"], r["source_inbox_ids"] or ()),
        )
        for r in rows
    ]


def _existing_rows(conn: Connection, user_id: int, new_rows: list[DedupRow]) -> list[DedupRow]:
    """Transacciones ya persistidas comparables con el lote: dentro de la ventana temporal ± margen,
    excluyendo las recién insertadas. El volumen por ventana (± pocos días) es chico, así que traer
    todo el rango es barato."""
    instants = [r.occurred_at for r in new_rows]
    return load_dedup_rows(
        conn,
        user_id,
        lo=min(instants) - _EXISTING_MARGIN,
        hi=max(instants) + _EXISTING_MARGIN,
        exclude_ids=[r.transaction_id for r in new_rows],
    )


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


def insert_pairs(conn: Connection, user_id: int, pairs: Sequence[DedupPair]) -> None:
    """Registra pares FASE 1 en `mod_finance_dedup_candidates` (candidatos y auto-confirmados;
    estos quedan auditados como `decided_by='procedural'`). Un par ya registrado no se pisa."""
    if not pairs:
        return
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
                "decided_by": "procedural" if p.decision == "confirmed" else None,
            }
            for p in pairs
        ],
    )


def _mark_dedup(conn: Connection, user_id: int, new_rows: list[DedupRow]) -> list[DedupPair]:
    """Corre la FASE 1 determinista, registra los pares (candidatos + auto-confirmados) y marca el
    estado de procesamiento. Devuelve los pares marcados (para la traza)."""
    existing = _existing_rows(conn, user_id, new_rows)
    pairs = mark_duplicates(new_rows, existing)
    in_pair: set[int] = set()
    for p in pairs:
        in_pair.update((p.a_id, p.b_id))
    if pairs:
        insert_pairs(conn, user_id, pairs)
    _mark_processed(conn, [r.transaction_id for r in new_rows], in_pair)
    if pairs:
        _log.info(
            "finance.dedup.marked",
            pairs=len(pairs),
            confirmed=sum(1 for p in pairs if p.decision == "confirmed"),
        )
    return pairs


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
    capabilities: ClassVar[frozenset[str]] = frozenset({CAP_EXTRACT})
    consumes_kinds: ClassVar[frozenset[SourceKind]] = frozenset(
        {SourceKind.EMAIL, SourceKind.CHAT, SourceKind.DOCUMENTOS}
    )
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
        # Origen de cada fila (observador por mensaje): veto de origen + fuentes independientes.
        observer_by_inbox = observers_by_inbox(ctx.conn, ctx.user_id, ctx.inbox_ids)
        reception_by_id = _document_date_fallback(txs, reception_by_id, observer_by_inbox)
        # Dependencia BLANDA: el handle del directorio si identidades está activo, None si no.
        identidades = ctx.deps.get("identidades")
        new_rows = _insert_transactions(
            ctx.conn, ctx.user_id, txs, reception_by_id, observer_by_inbox, identidades
        )
        # Traza: una ENTIDAD por transacción + el seam contraparte→identidad (no-op si apagada).
        ents: dict[int, TraceNode] = {}
        for it, r in zip(txs, new_rows, strict=True):
            cp = (r.counterparty or r.place or "").strip()
            label = f"{r.direction} {r.amount} {r.currency}" + (f" · {cp}" if cp else "")
            if r.kind != "pago":
                label += f" · {r.kind}"
            if r.status != "completada":
                label += f" · {r.status}"
            ent = ctx.trace.entity(
                "mod_finance_transactions",
                id=r.transaction_id,
                label=label,
                status="ok",
                source_inbox_ids=it.source_inbox_ids,
            )
            if r.counterparty_identity_id is not None:
                ent.decision(
                    f"contraparte → identidad #{r.counterparty_identity_id}",
                    ref=("mod_identidades", r.counterparty_identity_id),
                    detail={"contraparte": r.counterparty},
                    status="ok",
                )
            elif r.counterparty.strip():
                ent.log(
                    "contraparte sin identidad resuelta", detail={"contraparte": r.counterparty}
                )
            ents[r.transaction_id] = ent
        # Traza: dedup FASE 1 → comparación "vs tx #other" bajo la entidad que toca el par.
        pairs = _mark_dedup(ctx.conn, ctx.user_id, new_rows)
        steps: dict[int, TraceNode] = {}
        for p in pairs:
            for tid in (p.a_id, p.b_id):
                if tid not in ents:
                    continue  # el otro lado es una transacción pre-existente, no de este mensaje
                other = p.b_id if tid == p.a_id else p.a_id
                step = steps.get(tid)
                if step is None:
                    step = ents[tid].step("dedup")
                    steps[tid] = step
                step.decision(
                    f"vs tx #{other}",
                    ref=("mod_finance_transactions", other),
                    detail={"reason": p.reason, "score": p.score, "decision": p.decision},
                    status="ok" if p.decision == "confirmed" else "warn",
                )
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
                    SELECT direction, status, kind, amount, currency, category, counterparty,
                           place, occurred_at, occurred_at_precision, description, evidence
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

    def forget_inbox(self, conn: Connection, user_id: int, inbox_ids: Sequence[int]) -> int:
        """Olvida lo aportado por `inbox_ids` a las transacciones (fila cruda): les saca la
        referencia y borra solo la fila huérfana (los candidatos/links/consolidado cuelgan por FK
        CASCADE). NO toca el consolidado de las que sobreviven (es idempotente, se reconstruye)."""
        return forget_inbox_rows(
            conn, "mod_finance_transactions", user_id=user_id, inbox_ids=inbox_ids
        )


def register(
    conn: Connection,
    user_id: int,
    *,
    amount: Decimal,
    currency: str,
    direction: str = "egreso",
    category: str = "otros",
    counterparty: str = "",
    place: str = "",
    occurred_at: datetime | None = None,
    occurred_at_precision: str | None = None,
    description: str = "",
    event_id: str | None = None,
    counterparty_identity_id: int | None = None,
    status: str = "completada",
    kind: str = "pago",
) -> dict[str, Any]:
    """Registra una transacción DETERMINISTA (sin LLM): inserta + resuelve la contraparte a una
    identidad del directorio + marca dedup FASE 1. Entrada por AGENTE (Hermes pasa los campos ya
    leídos de la factura/texto); el enriquecimiento de dominio (identidad, dedup) corre ACÁ, no en
    Hermes. `event_id` correlaciona con otros hechos del mismo mensaje. Asegura el CONSOLIDADO de la
    tx en el acto (`ensure_consolidated`, el vértice de finanzas) y teje sus aristas «contraparte» +
    «mismo_evento» en la misma tx; `run_consolidation` queda como reconciliador (FASE 2 / merges).

    `counterparty_identity_id`: si viene (p. ej. del cierre de un evento del agente, donde la
    identidad del comercio se creó ANTES en la misma tx), se usa DIRECTO y se saltea
    `_resolve_identity` — ata el FK por id, determinista, sin depender del match-exacto por nombre.
    `status`/`kind` (0090) se normalizan como en el schema (sinónimos → valor cerrado; raro →
    default): un agente puede registrar un reembolso o un cobro fallido a sabiendas.
    Devuelve la fila pública (con `amount` float)."""
    from memex.modules.identidades.module import IdentidadesModule

    if occurred_at is None:
        occurred_at = datetime.now(UTC)
        prec = PRECISION_DATETIME
    else:
        prec = (
            occurred_at_precision
            if occurred_at_precision in (PRECISION_DATETIME, PRECISION_DATE)
            else PRECISION_DATETIME
        )
    direction = "ingreso" if direction.strip().lower() == "ingreso" else "egreso"
    status = normalize_status(status)
    kind = normalize_kind(kind)
    cat = category.strip().lower()
    category = cat if cat in FINANCE_CATEGORIES else "otros"
    category = rubros.with_rule(rubros.rules_map(conn, user_id), counterparty, category)
    currency = (currency or "").strip().upper()

    # Identidad del comercio: explícita (cierre de evento) o best-effort contra el directorio
    # (aunque el módulo esté apagado).
    if counterparty_identity_id is not None:
        identity_id: int | None = counterparty_identity_id
    else:
        identity_id = _resolve_identity(
            IdentidadesModule().provide_domain(conn, user_id), counterparty
        )

    row = (
        conn.execute(
            text(
                """
                INSERT INTO mod_finance_transactions
                  (user_id, source_inbox_ids, direction, status, kind, amount, currency, category,
                   counterparty, counterparty_identity_id, place, occurred_at,
                   occurred_at_precision, description, evidence, event_id)
                VALUES
                  (:uid, ARRAY[]::bigint[], :direction, :status, :kind, :amount, :currency,
                   :category, :counterparty, :identity_id, :place, :occurred_at, :precision,
                   :description, '', :event_id)
                RETURNING id, direction, status, kind, amount, currency, category, counterparty,
                          counterparty_identity_id, place, occurred_at, occurred_at_precision,
                          description, event_id, created_at
                """
            ),
            {
                "uid": user_id,
                "direction": direction,
                "status": status,
                "kind": kind,
                "amount": amount,
                "currency": currency,
                "category": category,
                "counterparty": counterparty,
                "identity_id": identity_id,
                "place": place,
                "occurred_at": occurred_at,
                "precision": prec,
                "description": description.strip(),
                "event_id": event_id,
            },
        )
        .mappings()
        .one()
    )
    _mark_dedup(
        conn,
        user_id,
        [
            DedupRow(
                transaction_id=int(row["id"]),
                direction=direction,
                amount=amount,
                currency=currency,
                category=category,
                counterparty=counterparty,
                place=place,
                occurred_at=occurred_at,
                precision=prec,
                counterparty_identity_id=identity_id,
                status=status,
                kind=kind,
                observers=frozenset({KIND_AGENT}),
            )
        ],
    )
    # El vértice de finanzas (el consolidado) nace acá, al escribir: aseguramos su consolidado en la
    # misma tx y tejemos sus aristas (contraparte + mismo_evento). El batch queda de reconciliador.
    cons_id = ensure_consolidated(conn, user_id, int(row["id"]))
    weave_finance_consolidated(conn, user_id, [cons_id], [event_id] if event_id else [])
    _log.info(
        "finance.registered",
        user_id=user_id,
        transaction_id=int(row["id"]),
        amount=str(amount),
        currency=currency,
        event_id=event_id,
    )
    out = dict(row)
    out["amount"] = float(out["amount"])
    # El id consolidado va en la respuesta para que el agente pueda encadenar (p.ej.
    # `memex finance set-place --id <consolidated_id> ...`).
    out["consolidated_id"] = cons_id
    # ¿Se fundió con un pago que YA estaba? (FASE 1 auto-confirma el dup: mismo monto, fecha y
    # contraparte, p. ej. la misma factura mandada dos veces al bot). El caller lo dice en vez de
    # «registré» a secas; el dato no se duplica en el consolidado de todos modos.
    members = conn.execute(
        text("SELECT count(*) FROM mod_finance_transaction_links WHERE consolidated_id = :c"),
        {"c": cons_id},
    ).scalar_one()
    out["already_registered"] = int(members) > 1
    return out
