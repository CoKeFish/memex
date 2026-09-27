"""`run_log_purge` — política de retención de log_events: por default NO se borra nada; con
retención configurada se poda SOLO el ruido (debug/info) y warnings/errores quedan para siempre.

OJO: `log_events` NO está en el TRUNCATE por-test del conftest (los logs sobreviven entre tests
del mismo worker a propósito) → cada test usa nombres de evento ÚNICOS y asserta solo sobre ellos.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from memex.config import settings
from memex.db import connection
from memex.scheduler.jobs import run_log_purge


def _insert_log(*, days_ago: int, level: str, event: str) -> None:
    with connection() as conn:
        conn.execute(
            text(
                "INSERT INTO log_events (ts, level, event) "
                "VALUES (NOW() - make_interval(days => :d), :lvl, :ev)"
            ),
            {"d": days_ago, "lvl": level, "ev": event},
        )


def _events_like(prefix: str) -> set[str]:
    with connection() as conn:
        rows = conn.execute(
            text("SELECT event FROM log_events WHERE event LIKE :p"), {"p": f"{prefix}%"}
        ).all()
    return {str(r[0]) for r in rows}


def test_default_retention_is_never_delete() -> None:
    # El default de la config es 0 = nunca borrar (los logs son archivo, no caché).
    assert settings.log_persist_retention_days == 0


def test_purge_disabled_deletes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "log_persist_retention_days", 0)
    _insert_log(days_ago=400, level="info", event="purge-off.viejo-info")
    _insert_log(days_ago=400, level="error", event="purge-off.viejo-error")

    stats = run_log_purge(1)

    assert stats.deleted == 0
    assert _events_like("purge-off.") == {"purge-off.viejo-info", "purge-off.viejo-error"}


def test_purge_enabled_prunes_only_old_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Con retención N: se va SOLO debug/info más viejo que N; warnings/errores viejos y el
    ruido reciente se quedan."""
    monkeypatch.setattr(settings, "log_persist_retention_days", 30)
    _insert_log(days_ago=40, level="info", event="purge-on.info-viejo")
    _insert_log(days_ago=40, level="debug", event="purge-on.debug-viejo")
    _insert_log(days_ago=40, level="error", event="purge-on.error-viejo")
    _insert_log(days_ago=40, level="warning", event="purge-on.warning-viejo")
    _insert_log(days_ago=1, level="info", event="purge-on.info-reciente")

    stats = run_log_purge(1)

    assert stats.deleted == 2  # info-viejo + debug-viejo
    assert _events_like("purge-on.") == {
        "purge-on.error-viejo",
        "purge-on.warning-viejo",
        "purge-on.info-reciente",
    }
