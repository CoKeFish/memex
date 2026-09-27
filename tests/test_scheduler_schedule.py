"""Primitivos compartidos de scheduling (puros, sin DB)."""

from __future__ import annotations

import pytest

from memex.core.schedule import backoff_seconds, parse_duration


def test_parse_duration_variants() -> None:
    assert parse_duration("PT5M") == 300
    assert parse_duration("PT1H") == 3600
    assert parse_duration("PT24H") == 86400
    assert parse_duration("P1D") == 86400
    assert parse_duration("P1DT2H30M") == 86400 + 7200 + 1800


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_duration("5 minutes")
    with pytest.raises(ValueError):
        parse_duration("PT")


def test_backoff_grows_and_caps_at_one_hour() -> None:
    assert backoff_seconds(0) == 60
    assert backoff_seconds(1) == 120
    assert backoff_seconds(2) == 240
    assert backoff_seconds(100) == 3600  # techo


def test_local_client_reexports_parse_duration() -> None:
    # El daemon de plugins re-exporta parse_duration desde memex.core.schedule.
    from memex_local_client.scheduler import parse_duration as reexported

    assert reexported is parse_duration
