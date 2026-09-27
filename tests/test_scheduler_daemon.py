"""Loop del daemon async: tick, backoff, supervivencia a errores, cierre por stop.

worker_runs se stubea (este archivo testea la lógica del loop, no la persistencia — eso lo cubre
test_scheduler_runs.py). Workers reemplazados por fakes async/sync; cero datos reales.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from memex.llm.client import LLMQuotaError
from memex.scheduler.daemon import AsyncScheduler
from memex.scheduler.jobs import Job, _sync


@pytest.fixture(autouse=True)
def _stub_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memex.scheduler.runs.start_run", lambda uid, job: 1)
    monkeypatch.setattr("memex.scheduler.runs.finish_run", lambda run_id, **kw: None)


def _sched(jobs: list[Job]) -> AsyncScheduler:
    return AsyncScheduler(user_id=1, jobs=jobs, tick_seconds=0.01)


@pytest.mark.asyncio
async def test_tick_runs_due_job() -> None:
    calls: list[int] = []

    async def run(uid: int) -> None:
        calls.append(uid)

    sched = _sched([Job("fake", "PT1H", run)])
    sched._runtimes["fake"].next_run_at = 0.0  # forzar vencido
    await sched._tick(time.monotonic())
    assert calls == [1]


@pytest.mark.asyncio
async def test_tick_skips_not_due_job() -> None:
    calls: list[int] = []

    async def run(uid: int) -> None:
        calls.append(uid)

    # next_run_at = now + interval (PT1H) → no vencido al primer tick.
    sched = _sched([Job("fake", "PT1H", run)])
    await sched._tick(time.monotonic())
    assert calls == []


@pytest.mark.asyncio
async def test_job_failure_triggers_backoff_and_loop_survives() -> None:
    async def boom(uid: int) -> None:
        raise RuntimeError("x")

    sched = _sched([Job("fake", "PT1H", boom)])
    rt = sched._runtimes["fake"]
    rt.next_run_at = 0.0
    await sched._tick(time.monotonic())  # NO debe propagar
    assert rt.failure_count == 1
    assert rt.next_run_at > time.monotonic()  # reprogramado con backoff


@pytest.mark.asyncio
async def test_quota_error_does_not_kill_tick() -> None:
    async def quota(uid: int) -> None:
        raise LLMQuotaError(402, "sin saldo")

    sched = _sched([Job("fake", "PT1H", quota)])
    sched._runtimes["fake"].next_run_at = 0.0
    await sched._tick(time.monotonic())  # se trata como error genérico → loop vivo
    assert sched._runtimes["fake"].failure_count == 1


@pytest.mark.asyncio
async def test_sync_worker_wrapped_runs_off_thread() -> None:
    seen: list[int] = []

    def sync_fn(uid: int) -> None:
        seen.append(uid)

    sched = _sched([Job("s", "PT1H", _sync(sync_fn))])
    sched._runtimes["s"].next_run_at = 0.0
    await sched._tick(time.monotonic())
    assert seen == [1]


@pytest.mark.asyncio
async def test_stop_event_exits_run_forever() -> None:
    ran: list[int] = []

    async def run(uid: int) -> None:
        ran.append(uid)

    sched = _sched([Job("fake", "PT1H", run)])
    sched.request_stop()  # stop ya seteado → run_forever sale sin correr nada
    await asyncio.wait_for(sched.run_forever(), timeout=1.0)
    assert ran == []


@pytest.mark.asyncio
async def test_no_jobs_idles_without_error() -> None:
    sched = _sched([])  # desarmado
    sched.request_stop()
    await asyncio.wait_for(sched.run_forever(), timeout=1.0)


def test_init_uses_caller_provided_args() -> None:
    # Smoke de construcción sin event loop: runtimes poblados, next_run_at en el futuro.
    async def run(uid: int) -> Any:
        return None

    sched = AsyncScheduler(user_id=7, jobs=[Job("fake", "PT1H", run)], tick_seconds=1.0)
    assert set(sched._runtimes) == {"fake"}
    assert sched._runtimes["fake"].next_run_at > time.monotonic()
