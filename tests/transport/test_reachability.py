"""Motor puro de reachability: los bordes de lead_window/buffer y el caso sin ruta."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memex.geo.client import TravelMode
from memex.transport.reachability import ArrivalAssessment, ArrivalVerdict, assess_arrival

_NOW = datetime(2026, 6, 17, 14, 0, tzinfo=UTC)
_BUFFER = timedelta(minutes=10)
_LEAD = timedelta(minutes=30)


def _assess(*, event_in_min: int, travel_min: int | None) -> ArrivalAssessment:
    return assess_arrival(
        now=_NOW,
        event_start=_NOW + timedelta(minutes=event_in_min),
        travel_seconds=None if travel_min is None else travel_min * 60,
        buffer=_BUFFER,
        lead_window=_LEAD,
    )


def test_unknown_when_no_travel() -> None:
    a = _assess(event_in_min=120, travel_min=None)
    assert a.verdict is ArrivalVerdict.UNKNOWN
    assert a.leave_by is None
    assert a.slack_seconds is None
    assert a.arrival_deadline == _NOW + timedelta(minutes=120)  # el evento sí se conoce


def test_on_time_with_ample_slack() -> None:
    a = _assess(event_in_min=180, travel_min=30)  # leave_by en 140 min → slack 140 > lead 30
    assert a.verdict is ArrivalVerdict.ON_TIME
    assert a.slack_seconds == 140 * 60


def test_leave_now_inside_lead_window() -> None:
    a = _assess(event_in_min=50, travel_min=30)  # leave_by en 10 min → slack 10 <= lead 30
    assert a.verdict is ArrivalVerdict.LEAVE_NOW
    assert a.slack_seconds == 10 * 60


def test_leave_now_at_exact_boundary() -> None:
    a = _assess(event_in_min=70, travel_min=30)  # slack == lead_window exacto → LEAVE_NOW (<=)
    assert a.slack_seconds == int(_LEAD.total_seconds())
    assert a.verdict is ArrivalVerdict.LEAVE_NOW


def test_late_when_slack_negative() -> None:
    a = _assess(event_in_min=30, travel_min=30)  # leave_by en -10 min → LATE
    assert a.verdict is ArrivalVerdict.LATE
    assert a.slack_seconds is not None
    assert a.slack_seconds < 0


def test_leave_by_and_fields() -> None:
    a = _assess(event_in_min=120, travel_min=45)
    assert a.arrival_deadline == _NOW + timedelta(minutes=120)
    assert a.leave_by == _NOW + timedelta(minutes=120) - timedelta(minutes=45) - _BUFFER
    assert a.travel_seconds == 45 * 60
    assert a.mode is TravelMode.DRIVING
