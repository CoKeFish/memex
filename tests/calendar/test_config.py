"""CalendarSyncConfig.from_env: defaults, override por env y por arg, extra prohibido.

No hay API key acá (el token OAuth NO entra a la config; se resuelve en runtime desde el archivo
apuntado por la env var de la cuenta), así que `from_env` no falla por falta de secreto.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memex.modules.calendar.providers.config import CalendarSyncConfig

_DEFAULT_BASE = "https://www.googleapis.com/calendar/v3"


def test_from_env_default_base_url() -> None:
    cfg = CalendarSyncConfig.from_env(env={})
    assert cfg.base_url == _DEFAULT_BASE
    assert cfg.max_results == 250


def test_from_env_reads_base_url_env() -> None:
    cfg = CalendarSyncConfig.from_env(env={"MEMEX_CALENDAR_BASE_URL": "https://x.test/v3"})
    assert cfg.base_url == "https://x.test/v3"


def test_explicit_base_url_wins_over_env() -> None:
    cfg = CalendarSyncConfig.from_env(
        env={"MEMEX_CALENDAR_BASE_URL": "https://env.test/v3"}, base_url="https://arg.test/v3"
    )
    assert cfg.base_url == "https://arg.test/v3"


def test_from_env_window_defaults() -> None:
    cfg = CalendarSyncConfig.from_env(env={})
    assert cfg.sync_past_days == 183
    assert cfg.sync_future_days == 365


def test_from_env_window_override() -> None:
    cfg = CalendarSyncConfig.from_env(
        env={
            "MEMEX_CALENDAR_SYNC_PAST_DAYS": "30",
            "MEMEX_CALENDAR_SYNC_FUTURE_DAYS": "90",
        }
    )
    assert cfg.sync_past_days == 30
    assert cfg.sync_future_days == 90


def test_from_env_window_bad_value_falls_back() -> None:
    cfg = CalendarSyncConfig.from_env(env={"MEMEX_CALENDAR_SYNC_PAST_DAYS": "abc"})
    assert cfg.sync_past_days == 183


def test_config_is_frozen_and_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        CalendarSyncConfig(base_url="https://x/v3", foo=1)  # type: ignore[call-arg]
