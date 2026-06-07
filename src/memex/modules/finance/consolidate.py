"""Consolidación de finance (ADR-015 §4): de transacciones crudas por-fuente a UNA canónica.

Calca `calendar/consolidate.py` pero SIN ecos de proveedor, conflictos ni enriquecimiento LLM
(finance no tiene proveedores ni choques de horario). Dos partes:

- PURO (testeable sin DB): `build_groups` (union-find sobre los pares `confirmed` de FASE 1+2 →
  componentes conexos) + `pick_winner`/`merge_fields` (qué versión gana: MÁS COMPLETA > fecha más
  precisa (`datetime` > `date` > `inferred`) > más reciente > id menor; `merge_fields` rellena los
  campos vacíos del ganador desde el resto y adopta la mejor fecha conocida del grupo).
- DB (`run_consolidation`): materializa `mod_finance_consolidated` + `mod_finance_transaction_links`
  de forma ESTABLE e idempotente — un `consolidated_id` no cambia entre corridas salvo merge de
  grupos (re-linkea al menor y tombstonea los otros). Marca el `processing_outcome` de cada cruda.

NO fusiona ni borra crudas: `mod_finance_transactions` es append/coexistencia; la consolidación es
una capa de proyección por encima (la que lee el dashboard).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.db import connection
from memex.logging import get_logger
from memex.relations.deterministic import weave_finance_consolidated

_log = get_logger("memex.modules.finance.consolidate")

#: Orden de preferencia de la precisión de la fecha (menor = mejor) para elegir ganador/fecha.
_PRECISION_RANK = {"datetime": 0, "date": 1, "inferred": 2}


@dataclass(frozen=True)
class ConsTx:
    """Una transacción cruda para consolidar (completitud + precisión + recencia)."""

    transaction_id: int
    direction: str
    amount: Decimal
    currency: str
    category: str
    counterparty: str
    place: str
    occurred_at: datetime
    precision: str
    description: str
    recency: datetime  # created_at (desempate por más reciente)
    counterparty_identity_id: int | None = None


@dataclass(frozen=True)
class ConsolidatedFields:
    """Los campos canónicos de la transacción consolidada + cuál cruda ganó."""

    direction: str
    amount: Decimal
    currency: str
    category: str
    counterparty: str
    place: str
    occurred_at: datetime
    precision: str
    description: str
    winner_transaction_id: int
    counterparty_identity_id: int | None = None


# --- PURO: agrupamiento + elección del ganador ------------------------------------- #


def build_groups(
    transaction_ids: Sequence[int], pairs: Sequence[tuple[int, int]]
) -> list[list[int]]:
    """Union-find: agrupa `transaction_ids` por los `pairs` confirmados (componentes conexos).

    Determinista: cada grupo viene ordenado y la lista de grupos ordenada por su menor id.
    """
    parent: dict[int, int] = {t: t for t in transaction_ids}

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for a, b in pairs:
        if a in parent and b in parent:
            union(a, b)

    groups: dict[int, list[int]] = {}
    for t in transaction_ids:
        groups.setdefault(find(t), []).append(t)
    return [sorted(members) for _, members in sorted(groups.items())]


def _completeness(t: ConsTx) -> int:
    """Cuántos campos opcionales tiene cargados (más completo = mejor ganador)."""
    return sum(
        (
            bool(t.counterparty.strip()),
            bool(t.place.strip()),
            bool(t.description.strip()),
            t.category != "otros",
        )
    )


def _winner_sort_key(t: ConsTx) -> tuple[int, int, float, int]:
    # Ascendente: el MEJOR primero. más completo > fecha más precisa > más reciente > id menor.
    return (
        -_completeness(t),
        _PRECISION_RANK.get(t.precision, 9),
        -t.recency.timestamp(),
        t.transaction_id,
    )


def pick_winner(members: Sequence[ConsTx]) -> ConsTx:
    """La transacción que representa al grupo: más completa > fecha precisa > más reciente > id."""
    return sorted(members, key=_winner_sort_key)[0]


def merge_fields(members: Sequence[ConsTx]) -> ConsolidatedFields:
    """Campos canónicos: los del ganador; rellena los vacíos desde el resto (por prioridad) y adopta
    la MEJOR fecha conocida del grupo si la del ganador es menos precisa."""
    ordered = sorted(members, key=_winner_sort_key)
    winner = ordered[0]
    counterparty, place, description = winner.counterparty, winner.place, winner.description
    occurred_at, precision = winner.occurred_at, winner.precision
    # La identidad del grupo es única (el veto del dedup impide agrupar identidades distintas): la
    # del ganador, o la primera no-nula del resto si el ganador no resolvió.
    counterparty_identity_id = winner.counterparty_identity_id

    for m in ordered:
        if not counterparty.strip() and m.counterparty.strip():
            counterparty = m.counterparty
        if not place.strip() and m.place.strip():
            place = m.place
        if not description.strip() and m.description.strip():
            description = m.description
        if counterparty_identity_id is None and m.counterparty_identity_id is not None:
            counterparty_identity_id = m.counterparty_identity_id
        # Adoptar una fecha más precisa si la del ganador no es la mejor (ordered ya está por
        # precisión dentro de la misma completitud, pero el ganador pudo ganar por completitud con
        # una fecha inferida; tomamos la mejor fecha del grupo).
        if _PRECISION_RANK.get(m.precision, 9) < _PRECISION_RANK.get(precision, 9):
            occurred_at, precision = m.occurred_at, m.precision

    return ConsolidatedFields(
        direction=winner.direction,
        amount=winner.amount,
        currency=winner.currency,
        category=winner.category,
        counterparty=counterparty,
        place=place,
        occurred_at=occurred_at,
        precision=precision,
        description=description,
        winner_transaction_id=winner.transaction_id,
        counterparty_identity_id=counterparty_identity_id,
    )


# --- DB worker --------------------------------------------------------------------- #


@dataclass
class ConsolidationStats:
    groups: int = 0
    consolidated: int = 0  # filas de mod_finance_consolidated insertadas o actualizadas
    merges: int = 0  # consolidados fusionados (tombstoneados) al unirse dos grupos


def _load_transactions(
    conn: Connection, user_id: int, *, ids: Sequence[int] | None = None
) -> list[ConsTx]:
    scope = "" if ids is None else " AND id = ANY(:ids)"
    params: dict[str, object] = {"uid": user_id}
    if ids is not None:
        params["ids"] = list(ids)
    rows = (
        conn.execute(
            text(
                f"""
                SELECT id, direction, amount, currency, category, counterparty,
                       counterparty_identity_id, place, occurred_at, occurred_at_precision,
                       description, created_at
                FROM mod_finance_transactions
                WHERE user_id = :uid{scope}
                ORDER BY id
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [
        ConsTx(
            transaction_id=int(r["id"]),
            direction=str(r["direction"]),
            amount=r["amount"],
            currency=str(r["currency"]),
            category=str(r["category"]),
            counterparty=str(r["counterparty"]),
            place=str(r["place"]),
            occurred_at=r["occurred_at"],
            precision=str(r["occurred_at_precision"]),
            description=str(r["description"]),
            recency=r["created_at"],
            counterparty_identity_id=(
                int(r["counterparty_identity_id"])
                if r["counterparty_identity_id"] is not None
                else None
            ),
        )
        for r in rows
    ]


