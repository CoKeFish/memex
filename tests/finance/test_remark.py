"""`remark_phase1`: re-marca la FASE 1 sobre lo YA cargado y reconcilia los candidatos.

Con DB real y un usuario propio (aislado de lo que dejan otros tests). Cubre: quita el par de dos
filas del MISMO extracto (veto de origen), agrega el par extracto↔correo que la regla vieja no
producía (fuentes independientes), respeta lo que decidió el juez, re-marca `processing_outcome`,
`dry_run` no escribe, y es idempotente.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text

from memex.db import connection
from memex.modules.finance.remark import remark_phase1

_DAY = datetime(2026, 1, 9, 17, 0, tzinfo=UTC)


def _new_user() -> int:
    with connection() as c:
        return int(
            c.execute(
                text("INSERT INTO users (email, display_name) VALUES (:e, 'remark') RETURNING id"),
                {"e": f"remark-{uuid4().hex[:8]}@local"},
            ).scalar_one()
        )


def _seed_inbox(uid: int, *, source_type: str, ext: str) -> int:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (:u, :n, :t) RETURNING id"),
            {"u": uid, "n": f"{ext}-{uid}", "t": source_type},
        ).scalar_one()
        return int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                    "VALUES (:u, :s, :e, :at, CAST('{}' AS JSONB)) RETURNING id"
                ),
                {"u": uid, "s": sid, "e": ext, "at": _DAY},
            ).scalar_one()
        )


def _seed_tx(
    uid: int, *, inbox_ids: list[int], amount: str, counterparty: str, at: datetime
) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_finance_transactions "
                    "(user_id, source_inbox_ids, direction, amount, currency, occurred_at, "
                    " occurred_at_precision, counterparty, processing_outcome) "
                    "VALUES (:u, CAST(:ids AS BIGINT[]), 'egreso', :amt, 'COP', :at, 'date', :cp, "
                    " 'pending') RETURNING id"
                ),
                {"u": uid, "ids": inbox_ids, "amt": amount, "at": at, "cp": counterparty},
            ).scalar_one()
        )


def _seed_candidate(
    uid: int,
    a: int,
    b: int,
    *,
    status: str = "candidate",
    decided_by: str | None = None,
    reason: str = "amount+dia+contraparte+rubro",
    score: float = 0.7,
) -> None:
    lo, hi = min(a, b), max(a, b)
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_finance_dedup_candidates "
                "(user_id, transaction_a_id, transaction_b_id, reason, score, status, decided_by) "
                "VALUES (:u, :a, :b, :r, :s, :st, :d)"
            ),
            {"u": uid, "a": lo, "b": hi, "r": reason, "s": score, "st": status, "d": decided_by},
        )


def _candidates(uid: int) -> list[tuple[int, int, str]]:
    with connection() as c:
        rows = c.execute(
            text(
                "SELECT transaction_a_id, transaction_b_id, status "
                "FROM mod_finance_dedup_candidates WHERE user_id = :u ORDER BY 1, 2"
            ),
            {"u": uid},
        ).all()
    return [(int(a), int(b), str(s)) for a, b, s in rows]


def _outcomes(uid: int) -> dict[int, str]:
    with connection() as c:
        rows = c.execute(
            text("SELECT id, processing_outcome FROM mod_finance_transactions WHERE user_id = :u"),
            {"u": uid},
        ).all()
    return {int(i): str(o) for i, o in rows}


def test_remark_reconciles_pairs_with_current_rules() -> None:
    uid = _new_user()
    statement = _seed_inbox(uid, source_type="documentos", ext="extracto")
    mail = _seed_inbox(uid, source_type="imap", ext="correo")

    # (1) Dos giros iguales en días seguidos del MISMO extracto: la regla vieja los marcó como
    #     candidatos; hoy es veto de origen → el par se quita y las filas vuelven a 'unique'.
    g1 = _seed_tx(uid, inbox_ids=[statement], amount="1000000.00", counterparty="Banco X", at=_DAY)
    g2 = _seed_tx(
        uid,
        inbox_ids=[statement],
        amount="1000000.00",
        counterparty="Banco X",
        at=_DAY + timedelta(days=1),
    )
    _seed_candidate(uid, g1, g2)

    # (2) Extracto (el banco nombra a la red de recaudo) vs correo (el comercio), mismo monto y
    #     día: la regla vieja no lo producía (texto distinto); hoy son fuentes independientes.
    s = _seed_tx(
        uid, inbox_ids=[statement], amount="13000.00", counterparty="Red de recaudo", at=_DAY
    )
    m = _seed_tx(uid, inbox_ids=[mail], amount="13000.00", counterparty="Operador móvil", at=_DAY)

    # (3) Par que la regla nueva ya no produce pero el JUEZ decidió: se respeta tal cual.
    j1 = _seed_tx(uid, inbox_ids=[statement], amount="100.00", counterparty="Tienda", at=_DAY)
    j2 = _seed_tx(uid, inbox_ids=[statement], amount="100.00", counterparty="Tienda", at=_DAY)
    _seed_candidate(uid, j1, j2, status="rejected", decided_by="llm")

    dry = remark_phase1(uid, dry_run=True)
    assert dry.dry_run is True
    assert {(c.a_id, c.b_id) for c in dry.removed} == {(g1, g2)}
    assert {(c.a_id, c.b_id) for c in dry.added} == {(min(s, m), max(s, m))}
    assert dry.kept_decided == 1
    # dry-run: la tabla sigue igual
    assert _candidates(uid) == [(g1, g2, "candidate"), (j1, j2, "rejected")]

    stats = remark_phase1(uid)
    assert stats.dry_run is False
    assert [(c.a_id, c.b_id) for c in stats.removed] == [(g1, g2)]
    (added,) = stats.added
    assert added.status == "candidate"
    assert "fuentes" in added.reason and "contraparte" not in added.reason
    assert _candidates(uid) == [(min(s, m), max(s, m), "candidate"), (j1, j2, "rejected")]

    outcomes = _outcomes(uid)
    assert outcomes[g1] == outcomes[g2] == "unique"
    assert outcomes[s] == outcomes[m] == "pending"

    # Idempotente: una segunda corrida no cambia nada.
    again = remark_phase1(uid)
    assert (again.added, again.removed, again.updated) == ([], [], 0)
    assert again.kept_decided == 1


def test_remark_updates_stale_reason_of_live_candidate() -> None:
    """Un candidato que la regla SIGUE produciendo pero con otra razón/score se actualiza (sin
    tocar decisiones del juez)."""
    uid = _new_user()
    statement = _seed_inbox(uid, source_type="documentos", ext="extracto")
    mail = _seed_inbox(uid, source_type="imap", ext="correo")
    s = _seed_tx(
        uid, inbox_ids=[statement], amount="6698.00", counterparty="Transporte App", at=_DAY
    )
    m = _seed_tx(uid, inbox_ids=[mail], amount="6698.00", counterparty="Transporte App", at=_DAY)
    _seed_candidate(uid, s, m, reason="amount+dia+contraparte+rubro", score=0.7)

    stats = remark_phase1(uid)
    assert stats.updated == 1 and stats.added == [] and stats.removed == []
    with connection() as c:
        reason, score = c.execute(
            text(
                "SELECT reason, score FROM mod_finance_dedup_candidates "
                "WHERE user_id = :u AND transaction_a_id = :a AND transaction_b_id = :b"
            ),
            {"u": uid, "a": min(s, m), "b": max(s, m)},
        ).one()
    assert "fuentes" in reason
    assert float(score) > 0.7
