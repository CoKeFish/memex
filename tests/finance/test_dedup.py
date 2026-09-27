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
    identity_id: int | None = None,
    inbox: tuple[int, ...] = (),
    observers: tuple[str, ...] = (),
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
        counterparty_identity_id=identity_id,
        source_inbox_ids=frozenset(inbox),
        observers=frozenset(observers),
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


# ----- multi-moneda (conversión + banda) ----------------------------------------- #


def test_cross_currency_equivalent_is_candidate() -> None:
    # 50 USD vs 200000 COP (≈50 USD) mismo día/contraparte/lugar → par, pero cross-currency NUNCA
    # auto-confirma (conversión difusa): queda candidate con señales 'fx' y 'dia'.
    a = _row(1, amount="50.00", currency="USD", counterparty="Rappi", place="Calle 1")
    b = _row(2, amount="200000.00", currency="COP", counterparty="Rappi", place="Calle 1")
    pairs = _pairs(a, b)
    assert len(pairs) == 1
    assert pairs[0].decision == "candidate"
    assert "fx" in pairs[0].reason
    assert "dia" in pairs[0].reason  # cross-currency compara a nivel día aunque ambos sean datetime


def test_cross_currency_mismatched_amount_no_pair() -> None:
    # 50 USD vs 100000 COP (≈25 USD) → fuera de la banda del 12 % → coexisten.
    a = _row(1, amount="50.00", currency="USD")
    b = _row(2, amount="100000.00", currency="COP")
    assert _pairs(a, b) == []


def test_cross_currency_unknown_rate_no_pair() -> None:
    # sin tasa para una moneda → no se puede comparar → coexisten (no se adivina).
    a = _row(1, amount="50.00", currency="USD")
    b = _row(2, amount="50.00", currency="XYZ")
    assert _pairs(a, b) == []


def test_cross_currency_different_identity_still_vetoes() -> None:
    # equivalente por conversión pero identidades distintas → veta (el monto no salva el veto).
    a = _row(1, amount="50.00", currency="USD", identity_id=1)
    b = _row(2, amount="200000.00", currency="COP", identity_id=2)
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


def test_same_responsible_same_identity_true() -> None:
    # ambos resuelven a la MISMA identidad → True aunque el texto difiera mucho.
    a = _row(1, counterparty="RAPPI*REST", identity_id=42)
    b = _row(2, counterparty="Rappi Colombia SAS", identity_id=42)
    assert _same_responsible(a, b, text_threshold=0.85) is True


def test_same_responsible_different_identity_vetoes() -> None:
    # ambos resuelven a identidades DISTINTAS → False (veta el par aunque el texto se parezca).
    a = _row(1, counterparty="Rappi", identity_id=42)
    b = _row(2, counterparty="Rappi", identity_id=99)
    assert _same_responsible(a, b, text_threshold=0.85) is False


def test_same_responsible_one_identity_falls_back_to_text() -> None:
    # solo un lado resolvió → no se compara por id; cae al texto (alta similitud → True).
    a = _row(1, counterparty="Rappi", identity_id=42)
    b = _row(2, counterparty="Rappi", identity_id=None)
    assert _same_responsible(a, b, text_threshold=0.85) is True


def test_different_identity_vetoes_pair_despite_match() -> None:
    # integración de la compuerta: mismo monto + misma hora + lugar (score alto) pero identidades
    # distintas → el veto de `_same_responsible` (False) descarta el par por completo.
    a = _row(1, counterparty="Rappi", place="Calle 1", identity_id=42)
    b = _row(2, counterparty="Rappi", place="Calle 1", identity_id=99)
    assert _pairs(a, b) == []


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


# ----- vetos de naturaleza (0090): dirección / status / kind ---------------------- #


def test_opposite_direction_never_pairs() -> None:
    # Un cobro y su devolución: mismo monto, hora y contraparte, pero NO el mismo movimiento.
    a = _row(1, direction="egreso", place="Calle 1")
    b = _row(2, direction="ingreso", place="Calle 1")
    assert _pairs(a, b) == []


def test_failed_attempt_and_settled_charge_never_pair() -> None:
    # El intento fallido y el cobro que después sí pasó son dos hechos distintos.
    a = DedupRow(**{**_row(1, place="Calle 1").__dict__, "status": "fallida"})
    b = _row(2, place="Calle 1")
    assert _pairs(a, b) == []


def test_refund_and_payment_never_pair() -> None:
    a = DedupRow(**{**_row(1, place="Calle 1").__dict__, "kind": "reembolso"})
    b = _row(2, place="Calle 1")
    assert _pairs(a, b) == []


def test_same_nature_still_pairs_and_direction_not_in_reason() -> None:
    # Dos avisos del mismo cobro (misma naturaleza) siguen emparejando; la dirección ya no es
    # señal del score (es precondición), así que no aparece en `reason`.
    a = _row(1, place="Calle 1")
    b = _row(2, place="Calle 1")
    pairs = _pairs(a, b)
    assert len(pairs) == 1
    assert "direccion" not in pairs[0].reason
    assert pairs[0].decision == "confirmed"


# ----- contraparte por núcleo de organización (sufijos legales) ------------------- #


def test_counterparty_matches_ignoring_legal_suffix() -> None:
    # 'BELLA PIEL' vs 'BELLA PIEL SAS': ratio de texto 0.83 (< 0.85) pero mismo núcleo de org.
    a = _row(1, counterparty="BELLA PIEL", precision=PRECISION_DATE)
    b = _row(2, counterparty="BELLA PIEL SAS", precision=PRECISION_DATE)
    assert _same_responsible(a, b, text_threshold=0.85) is True
    pairs = _pairs(a, b)
    assert len(pairs) == 1
    assert "contraparte" in pairs[0].reason


