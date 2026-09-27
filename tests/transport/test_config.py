"""Resolución de TransportSettings → TransportConfig."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from memex.geo.client import TravelMode
from memex.transport.config import TransportConfig, TransportSettings


def test_from_env_resolves_types() -> None:
    cfg = TransportConfig.from_env(
        TransportSettings(
            mode="walking",
            buffer_min=5,
            lead_min=20,
            compute_window_min=90,
            horizon_hours=12,
            tz="America/Bogota",
        )
    )
    assert cfg.mode is TravelMode.WALKING
    assert cfg.buffer == timedelta(minutes=5)
    assert cfg.lead_window == timedelta(minutes=20)
    assert cfg.compute_window == timedelta(minutes=90)
    assert cfg.horizon == timedelta(hours=12)
    assert cfg.tz == ZoneInfo("America/Bogota")


def test_defaults() -> None:
    cfg = TransportConfig.from_env(TransportSettings())
    assert cfg.mode is TravelMode.DRIVING
    assert cfg.buffer == timedelta(minutes=10)
    assert cfg.lead_window == timedelta(minutes=30)
    assert cfg.compute_window == timedelta(minutes=120)
    assert cfg.horizon == timedelta(hours=24)
    assert cfg.tz == ZoneInfo("America/Bogota")
