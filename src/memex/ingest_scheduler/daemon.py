"""Loop principal del daemon de ingesta: agenda cada fuente por su `fetch_schedule` y dispara fetch.

Espeja `memex.scheduler.daemon.AsyncScheduler` (async, single-task, polling, un fetch a la vez) pero
las "tareas" son FUENTES, no jobs estáticos: el intervalo de cada una sale de `fetch_schedule` (no
del env). El reload reconcilia POR FUENTE y refresca intervalo/config en vivo. Corre las fuentes
vencidas EN SERIE dentro de cada tick; nada tumba el loop salvo SIGINT/SIGTERM (una fuente que
explota se loguea, entra en backoff y sigue). La fila `ingestion_runs` la escribe el runner.
"""

from __future__ import annotations

import asyncio
import signal
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from memex.api.fetch_runner import run_fetch_window
from memex.core.schedule import backoff_seconds, parse_duration
from memex.db import connection
from memex.logging import get_logger

_log = get_logger("memex.ingest_scheduler.daemon")


@dataclass(frozen=True)
class ScheduledSource:
    """Lo que la DB pide agendar de una fuente: identidad + cómo traerla + cada cuánto."""

    source_id: int
    source_type: str
    cfg: dict[str, Any]
    account_id: int | None
    interval_s: float


@dataclass
class _SourceRuntime:
    """Estado vivo de una fuente agendada dentro del loop (timing + backoff)."""

    source_id: int
    source_type: str
    cfg: dict[str, Any]
    account_id: int | None
    interval_s: float
    next_run_at: float
    failure_count: int = 0
    _meta: dict[str, Any] = field(default_factory=dict)


