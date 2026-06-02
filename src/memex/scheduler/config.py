"""Config del scheduler server-side (env-driven) + factory de jobs habilitados.

Apagado por default: `enabled_jobs` vacío → `build_jobs` devuelve [] y el daemon idlea sin
procesar nada. El dueño arma seteando `MEMEX_SCHEDULER_ENABLED_JOBS` (CSV) tras el backfill
controlado. Prefijo de env propio (`MEMEX_SCHEDULER_`) para no chocar con `memex.config.Settings`
(prefijo `MEMEX_`); la DB sigue saliendo de `settings.database_url`, no se duplica acá.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from memex.core.schedule import parse_duration
from memex.logging import get_logger
from memex.scheduler.jobs import Job, all_jobs

_log = get_logger("memex.scheduler.config")


class SchedulerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMEX_SCHEDULER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    user_id: int = 1
    enabled_jobs: str = ""  # CSV de nombres de job; VACÍO = desarmado (no procesa nada)
    tick_seconds: float = 5.0
    interval_classify: str = "PT15M"  # reglas, barato
    interval_summarize: str = "PT1H"  # LLM, más grueso
    interval_extract: str = "PT1H"  # LLM
    interval_ocr: str = "PT1H"  # visión, opt-in
    interval_calendar: str = "PT30M"
    interval_log_purge: str = "P1D"  # retención de log_events, diario

    def interval_for(self, job_name: str) -> str | None:
        value = getattr(self, f"interval_{job_name}", None)
        return value if isinstance(value, str) else None


def build_jobs(settings: SchedulerSettings) -> list[Job]:
    """Resuelve los jobs habilitados (CSV) contra el registry, con el intervalo configurado.

    Nombre desconocido → se saltea con `scheduler.config.unknown_job`. Intervalo ISO inválido →
    se saltea con `scheduler.config.bad_interval`. Con `enabled_jobs` vacío devuelve [] (desarmado).
    """
    registry = all_jobs()
    jobs: list[Job] = []
    for raw in settings.enabled_jobs.split(","):
        name = raw.strip()
        if not name:
            continue
        base = registry.get(name)
        if base is None:
            _log.warning("scheduler.config.unknown_job", job=name)
            continue
        interval = settings.interval_for(name) or base.default_interval
        try:
            parse_duration(interval)
        except ValueError:
            _log.warning("scheduler.config.bad_interval", job=name, interval=interval)
            continue
        jobs.append(Job(name=base.name, default_interval=interval, run=base.run))
    return jobs
