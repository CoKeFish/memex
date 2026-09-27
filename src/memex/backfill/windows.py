"""Aritmética pura de ventanas para el backfill segmentado.

Sin IO ni DB: solo `date`. Las ventanas avanzan hacia adelante dentro de `[range_start, range_end)`,
con `range_end` EXCLUSIVO (igual que el `until` del fetch a demanda e IMAP `BEFORE`). `next_window`
recorta la última ventana parcial a `range_end`. Todo es date-only → sin conversión de TZ: IMAP
`SINCE/BEFORE` son date-only, mismo criterio que el `range` del fetch (la TZ del usuario, America/
Bogota, no entra acá).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

WindowUnit = Literal["day", "week", "month"]


def add_months(start: date, months: int) -> date:
    """Suma `months` meses calendario, con clamp a fin de mes (31-ene +1 mes → 28/29-feb)."""
    if months < 0:
        raise ValueError(f"months debe ser >= 0, got {months}")
    month_index = (start.month - 1) + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    # Último día del mes destino: primer día del mes siguiente menos un día.
    first_of_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = (first_of_next - timedelta(days=1)).day
    return date(year, month, min(start.day, last_day))


def add_window(start: date, unit: WindowUnit, count: int) -> date:
    """Fecha al avanzar `count` unidades desde `start`; la ventana abarca `[start, resultado)`."""
    if count < 1:
        raise ValueError(f"count debe ser >= 1, got {count}")
    if unit == "day":
        return start + timedelta(days=count)
    if unit == "week":
        return start + timedelta(weeks=count)
    if unit == "month":
        return add_months(start, count)
    raise ValueError(f"unidad de ventana inválida: {unit!r}")  # defensivo (Literal lo previene)


def next_window(frontier: date, range_end: date, unit: WindowUnit, count: int) -> tuple[date, date]:
    """Próxima ventana `[frontier, end)` avanzando; `end` recortado a `range_end` (última parcial).

    Precondición: `frontier < range_end` (el caller chequea `is_done` antes de llamar).
    """
    end = min(add_window(frontier, unit, count), range_end)
    return frontier, end


def is_done(frontier: date, range_end: date) -> bool:
    """True si la frontera ya alcanzó (o pasó) el fin del rango — no queda nada por traer."""
    return frontier >= range_end


def progress_pct(range_start: date, range_end: date, frontier: date) -> float:
    """Porcentaje del rango ya cubierto por la frontera (0..100), por días transcurridos."""
    total_days = (range_end - range_start).days
    if total_days <= 0:
        return 100.0
    done_days = (frontier - range_start).days
    return max(0.0, min(100.0, done_days / total_days * 100.0))
