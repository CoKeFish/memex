"""Motor determinista de "¿llego a tiempo?" — aritmética pura, sin DB, red ni reloj propio.

`assess_arrival` toma el `now`, el inicio del evento y el tiempo de viaje estimado, y decide si hay
que salir ya. Todo se inyecta (incluido `now`) → 100% testeable y reproducible. Sin LLM: es resta de
tiempos (decisión #3 de `docs/subsistema-geo.md`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from memex.geo.client import TravelMode


class ArrivalVerdict(StrEnum):
    """El dictamen de viabilidad de llegada al próximo evento."""

    ON_TIME = "on_time"  # hay holgura de sobra: todavía no hace falta salir
    LEAVE_NOW = "leave_now"  # estás dentro de la ventana de aviso: hora de salir
    LATE = "late"  # ya deberías haber salido (la holgura es negativa)
    UNKNOWN = "unknown"  # falta un dato (sin ubicación, sin ruta, sin hora)


@dataclass(frozen=True)
class ArrivalAssessment:
    """Resultado del cálculo: el veredicto + los números que lo sustentan."""

    verdict: ArrivalVerdict
    leave_by: datetime | None  # inicio - viaje - colchón (None si UNKNOWN)
    arrival_deadline: datetime | None  # = inicio del evento (None si UNKNOWN)
    travel_seconds: int | None
    slack_seconds: int | None  # (leave_by - now) en segundos; negativo = vas tarde
    mode: TravelMode


def assess_arrival(
    *,
    now: datetime,
    event_start: datetime,
    travel_seconds: int | None,
    buffer: timedelta,
    lead_window: timedelta,
    mode: TravelMode = TravelMode.DRIVING,
) -> ArrivalAssessment:
    """Decide si hay que salir hacia el próximo evento.

    `leave_by = event_start - travel - buffer`. Con `slack = leave_by - now`:
    `slack < 0` → LATE; `slack <= lead_window` → LEAVE_NOW; si no → ON_TIME. Sin `travel_seconds`
    (no se pudo estimar) → UNKNOWN. `now` y `event_start` deben ser aware y comparables.
    """
    if travel_seconds is None:
        # El evento se conoce (su inicio); lo único que falta es el viaje → UNKNOWN, pero el
        # `arrival_deadline` sí se reporta.
        return ArrivalAssessment(
            verdict=ArrivalVerdict.UNKNOWN,
            leave_by=None,
            arrival_deadline=event_start,
            travel_seconds=None,
            slack_seconds=None,
            mode=mode,
        )
    leave_by = event_start - timedelta(seconds=travel_seconds) - buffer
    slack = leave_by - now
    slack_seconds = int(slack.total_seconds())
    if slack_seconds < 0:
        verdict = ArrivalVerdict.LATE
    elif slack <= lead_window:
        verdict = ArrivalVerdict.LEAVE_NOW
    else:
        verdict = ArrivalVerdict.ON_TIME
    return ArrivalAssessment(
        verdict=verdict,
        leave_by=leave_by,
        arrival_deadline=event_start,
        travel_seconds=travel_seconds,
        slack_seconds=slack_seconds,
        mode=mode,
    )