class IngestScheduler:
    """Loop async, single-task, polling-based. Una fuente a la vez."""

    def __init__(
        self,
        *,
        user_id: int,
        sources: list[ScheduledSource],
        tick_seconds: float = 5.0,
    ) -> None:
        self._user_id = user_id
        self._tick_s = tick_seconds
        self._stop = asyncio.Event()
        now = time.monotonic()
        self._runtimes: dict[int, _SourceRuntime] = {}
        for s in sources:
            # next_run_at = ahora + intervalo → sin stampede: la 1ª corrida espera un intervalo.
            self._runtimes[s.source_id] = _SourceRuntime(
                source_id=s.source_id,
                source_type=s.source_type,
                cfg=s.cfg,
                account_id=s.account_id,
                interval_s=s.interval_s,
                next_run_at=now + s.interval_s,
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
            _log.warning("ingest_scheduler.no_sources_scheduled")  # desarmado: no trae nada
        _log.info("ingest_scheduler.start", user_id=self._user_id, sources=list(self._runtimes))
        while not self._stop.is_set():
            # Control runtime: la DB manda (ingest_scheduler_settings + sources.fetch_schedule).
            await asyncio.to_thread(self._reload_sources_if_needed)
            await self._tick(time.monotonic())
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_s)
        _log.info("ingest_scheduler.stop")

    def _desired_sources(self) -> list[ScheduledSource] | None:
        """Fuentes que la DB quiere agendar ahora, o `None` ante un error de DB (no tumbar el loop).

        Semántica del master toggle: sin fila o `daemon_enabled=False` → lista vacía (idlea). A
        diferencia del scheduler de procesamiento NO hay bootstrap por env que conservar, así que
        "sin fila" = apagado (no `None`). `None` se reserva a un error de DB → conservar runtimes.
        """
        try:
            with connection() as conn:
                srow = (
                    conn.execute(
                        text(
                            "SELECT daemon_enabled FROM ingest_scheduler_settings "
                            "WHERE user_id = :uid"
                        ),
                        {"uid": self._user_id},
                    )
                    .mappings()
                    .first()
                )
                if srow is None or not srow["daemon_enabled"]:
                    return []
                rows = (
                    conn.execute(
                        text(
                            "SELECT id, type, config, account_id, fetch_schedule "
                            "FROM sources "
                            "WHERE user_id = :uid AND enabled = TRUE "
                            "AND fetch_schedule IS NOT NULL "
                            "ORDER BY id"
                        ),
                        {"uid": self._user_id},
                    )
                    .mappings()
                    .all()
                )
        except Exception as e:  # tabla no migrada / DB caída: no tumbar el daemon
            _log.warning("ingest_scheduler.settings_read_failed", error=str(e))
            return None

        desired: list[ScheduledSource] = []
        for r in rows:
            schedule = str(r["fetch_schedule"])
            try:
                interval = parse_duration(schedule)
            except ValueError:
                _log.warning(
                    "ingest_scheduler.bad_interval", source_id=int(r["id"]), schedule=schedule
                )
                continue
            desired.append(
                ScheduledSource(
                    source_id=int(r["id"]),
                    source_type=str(r["type"]),
                    cfg=dict(r["config"] or {}),
                    account_id=r["account_id"],
                    interval_s=interval,
                )
            )
        return desired

    def _reload_sources_if_needed(self) -> None:
        """Reconcilia los runtimes con lo que pide la DB.

        Preserva `next_run_at`+`failure_count` de las fuentes que siguen, y REFRESCA en vivo
        intervalo/config/tipo/cuenta (a propósito: editar el schedule de una fuente aplica sin
        reiniciar). El intervalo nuevo NO resetea `next_run_at` → arranca a regir desde el próximo
        ciclo (evita stampede/inanición); hay un lag de a lo sumo un ciclo.
        """
        desired = self._desired_sources()
        if desired is None:
            return  # error de DB: conservar lo actual (resiliente)
        new_ids = {s.source_id for s in desired}
        set_changed = new_ids != set(self._runtimes)
        now = time.monotonic()
        runtimes: dict[int, _SourceRuntime] = {}
        for spec in desired:
            existing = self._runtimes.get(spec.source_id)
            if existing is not None:
                existing.source_type = spec.source_type
                existing.cfg = spec.cfg
                existing.account_id = spec.account_id
                existing.interval_s = spec.interval_s  # aplica desde el próximo ciclo
                runtimes[spec.source_id] = existing
            else:
                runtimes[spec.source_id] = _SourceRuntime(
                    source_id=spec.source_id,
                    source_type=spec.source_type,
                    cfg=spec.cfg,
                    account_id=spec.account_id,
                    interval_s=spec.interval_s,
                    next_run_at=now + spec.interval_s,
                )
        self._runtimes = runtimes
        if set_changed:
            _log.info("ingest_scheduler.sources_reloaded", sources=sorted(new_ids))

    async def _tick(self, now: float) -> None:
        for rt in list(self._runtimes.values()):
            if self._stop.is_set():
                break
            if now < rt.next_run_at:
                continue
            await self._run_source(rt)

    async def _run_source(self, rt: _SourceRuntime) -> None:
        log = _log.bind(source_id=rt.source_id, user_id=self._user_id)
        log.info("ingest_scheduler.source.start", source_type=rt.source_type)
        try:
            # `run_fetch_window` ya envuelve la corrida en `ingestion_run(trigger='daemon')`:
            # escribe la fila de `ingestion_runs` y bindea run_id/source_id/trigger a los logs.
            stats = await run_fetch_window(
                user_id=self._user_id,
                source_id=rt.source_id,
                source_type=rt.source_type,
                cfg=rt.cfg,
                account_id=rt.account_id,
                mode="incremental",
                dry_run=False,
                trigger="daemon",
            )
        except Exception as e:  # incl. HTTPException(502/422): backoff y seguir
            rt.failure_count += 1
            backoff = backoff_seconds(rt.failure_count)
            rt.next_run_at = time.monotonic() + backoff
            log.warning(
                "ingest_scheduler.source.failed",
                failures=rt.failure_count,
                backoff_s=backoff,
                error=str(e),
            )
            return
        rt.failure_count = 0
        rt.next_run_at = time.monotonic() + rt.interval_s
        log.info("ingest_scheduler.source.end", posted=stats.posted, inserted=stats.inserted)