def _confirmed_pairs(conn: Connection, user_id: int) -> list[tuple[int, int]]:
    rows = conn.execute(
        text(
            "SELECT transaction_a_id, transaction_b_id FROM mod_finance_dedup_candidates "
            "WHERE user_id = :uid AND status = 'confirmed'"
        ),
        {"uid": user_id},
    ).all()
    return [(int(a), int(b)) for a, b in rows]


def _pending_transaction_ids(conn: Connection, user_id: int) -> set[int]:
    rows = conn.execute(
        text(
            "SELECT transaction_a_id, transaction_b_id FROM mod_finance_dedup_candidates "
            "WHERE user_id = :uid AND status = 'candidate'"
        ),
        {"uid": user_id},
    ).all()
    pending: set[int] = set()
    for a, b in rows:
        pending.update((int(a), int(b)))
    return pending


def _write_consolidated(
    conn: Connection, user_id: int, cons_id: int | None, fields: ConsolidatedFields
) -> int:
    params = {
        "uid": user_id,
        "direction": fields.direction,
        "amount": fields.amount,
        "currency": fields.currency,
        "category": fields.category,
        "counterparty": fields.counterparty,
        "identity_id": fields.counterparty_identity_id,
        "place": fields.place,
        "occurred_at": fields.occurred_at,
        "precision": fields.precision,
        "description": fields.description,
        "winner": fields.winner_transaction_id,
    }
    if cons_id is None:
        return int(
            conn.execute(
                text(
                    """
                    INSERT INTO mod_finance_consolidated
                      (user_id, direction, amount, currency, category, counterparty,
                       counterparty_identity_id, place, occurred_at, occurred_at_precision,
                       description, winner_transaction_id)
                    VALUES
                      (:uid, :direction, :amount, :currency, :category, :counterparty,
                       :identity_id, :place, :occurred_at, :precision, :description, :winner)
                    RETURNING id
                    """
                ),
                params,
            ).scalar_one()
        )
    conn.execute(
        text(
            """
            UPDATE mod_finance_consolidated SET
              direction = :direction, amount = :amount, currency = :currency, category = :category,
              counterparty = :counterparty, counterparty_identity_id = :identity_id,
              place = :place, occurred_at = :occurred_at, occurred_at_precision = :precision,
              description = :description, winner_transaction_id = :winner, deleted = FALSE,
              updated_at = NOW()
            WHERE id = :id
            """
        ),
        {**params, "id": cons_id},
    )
    return cons_id


