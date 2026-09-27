"""El job de transporte está registrado en el scheduler con su intervalo."""

from __future__ import annotations

from memex.scheduler.jobs import all_jobs


def test_transport_registered() -> None:
    jobs = all_jobs()
    assert "transport" in jobs
    assert jobs["transport"].name == "transport"
    assert jobs["transport"].default_interval == "PT10M"
