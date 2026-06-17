"""Orquestación del daemon de transporte: cruza calendario + ubicación + tiempo de viaje.

Tres capas:

- `find_next_event` — el próximo evento del calendario con hora y coordenadas (determinista, DB).
- `assess_next_arrival` — añade la ubicación actual (`StoredLocationSource`) y el tiempo de viaje
  (`estimate_trip_from_source`); devuelve el veredicto. Solo CONSULTA (no emite); endpoint y CLI.
- `run_transport_for_user` — encima de la consulta, EMITE un aviso por el seam `Notifier` cuando hay
  que salir (`LEAVE_NOW`/`LATE`). La usa el daemon.

Los errores "reales" del proveedor (`GeoProviderError`/`GeoQuotaError`) NO se atrapan acá: suben al
wrapper del job, que es best-effort. Sí se traducen a veredicto los vacíos legítimos: sin ubicación
(`LocationUnavailableError`) y sin ruta (`GeoNotFoundError`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection

from memex.db import connection
from memex.geo.client import GeoNotFoundError, GeoPoint, GeoProvider
from memex.geo.domain import LocationUnavailableError, StoredLocationSource
from memex.geo.service import estimate_trip_from_source
from memex.modules.calendar.domain import CalendarDomainReader, CalendarEvent
from memex.notifications import Notification, Notifier
from memex.transport.config import TransportConfig
from memex.transport.reachability import (
    ArrivalAssessment,
    ArrivalVerdict,
    assess_arrival,
)


@dataclass(frozen=True)
class NextArrivalResult:
    """El próximo evento + su veredicto de llegada. `reason` explica un veredicto no-numérico
    (`too_far`/`no_location`/`no_route`); es None cuando se calculó un viaje de verdad."""

    event_id: int
    title: str
    destination: GeoPoint
    event_start: datetime
    assessment: ArrivalAssessment
    reason: str | None = None


@dataclass
class TransportStats:
    """Roll-up de una corrida del daemon para un user (serializable a `worker_runs`)."""

    checked: int = 0  # 1 si había un próximo evento que evaluar
    notified: int = 0  # 1 si se emitió un aviso
    verdict: str = "none"
    reason: str = ""


def find_next_event(
    conn: Connection, user_id: int, *, now: datetime, cfg: TransportConfig
) -> tuple[CalendarEvent, datetime, GeoPoint] | None:
    """El próximo evento (más cercano a `now`) con hora y coordenadas, dentro del horizonte.

    Omite los eventos sin `start_time` (todo-el-día) o sin coords: no se puede medir "¿llego?" sin
    destino ni hora. El inicio se hace aware con `cfg.tz` (la hora del calendario es local).
    Devuelve también el `GeoPoint` destino ya validado.
    """
    reader = CalendarDomainReader(conn, user_id)
    horizon_end = now + cfg.horizon
    best: tuple[CalendarEvent, datetime, GeoPoint] | None = None
    for ev in reader.events_in_range(now.date(), horizon_end.date()):
        if ev.start_time is None or ev.geo_lat is None or ev.geo_lng is None:
            continue
        event_start = datetime.combine(ev.starts_on, ev.start_time, tzinfo=cfg.tz)
        if not (now < event_start <= horizon_end):
            continue
        if best is None or event_start < best[1]:
            best = (ev, event_start, GeoPoint(ev.geo_lat, ev.geo_lng))
    return best


async def assess_next_arrival(
    *, user_id: int, provider: GeoProvider, cfg: TransportConfig, now: datetime
) -> NextArrivalResult | None:
    """Evalúa la llegada al próximo evento. None si no hay ninguno con hora+lugar en el horizonte.

    No emite nada (es la consulta que comparten endpoint y CLI). Eventos más lejanos que
    `compute_window` no gastan una llamada a Maps (el tráfico de ahora no predice el de dentro de
    horas): se reportan ON_TIME con `reason="too_far"`.
    """
    with connection() as conn:
        nxt = find_next_event(conn, user_id, now=now, cfg=cfg)
    if nxt is None:
        return None
    event, event_start, destination = nxt

    if event_start - now > cfg.compute_window:
        return _result(event, event_start, destination, _on_time_far(event_start, cfg), "too_far")

    source = StoredLocationSource(user_id)
    try:
        estimate = await estimate_trip_from_source(
            provider, source, destination, mode=cfg.mode, departure_time=now
        )
    except LocationUnavailableError:
        return _result(
            event, event_start, destination, _unknown(now, event_start, cfg), "no_location"
        )
    except GeoNotFoundError:
        return _result(event, event_start, destination, _unknown(now, event_start, cfg), "no_route")

    travel = (
        estimate.duration_in_traffic_s
        if estimate.duration_in_traffic_s is not None
        else estimate.duration_s
    )
    assessment = assess_arrival(
        now=now,
        event_start=event_start,
        travel_seconds=travel,
        buffer=cfg.buffer,
        lead_window=cfg.lead_window,
        mode=cfg.mode,
    )
    return _result(event, event_start, destination, assessment, None)


async def run_transport_for_user(
    *,
    user_id: int,
    provider: GeoProvider,
    notifier: Notifier,
    cfg: TransportConfig,
    now: datetime,
) -> TransportStats:
    """Evalúa y, si hay que salir (`LEAVE_NOW`/`LATE`), emite un aviso por el seam `Notifier`."""
    result = await assess_next_arrival(user_id=user_id, provider=provider, cfg=cfg, now=now)
    if result is None:
        return TransportStats(checked=0, verdict="none", reason="no_event")
    assessment = result.assessment
    stats = TransportStats(checked=1, verdict=assessment.verdict.value, reason=result.reason or "")
    if assessment.verdict in (ArrivalVerdict.LEAVE_NOW, ArrivalVerdict.LATE):
        leave_by = assessment.leave_by
        assert leave_by is not None  # invariante: LEAVE_NOW/LATE siempre traen leave_by
        await notifier.notify(
            _build_notification(user_id=user_id, result=result, leave_by=leave_by, now=now)
        )
        stats.notified = 1
    return stats


def _result(
    event: CalendarEvent,
    event_start: datetime,
    destination: GeoPoint,
    assessment: ArrivalAssessment,
    reason: str | None,
) -> NextArrivalResult:
    return NextArrivalResult(
        event_id=event.event_id,
        title=event.title,
        destination=destination,
        event_start=event_start,
        assessment=assessment,
        reason=reason,
    )


def _unknown(now: datetime, event_start: datetime, cfg: TransportConfig) -> ArrivalAssessment:
    """Veredicto UNKNOWN (no se pudo medir el viaje): sin ubicación o sin ruta."""
    return assess_arrival(
        now=now,
        event_start=event_start,
        travel_seconds=None,
        buffer=cfg.buffer,
        lead_window=cfg.lead_window,
        mode=cfg.mode,
    )


def _on_time_far(event_start: datetime, cfg: TransportConfig) -> ArrivalAssessment:
    """Veredicto para eventos lejanos: vas bien (ON_TIME) y ni se calcula el viaje todavía."""
    return ArrivalAssessment(
        verdict=ArrivalVerdict.ON_TIME,
        leave_by=None,
        arrival_deadline=event_start,
        travel_seconds=None,
        slack_seconds=None,
        mode=cfg.mode,
    )


def _build_notification(
    *, user_id: int, result: NextArrivalResult, leave_by: datetime, now: datetime
) -> Notification:
    assessment = result.assessment
    if assessment.verdict is ArrivalVerdict.LATE:
        severity = "critica"
        title = f"Vas tarde a «{result.title}»"
        body = (
            f"Deberías haber salido a las {leave_by:%H:%M} para llegar a las "
            f"{result.event_start:%H:%M}."
        )
    else:  # LEAVE_NOW
        severity = "alta"
        title = f"Hora de salir hacia «{result.title}»"
        body = f"Salí a más tardar {leave_by:%H:%M} para llegar a las {result.event_start:%H:%M}."
    payload: dict[str, Any] = {
        "event_id": result.event_id,
        "verdict": assessment.verdict.value,
        "leave_by": leave_by.isoformat(),
        "travel_seconds": assessment.travel_seconds,
        "event_start": result.event_start.isoformat(),
    }
    return Notification(
        kind="transport.leave_by",
        severity=severity,
        title=title,
        body=body,
        dedup_key=f"transport:{user_id}:{result.event_id}:{assessment.verdict.value}",
        created_at=now,
        payload=payload,
    )
