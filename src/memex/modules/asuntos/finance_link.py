"""Puente determinista asuntos ↔ finanzas: un asunto DE PAGO (una advertencia real con monto o
plazo) se cierra solo cuando finanzas registra un pago completado a esa contraparte POSTERIOR a la
última señal del asunto. Decisión del dueño (2026-09-10): «puede ser un asunto y el pago se debe
hacer y hay una advertencia, pero se cierra una vez pague».

Qué es un asunto de pago: tipo `servicio` o `tramite` cuyo nombre, estado o motivo de prioridad
habla de pagar (pago, cobro, factura, suscripción, deuda, mora, cargo, saldo). Así «Proceso de
grado» (trámite) no se cierra porque hubo un pago a la universidad.

Qué contraparte cuenta: la del pago comparte al menos un token DISTINTIVO (≥ 5 letras, no numérico)
con el nombre o las entidades del asunto («Google Cloud Platform & APIs» ↔ «Pagos Google Cloud»).
La palabra del usuario (estado manual posterior a la última señal) frena el cierre.
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.modules.asuntos import store

_log = get_logger("memex.modules.asuntos.finance_link")

_TOKEN_RX = re.compile(r"[^\wáéíóúüñ]+", re.U)
_DISTINCTIVE_LEN = 5
#: Palabras que marcan un asunto DE PAGO (en español, como escribe el asignador).
_PAYMENT_WORDS = (
    "pago",
    "pagos",
    "cobro",
    "cobros",
    "factur",
    "suscripci",
    "deuda",
    "mora",
    "cargo",
    "cargos",
    "saldo",
)
_GENERIC = frozenset(
    {
        "pagos",
        "pago",
        "cuenta",
        "cuentas",
        "servicio",
        "servicios",
        "platform",
        "plataforma",
        "cloud",
    }
)


def _distinctive(text_: str) -> set[str]:
    return {
        t
        for t in _TOKEN_RX.split(text_.lower())
        if len(t) >= _DISTINCTIVE_LEN and not t.isdigit() and t not in _GENERIC
    }


def is_payment_asunto(a: store.Asunto) -> bool:
    if a.kind not in ("servicio", "tramite"):
        return False
    blob = f"{a.name} {a.state_text} {a.priority_reason}".lower()
    return any(w in blob for w in _PAYMENT_WORDS)


def close_paid(conn: Connection, user_id: int) -> int:
    """Cierra los asuntos de pago vivos que ya tienen un pago completado (egreso) a su contraparte
    después de la última señal. Devuelve cuántos cerró."""
    n = 0
    for a in store.list_asuntos(
        conn, user_id, statuses=("abierto", "candidato"), kinds=("servicio", "tramite"), limit=2000
    ):
        if not is_payment_asunto(a):
            continue
        if a.manual_status_at is not None and (
            a.last_signal_at is None or a.manual_status_at > a.last_signal_at
        ):
            continue
        keys = _distinctive(a.name) | set().union(*(_distinctive(e) for e in a.entities))
        if not keys:
            continue
        since: datetime | None = a.last_signal_at
        rows = conn.execute(
            text(
                """
                SELECT occurred_at, amount, currency, counterparty
                FROM mod_finance_consolidated
                WHERE user_id = :u AND NOT deleted AND status = 'completada'
                  AND direction = 'egreso' AND kind = 'pago'
                  AND (:since IS NULL OR occurred_at > CAST(:since AS TIMESTAMPTZ))
                ORDER BY occurred_at
                """
            ),
            {"u": user_id, "since": since},
        ).all()
        hit = next((r for r in rows if _distinctive(str(r[3])) & keys), None)
        if hit is None:
            continue
        when, amount, currency, counterparty = hit
        store.record_state(
            conn,
            user_id,
            a.id,
            f"Pago de {amount} {currency} a {counterparty} registrado el {when:%Y-%m-%d}: "
            "regularizado (cierre automático por pago).",
            closed=True,
            source="system",
        )
        n += 1
        _log.info(
            "asuntos.close_paid",
            user_id=user_id,
            asunto_id=a.id,
            counterparty=str(counterparty)[:60],
            paid_on=when.date().isoformat(),
        )
    return n
