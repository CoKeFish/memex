"""build_jobs: resolución del CSV de jobs habilitados (apagado por default)."""

from __future__ import annotations

from memex.scheduler.config import SchedulerSettings, build_jobs


def test_enabled_jobs_empty_is_disarmed() -> None:
    # Default: enabled_jobs vacío → ningún job (el daemon idlea).
    assert build_jobs(SchedulerSettings(enabled_jobs="")) == []


def test_build_jobs_respects_csv_and_interval_override() -> None:
    settings = SchedulerSettings(enabled_jobs="classify,calendar", interval_classify="PT5M")
    jobs = build_jobs(settings)
    assert [j.name for j in jobs] == ["classify", "calendar"]
    classify = next(j for j in jobs if j.name == "classify")
    assert classify.default_interval == "PT5M"  # override aplicado


def test_build_jobs_skips_unknown_job() -> None:
    jobs = build_jobs(SchedulerSettings(enabled_jobs="classify,bogus"))
    assert [j.name for j in jobs] == ["classify"]


def test_build_jobs_skips_bad_interval() -> None:
    # Job real (registrado) con intervalo ISO inválido → rama bad_interval (no unknown_job).
    jobs = build_jobs(SchedulerSettings(enabled_jobs="classify", interval_classify="not-iso"))
    assert jobs == []
