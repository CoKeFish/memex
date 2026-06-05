"""Tests puros de la FASE 1 procedimental de finance (`dedup.py`): compuerta, bandas y seam.

Sin DB ni LLM. Cubre: la compuerta mínima (monto/moneda/tiempo), las tres bandas de decisión
(coexisten / candidate / confirmed), la regla de que el auto-confirm exige hora confiable, el seam
`_same_responsible`, y el orden canónico de los pares.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from memex.modules.finance.dedup import (
    PRECISION_DATE,
    PRECISION_DATETIME,
    DedupRow,
    _same_responsible,
    mark_duplicates,
)

_BASE = datetime(2026, 6, 3, 14, 0, tzinfo=UTC)


def _row(
    tid: int,
    *,
    amount: str = "100.00",
    currency: str = "USD",
    direction: str = "egreso",
    category: str = "comida",
    counterparty: str = "Rappi",
    place: str = "",
    at: datetime | None = None,
    precision: str = PRECISION_DATETIME,
) -> DedupRow:
    return DedupRow(
        transaction_id=tid,
        direction=direction,
        amount=Decimal(amount),
        currency=currency,
        category=category,
        counterparty=counterparty,
        place=place,
        occurred_at=at if at is not None else _BASE,
        precision=precision,
    )


def _pairs(*rows: DedupRow):  # type: ignore[no-untyped-def]
    return mark_duplicates(list(rows), [])


# ----- compuerta mínima ---------------------------------------------------------- #


def test_different_amount_no_pair() -> None:
    assert _pairs(_row(1, amount="100.00"), _row(2, amount="200.00")) == []


def test_different_currency_no_pair() -> None:
    assert _pairs(_row(1, currency="USD"), _row(2, currency="ARS")) == []


def test_hour_window_excludes_far_apart() -> None:
    # ambos con hora: 2h de diferencia supera la ventana de 1h → coexisten.
    a = _row(1, at=_BASE)
    b = _row(2, at=_BASE + timedelta(hours=2))
    assert _pairs(a, b) == []


def test_day_window_allows_same_day_no_time() -> None:
    # sin hora (date): 20h de diferencia entra en la ventana de día (30h).
    a = _row(1, at=_BASE, precision=PRECISION_DATE)
    b = _row(2, at=_BASE + timedelta(hours=20), precision=PRECISION_DATE)
    assert len(_pairs(a, b)) == 1


# ----- bandas de decisión -------------------------------------------------------- #


def test_amount_hour_only_is_candidate() -> None:
    # mismo monto + misma hora, sin contraparte, rubro distinto → base 0.40 + dirección 0.05.
    a = _row(1, counterparty="", category="comida")
    b = _row(2, counterparty="", category="transporte")
    pairs = _pairs(a, b)
    assert len(pairs) == 1
    assert pairs[0].decision == "candidate"
    assert pairs[0].score == 0.45


def test_counterparty_match_below_confirm_is_candidate() -> None:
    # misma hora + contraparte + dirección (sin lugar, rubro distinto) = 0.80 → candidate.
    a = _row(1, counterparty="Rappi", category="comida")
    b = _row(2, counterparty="Rappi", category="transporte")
    pairs = _pairs(a, b)
    assert pairs[0].decision == "candidate"
    assert pairs[0].score == 0.80
    assert "contraparte" in pairs[0].reason


def test_counterparty_and_place_auto_confirms_with_hour() -> None:
    # misma hora + contraparte + lugar + dirección = 0.95 ≥ 0.85 y ambos datetime → confirmed.
    a = _row(1, counterparty="Rappi", place="Calle 1", category="comida")
    b = _row(2, counterparty="Rappi", place="Calle 1", category="transporte")
    pairs = _pairs(a, b)
    assert pairs[0].decision == "confirmed"
    assert pairs[0].score == 0.95


def test_high_score_without_hour_stays_candidate() -> None:
    # mismo DÍA (no hora) + contraparte + lugar + rubro + dirección = 0.85, pero sin hora confiable
    # NO se auto-confirma: lo decide el LLM (sesgo a coexistir cuando no hay hora).
    a = _row(1, place="Calle 1", precision=PRECISION_DATE)
    b = _row(2, place="Calle 1", precision=PRECISION_DATE)
    pairs = _pairs(a, b)
    assert pairs[0].score == 0.85
    assert pairs[0].decision == "candidate"


def test_weak_day_level_coexists() -> None:
    # mismo día, sin contraparte, rubro y dirección distintos → 0.25 < 0.40 → sin par.
    a = _row(1, counterparty="", category="comida", direction="egreso", precision=PRECISION_DATE)
    b = _row(2, counterparty="", category="salud", direction="ingreso", precision=PRECISION_DATE)
    assert _pairs(a, b) == []


# ----- seam de identidad --------------------------------------------------------- #


def test_same_responsible_text_match() -> None:
    a = _row(1, counterparty="Rappi Colombia")
    b = _row(2, counterparty="Rappi Colombia")
    assert _same_responsible(a, b, text_threshold=0.85) is True


def test_same_responsible_none_when_empty() -> None:
    a = _row(1, counterparty="")
    b = _row(2, counterparty="Rappi")
    assert _same_responsible(a, b, text_threshold=0.85) is None


def test_same_responsible_none_when_low_similarity() -> None:
    # texto no puede afirmar "distintos" con confianza → None (no veta; lo decide el LLM).
    a = _row(1, counterparty="Rappi")
    b = _row(2, counterparty="Cabify")
    assert _same_responsible(a, b, text_threshold=0.85) is None


# ----- determinismo / orden ------------------------------------------------------ #


def test_canonical_pair_order() -> None:
    # el id menor primero, sin importar el orden de entrada.
    pairs = mark_duplicates([_row(5), _row(2)], [])
    assert (pairs[0].a_id, pairs[0].b_id) == (2, 5)


def test_new_vs_existing_compared() -> None:
    new = _row(10, counterparty="Rappi", place="Calle 1")
    existing = _row(3, counterparty="Rappi", place="Calle 1")
    pairs = mark_duplicates([new], [existing])
    assert len(pairs) == 1
    assert (pairs[0].a_id, pairs[0].b_id) == (3, 10)
