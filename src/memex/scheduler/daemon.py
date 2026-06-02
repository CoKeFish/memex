"""Loop principal del daemon server-side: agenda los workers por intervalo y los dispara.

Espeja `memex_local_client.scheduler.Scheduler` pero ASYNC (la mayoría de los workers son async)
y server-side (escribe `worker_runs`, habla con la DB directo). Corre los jobs EN SERIE dentro de
cada tick (`await` uno a uno) — NUNCA en paralelo: los workers no están endurecidos para
concurrencia (contención DB, race del claim de OCR, rate-limit LLM), y la serialización hace el
cierre graceful trivial (la señal termina la corrida en curso y se sale en el próximo chequeo).
Nada tumba el loop salvo SIGINT/SIGTERM; un job que explota se loguea, entra en backoff y sigue.
"""

from __future__ import annotations

import asyncio
import signal
import time
from contextlib import suppress
from dataclasses import dataclass

from sqlalchemy import text

from memex.core.schedule import backoff_seconds, parse_duration
from memex.db import connection
from memex.logging import get_logger
from memex.scheduler import runs
from memex.scheduler.config import SchedulerSettings, build_jobs
from memex.scheduler.jobs import Job

_log = get_logger("memex.scheduler.daemon")


@dataclass
class _JobRuntime:
    job: Job
    interval_s: float
    next_run_at: float
    failure_count: int = 0


class AsyncScheduler:
    """Loop async, single-task, polling-based. Un job a la vez."""

    def __init__(self, *, user_id: int, jobs: list[Job], tick_seconds: float = 5.0) -> None:
        self._user_id = user_id
        self._tick_s = tick_seconds
        self._stop = asyncio.Event()
        now = time.monotonic()
        self._runtimes: dict[str, _JobRuntime] = {}
        for job in jobs:
            interval = parse_duration(job.default_interval)
            # next_run_at = ahora + intervalo → sin stampede: el primer run espera un intervalo.
            self._runtimes[job.name] = _JobRuntime(
                job=job, interval_s=interval, next_run_at=now + interval
            )

    def request_stop(self) -> None:
        self._stop.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                # Windows: el event loop no soporta add_signal_handler. Fallback a signal.signal
                # para capturar al menos Ctrl-C (SIGINT) y pedir stop de forma thread-safe.
                with suppress(ValueError, OSError):
                    signal.signal(sig, lambda *_: loop.call_soon_threadsafe(self._stop.set))

    async def run_forever(self) -> None:
        if not self._runtimes:
            _log.warning("scheduler.no_jobs_enabled")  # desarmado: no procesa nada
        _log.info("scheduler.start", user_id=self._user_id, jobs=list(self._runtimes))
        while not self._stop.is_set():
            # Control runtime: la DB (scheduler_settings) manda; el env solo fue el bootstrap.
            await asyncio.to_thread(self._reload_jobs_if_needed)
            await self._tick(time.monotonic())
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_s)
        _log.info("scheduler.stop")

    def _desired_jobs(self) -> list[Job] | None:
        """Jobs que la DB quiere correr ahora, o `None` si no hay fila (conservar el bootstrap env).

        `daemon_enabled=False` → lista vacía (idlea). Resiliente: un error de DB (tabla ausente o
        caída) loguea y devuelve `None` para no tumbar el loop.
        """
        try:
            with connection() as conn:
                row = (
                    conn.execute(
                        text(
                            "SELECT daemon_enabled, enabled_jobs "
                            "FROM scheduler_settings WHERE user_id = :uid"
                        ),
                        {"uid": self._user_id},
                    )
                    .mappings()
                    .first()
                )
        except Exception as e:  # tabla no migrada / DB caída: no tumbar el daemon
            _log.warning("scheduler.settings_read_failed", error=str(e))
            return None
        if row is None:
            return None
        if not row["daemon_enabled"]:
            return []
        return build_jobs(SchedulerSettings(enabled_jobs=str(row["enabled_jobs"] or "")))

    def _reload_jobs_if_needed(self) -> None:
        """Reconcilia los runtimes con lo que pide la DB; preserva el timing existente."""
        desired = self._desired_jobs()
        if desired is None:
            return  # sin fila / error: conservar lo actual
        new_names = {j.name for j in desired}
        if new_names == set(self._runtimes):
            return  # sin cambios
        now = time.monotonic()
        runtimes: dict[str, _JobRuntime] = {}
        for job in desired:
            existing = self._runtimes.get(job.name)
            if existing is not None:
                runtimes[job.name] = existing  # preserva next_run_at + failure_count
            else:
                interval = parse_duration(job.default_interval)
                runtimes[job.name] = _JobRuntime(
                    job=job, interval_s=interval, next_run_at=now + interval
                )
        self._runtimes = runtimes
        _log.info("scheduler.jobs_reloaded", jobs=sorted(new_names))

    async def _tick(self, now: float) -> None:
        for rt in self._runtimes.values():
            if self._stop.is_set():
                break
            if now < rt.next_run_at:
                continue
            await self._run_job(rt)

    async def _run_job(self, rt: _JobRuntime) -> None:
        name = rt.job.name
        log = _log.bind(job=name, user_id=self._user_id)
        run_id = runs.start_run(self._user_id, name)
        log.info("scheduler.job.start")
        try:
            stats = await rt.job.run(self._user_id)
        except Exception as e:
            rt.failure_count += 1
            backoff = backoff_seconds(rt.failure_count)
            rt.next_run_at = time.monotonic() + backoff
            runs.finish_run(run_id, status="error", error=str(e))
            log.warning(
                "scheduler.job.failed", failures=rt.failure_count, backoff_s=backoff, error=str(e)
            )
            return
        runs.finish_run(run_id, status="ok", stats=stats)
        rt.failure_count = 0
        rt.next_run_at = time.monotonic() + rt.interval_s
        log.info("scheduler.job.end")
