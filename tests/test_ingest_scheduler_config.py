"""IngestSchedulerSettings: parseo del env (bootstrap del daemon de ingesta)."""

from __future__ import annotations

import pytest

from memex.ingest_scheduler.config import IngestSchedulerSettings


def test_defaults() -> None:
    # `_env_file=None` → hermético (no lee un .env del repo).
    s = IngestSchedulerSettings(_env_file=None)
    assert s.user_id == 1
    assert s.tick_seconds == 5.0


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMEX_INGEST_SCHEDULER_USER_ID", "7")
    monkeypatch.setenv("MEMEX_INGEST_SCHEDULER_TICK_SECONDS", "2.5")
    s = IngestSchedulerSettings(_env_file=None)
    assert s.user_id == 7
    assert s.tick_seconds == 2.5