# ----- origen: mismo mensaje/documento y fuentes independientes ----------------------- #


def test_same_origin_never_pairs_even_when_identical() -> None:
    """Dos filas extraídas del MISMO documento (extracto) son movimientos distintos por
    construcción: ni con monto, hora, contraparte y lugar idénticos forman par."""
    a = _row(1, place="Calle 1", inbox=(11112,), observers=("documentos",))
    b = _row(2, place="Calle 1", inbox=(11112,), observers=("documentos",))
    assert _pairs(a, b) == []


def test_shared_origin_among_several_messages_still_vetoes() -> None:
    a = _row(1, inbox=(10, 11))
    b = _row(2, inbox=(11, 12))
    assert _pairs(a, b) == []


def test_independent_sources_same_day_different_counterparty_is_candidate() -> None:
    """Extracto (el banco nombra a la red de recaudo) vs correo (el comercio): mismo monto + mismo
    día alcanza para el juez aunque el texto de la contraparte no coincida."""
    day = datetime(2026, 1, 31, tzinfo=UTC)
    statement = _row(
        1,
        counterparty="Red de recaudo",
        at=day,
        precision=PRECISION_DATE,
        inbox=(100,),
        observers=("documentos",),
    )
    mail = _row(
        2,
        counterparty="Operador móvil",
        at=day,
        precision=PRECISION_DATE,
        inbox=(200,),
        observers=("email",),
    )
    (p,) = _pairs(statement, mail)
    assert p.decision == "candidate"
    assert "fuentes" in p.reason and "contraparte" not in p.reason


def test_same_kind_same_day_different_counterparty_coexists() -> None:
    """Entre fuentes de la MISMA categoría nada cambia: mismo monto + mismo día sin contraparte
    sigue sin ser par (dos cafés iguales el mismo día)."""
    day = datetime(2026, 1, 31, tzinfo=UTC)
    a = _row(
        1, counterparty="Café A", at=day, precision=PRECISION_DATE, inbox=(1,), observers=("email",)
    )
    b = _row(
        2, counterparty="Café B", at=day, precision=PRECISION_DATE, inbox=(2,), observers=("email",)
    )
    assert _pairs(a, b) == []


def test_independent_sources_different_identity_does_not_veto() -> None:
    """El seam de identidad veta entre fuentes de la misma categoría, pero NO entre fuentes
    independientes: el procesador y el comercio resuelven a identidades distintas y aun así pueden
    ser el mismo cobro — queda sin bonus de contraparte y lo decide el juez."""
    day = datetime(2026, 1, 14, tzinfo=UTC)
    statement = _row(
        1,
        counterparty="Pasarela",
        identity_id=1,
        at=day,
        precision=PRECISION_DATE,
        inbox=(100,),
        observers=("documentos",),
    )
    mail = _row(
        2,
        counterparty="Tienda",
        identity_id=2,
        at=day,
        precision=PRECISION_DATE,
        inbox=(200,),
        observers=("email",),
    )
    (p,) = _pairs(statement, mail)
    assert p.decision == "candidate"
    assert "contraparte" not in p.reason
    # misma categoría → el veto de identidad sigue
    same_kind = _row(
        3,
        counterparty="Tienda",
        identity_id=2,
        at=day,
        precision=PRECISION_DATE,
        inbox=(300,),
        observers=("documentos",),
    )
    assert _pairs(statement, same_kind) == []


def test_independent_sources_with_hour_and_counterparty_auto_confirms() -> None:
    """Registro del agente + alerta del banco: misma hora, mismo monto, misma contraparte, fuentes
    independientes → casi seguro el mismo cargo, se confirma sin juez."""
    agent = _row(1, observers=("agente",))
    mail = _row(2, inbox=(5,), observers=("email",))
    (p,) = _pairs(agent, mail)
    assert p.decision == "confirmed"
    assert "fuentes" in p.reason


def test_unknown_kinds_never_count_as_independent() -> None:
    """Sin categoría conocida de algún lado no se inventa independencia (filas viejas/tests)."""
    day = datetime(2026, 1, 31, tzinfo=UTC)
    a = _row(
        1, counterparty="X", at=day, precision=PRECISION_DATE, inbox=(1,), observers=("email",)
    )
    b = _row(2, counterparty="Y", at=day, precision=PRECISION_DATE, inbox=(2,))
    assert _pairs(a, b) == []


def test_two_senders_same_day_different_counterparty_is_candidate() -> None:
    """Aviso de la pasarela (un remitente) + recibo del comercio (otro remitente), ambos correos:
    observadores distintos → candidato aunque la contraparte difiera."""
    day = datetime(2026, 3, 19, tzinfo=UTC)
    gateway = _row(
        1,
        counterparty="Pasarela Pagos",
        at=day,
        precision=PRECISION_DATE,
        inbox=(1,),
        observers=("email:pasarela.com",),
    )
    shop = _row(
        2,
        counterparty="Tienda",
        at=day,
        precision=PRECISION_DATE,
        inbox=(2,),
        observers=("email:tienda.com",),
    )
    (p,) = _pairs(gateway, shop)
    assert p.decision == "candidate" and "fuentes" in p.reason


def test_same_sender_same_day_different_counterparty_coexists() -> None:
    """Dos alertas del MISMO banco con contrapartes distintas son dos cobros (dos cafés)."""
    day = datetime(2026, 3, 19, tzinfo=UTC)
    a = _row(
        1,
        counterparty="Café A",
        at=day,
        precision=PRECISION_DATE,
        inbox=(1,),
        observers=("email:banco.com",),
    )
    b = _row(
        2,
        counterparty="Café B",
        at=day,
        precision=PRECISION_DATE,
        inbox=(2,),
        observers=("email:banco.com",),
    )
    assert _pairs(a, b) == []