def _link(conn: Connection, user_id: int, cons_id: int, transaction_id: int) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mod_finance_transaction_links (user_id, consolidated_id, transaction_id)
            VALUES (:uid, :cid, :tid)
            ON CONFLICT (transaction_id) DO UPDATE SET consolidated_id = EXCLUDED.consolidated_id
            """
        ),
        {"uid": user_id, "cid": cons_id, "tid": transaction_id},
    )


def _set_outcomes(conn: Connection, outcome_to_ids: dict[str, list[int]]) -> None:
    for outcome, ids in outcome_to_ids.items():
        if ids:
            conn.execute(
                text(
                    "UPDATE mod_finance_transactions SET processing_outcome = :o "
                    "WHERE id = ANY(:ids)"
                ),
                {"o": outcome, "ids": ids},
            )


def _consolidate_group(
    conn: Connection, user_id: int, group_ids: list[int], fields: ConsolidatedFields
) -> tuple[int, int, int]:
    """Materializa el consolidado de UN grupo (crea / fusiona consolidados previos / deja estable) y
    linkea sus transacciones. Estable e idempotente: el `consolidated_id` no cambia salvo merge de
    grupos. Devuelve `(cons_id, escritos, merges)`. NO setea outcomes (lo hace el caller). Lo usan
    `run_consolidation` (batch) y `ensure_consolidated` (incremental)."""
    existing = sorted(
        {
            int(r[0])
            for r in conn.execute(
                text(
                    "SELECT DISTINCT consolidated_id FROM mod_finance_transaction_links "
                    "WHERE transaction_id = ANY(:ids)"
                ),
                {"ids": group_ids},
            ).all()
        }
    )
    written = 0
    merges = 0
    if not existing:
        cons_id = _write_consolidated(conn, user_id, None, fields)
        written = 1
    else:
        cons_id = existing[0]
        changed = len(existing) > 1  # fusión de grupos → siempre reescribe
        if changed:  # un par confirmado unió dos grupos previos → fusionar
            others = existing[1:]
            conn.execute(
                text(
                    "UPDATE mod_finance_transaction_links SET consolidated_id = :keep "
                    "WHERE consolidated_id = ANY(:others)"
                ),
                {"keep": cons_id, "others": others},
            )
            conn.execute(
                text(
                    "UPDATE mod_finance_consolidated "
                    "SET deleted = TRUE, updated_at = NOW() WHERE id = ANY(:others)"
                ),
                {"others": others},
            )
            merges = len(others)
        else:
            # ¿cambió la membresía? Si NO, dejamos sus campos tal cual (estable, sin churn).
            current = {
                int(r[0])
                for r in conn.execute(
                    text(
                        "SELECT transaction_id FROM mod_finance_transaction_links "
                        "WHERE consolidated_id = :cid"
                    ),
                    {"cid": cons_id},
                ).all()
            }
            changed = set(group_ids) != current
        if changed:
            _write_consolidated(conn, user_id, cons_id, fields)
            written = 1
    for tid in group_ids:
        _link(conn, user_id, cons_id, tid)
    return cons_id, written, merges


def _group_outcomes(
    group_ids: list[int], fields: ConsolidatedFields, pending_ids: set[int]
) -> dict[str, list[int]]:
    """Clasifica las tx del grupo en unique/duplicate (las que aún tienen un par 'candidate' sin
    resolver quedan fuera → pending)."""
    outcomes: dict[str, list[int]] = {"unique": [], "duplicate": []}
    for tid in group_ids:
        if tid in pending_ids:
            continue
        if len(group_ids) > 1 and tid != fields.winner_transaction_id:
            outcomes["duplicate"].append(tid)
        else:
            outcomes["unique"].append(tid)
    return outcomes


def run_consolidation(user_id: int) -> ConsolidationStats:
    """Reconstruye la proyección consolidada del user de forma estable e idempotente."""
    stats = ConsolidationStats()
    with connection() as conn:
        txs = _load_transactions(conn, user_id)
        tx_ids = {t.transaction_id for t in txs}
        by_id = {t.transaction_id: t for t in txs}
        pairs = [(a, b) for a, b in _confirmed_pairs(conn, user_id) if a in tx_ids and b in tx_ids]
        pending_ids = _pending_transaction_ids(conn, user_id)

        groups = build_groups(sorted(tx_ids), pairs)
        outcomes: dict[str, list[int]] = {"unique": [], "duplicate": []}
        touched: set[int] = set()  # consolidados tocados → tejer sus aristas al cierre

        for group_ids in groups:
            stats.groups += 1
            fields = merge_fields([by_id[i] for i in group_ids])
            cons_id, written, merges = _consolidate_group(conn, user_id, group_ids, fields)
            stats.consolidated += written
            stats.merges += merges
            touched.add(cons_id)
            grp = _group_outcomes(group_ids, fields, pending_ids)
            outcomes["unique"].extend(grp["unique"])
            outcomes["duplicate"].extend(grp["duplicate"])

        _set_outcomes(conn, outcomes)

        # Tejido incremental de aristas: el vértice de finanzas (el consolidado) nace acá, así que
        # acá se crean «contraparte» y «mismo_evento» (en la misma tx). El full-sweep es respaldo.
        if touched:
            cids = sorted(touched)
            eids = [
                str(r[0])
                for r in conn.execute(
                    text(
                        "SELECT DISTINCT t.event_id FROM mod_finance_transactions t "
                        "JOIN mod_finance_transaction_links l ON l.transaction_id = t.id "
                        "WHERE l.consolidated_id = ANY(:cids) AND t.event_id IS NOT NULL"
                    ),
                    {"cids": cids},
                ).all()
            ]
            weave_finance_consolidated(conn, user_id, cids, eids)

    _log.info(
        "finance.consolidate.done",
        user_id=user_id,
        groups=stats.groups,
        consolidated=stats.consolidated,
        merges=stats.merges,
    )
    return stats


def _confirmed_component(conn: Connection, user_id: int, transaction_id: int) -> list[int]:
    """La tx + sus vecinos por pares de dedup CONFIRMADOS que la tocan. Los pares de una tx recién
    registrada son directos (no existía antes), así que basta el vecindario directo: el resto del
    grupo de cada vecino ya queda representado por su consolidado."""
    rows = conn.execute(
        text(
            """
            SELECT transaction_b_id AS other FROM mod_finance_dedup_candidates
            WHERE user_id = :u AND status = 'confirmed' AND transaction_a_id = :t
            UNION
            SELECT transaction_a_id AS other FROM mod_finance_dedup_candidates
            WHERE user_id = :u AND status = 'confirmed' AND transaction_b_id = :t
            """
        ),
        {"u": user_id, "t": transaction_id},
    ).all()
    return [transaction_id, *(int(r[0]) for r in rows)]


def ensure_consolidated(conn: Connection, user_id: int, transaction_id: int) -> int:
    """INCREMENTAL: consolida el grupo de UNA transacción recién registrada, en la conexión del
    caller (misma tx que `finance.register`). Así el vértice de finanzas (el consolidado) nace al
    escribir, sin esperar al batch. Como FASE 1 puede auto-confirmar un dup, la tx puede UNIRSE a un
    consolidado existente en vez de crear uno nuevo. Reusa la MISMA lógica de grupo que
    `run_consolidation` (que sigue como reconciliador de FASE 2 / merges). Devuelve el cons_id."""
    component = _confirmed_component(conn, user_id, transaction_id)
    by_id = {t.transaction_id: t for t in _load_transactions(conn, user_id, ids=component)}
    pairs = [(a, b) for a, b in _confirmed_pairs(conn, user_id) if a in by_id and b in by_id]
    group_ids = next(g for g in build_groups(sorted(by_id), pairs) if transaction_id in g)
    fields = merge_fields([by_id[i] for i in group_ids])
    cons_id, _written, _merges = _consolidate_group(conn, user_id, group_ids, fields)
    _set_outcomes(conn, _group_outcomes(group_ids, fields, _pending_transaction_ids(conn, user_id)))
    return cons_id
