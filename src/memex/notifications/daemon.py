"""Loop del daemon de notificaciones: cada `tick_seconds` corre un ciclo si la DB lo tiene prendido.

Proceso propio (`memex-notifications daemon start`; servicio `notifications` en compose, bajo
profile, apagado por default) — no un job del scheduler — porque los recordatorios necesitan un
tick corto (30 s) y el scheduler serializa jobs largos (extracción, OCR). Control runtime: la fila
`notification_settings.daemon_enabled` manda (`memex-notifications enable|disable`); sin fila o en
false, el daemon idlea. Cada ciclo que hizo algo (encoló, entregó, falló) deja su fila en
`worker_runs` (job `notifications`); los ciclos vacíos no, para no llenar la tabla.
"""

from __future__ import annotations

import asyncio
import signal
import time
from contextlib import suppress

from memex.core.schedule import backoff_seconds
from memex.db import connection
from memex.logging import get_logger
from memex.notifications.service import run_cycle
from memex.notifications.settings import NotificationsEnv, get_settings
from memex.scheduler import runs

_log = get_logger("memex.notifications.daemon")

JOB_NAME = "notifications"


class NotificationsDaemon:
    def __init__(self, env: NotificationsEnv) -> None:
        self._env = env
        self._stop = asyncio.Event()
        self._failures = 0
        self._next_at = 0.0
        self._was_enabled: bool | None = None

    def request_stop(self) -> None:
        self._stop.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:  # Windows
                with suppress(ValueError, OSError):
                    signal.signal(sig, lambda *_: loop.call_soon_threadsafe(self._stop.set))

    def _enabled(self) -> bool:
        try:
            with connection() as conn:
                enabled = get_settings(conn, self._env.user_id).daemon_enabled
        except Exception as e:  # DB caída / sin migrar: no tumbar el loop
            _log.warning("notifications.daemon.settings_read_failed", error=str(e)[:200])
            return False
        if enabled != self._was_enabled:
            _log.info("notifications.daemon.state", enabled=enabled)
            self._was_enabled = enabled
        return enabled

    async def run_forever(self) -> None:
        _log.info(
            "notifications.daemon.start",
            user_id=self._env.user_id,
            tick_s=self._env.tick_seconds,
        )
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= self._next_at and await asyncio.to_thread(self._enabled):
                await self._run_once()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._env.tick_seconds)
        _log.info("notifications.daemon.stop")

    async def _run_once(self) -> None:
        user_id = self._env.user_id
        try:
            stats = await run_cycle(user_id, env=self._env)
        except Exception as e:
            self._failures += 1
            backoff = backoff_seconds(self._failures)
            self._next_at = time.monotonic() + backoff
            run_id = runs.start_run(user_id, JOB_NAME)
            runs.finish_run(run_id, status="error", error=str(e))
            _log.warning(
                "notifications.daemon.cycle_failed",
                failures=self._failures,
                backoff_s=backoff,
                error=str(e)[:300],
            )
            return
        self._failures = 0
        self._next_at = 0.0
        if stats.did_something:
            run_id = runs.start_run(user_id, JOB_NAME)
            runs.finish_run(run_id, status="ok", stats=stats)
