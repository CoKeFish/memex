"""Loop principal del daemon: agenda plugins por su schedule y los dispara.

v1 usa intervalos simples (ISO 8601 durations como "PT5M" → 5 minutos).
No soporta cron full — eso es overkill para personal scale. Si un plugin
quiere correr cada hora, su schedule es "PT1H". Si quiere correr una vez
al día, "PT24H". Suficiente.

Si una corrida explota, el daemon loggea, marca el plugin como unhealthy
en la siguiente corrida (backoff) y sigue con los demás plugins. Nada
tumba el loop salvo SIGINT/SIGTERM.
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memex.core.schedule import backoff_seconds, parse_duration
from memex.logging import get_logger
from memex_local_client.discovery import discover_plugins
from memex_local_client.protocol import LocalPlugin
from memex_local_client.run import execute_plugin
from memex_local_client.state import State

_log = get_logger("memex_local_client.scheduler")

# `parse_duration` y `backoff_seconds` viven ahora en `memex.core.schedule` (compartidos con el
# daemon server-side `memex.scheduler`). Se re-exportan acá para no romper imports existentes.
__all__ = ["Scheduler", "parse_duration"]


@dataclass
class _PluginRuntime:
    plugin: LocalPlugin
    interval_s: float
    next_run_at: float
    failure_count: int = 0
    _meta: dict[str, Any] = field(default_factory=dict)


def _build_runtimes(
    plugins: dict[str, LocalPlugin],
    state: State,
    now: float,
    prev: dict[str, _PluginRuntime] | None = None,
) -> dict[str, _PluginRuntime]:
    """Mapea los plugins habilitados a su runtime de agenda.

    `prev` son los runtimes del tick anterior: si un plugin ya estaba agendado se REUSA su
    runtime (conserva `next_run_at` y el backoff acumulado) y solo se refresca `plugin`/
    `interval_s` por si el código o el schedule cambió. Sin esto, reconstruir en cada tick
    reseteaba `next_run_at=now` y el plugin se disparaba en CADA tick, ignorando el intervalo
    (martilleo). Un plugin recién aparecido arranca con `next_run_at=now` (corre enseguida).
    """
    prev = prev or {}
    runtimes: dict[str, _PluginRuntime] = {}
    enabled_names = {p.name for p in state.list_enabled()}
    for name, plugin in plugins.items():
        if name not in enabled_names:
            continue
        row = state.get_plugin(name)
        schedule = (row.schedule if row else None) or plugin.default_schedule
        try:
            interval = parse_duration(schedule)
        except ValueError as e:
            _log.warning(
                "memex_local_client.scheduler.bad_schedule",
                plugin=name,
                schedule=schedule,
                error=str(e),
            )
            continue
        existing = prev.get(name)
        if existing is not None:
            existing.plugin = plugin
            existing.interval_s = interval
            runtimes[name] = existing
        else:
            runtimes[name] = _PluginRuntime(plugin=plugin, interval_s=interval, next_run_at=now)
    return runtimes


class Scheduler:
    """Loop principal. Single-thread, polling-based.

    Recibe el URL del gateway y el token al construirse; por cada dispatch
    el wrapper `execute_plugin` levanta un GatewayClient específico al plugin
    que está corriendo en ese momento. No hay cliente HTTP compartido entre
    plugins — cada uno tiene el suyo y vive solo el tiempo de su corrida.
    """

    def __init__(
        self,
        *,
        state: State,
        gateway_url: str,
        api_token: str | None,
        plugins_root: Path,
        tick_seconds: float = 1.0,
    ) -> None:
        self._state = state
        self._gateway_url = gateway_url
        self._api_token = api_token
        self._plugins_root = plugins_root
        self._tick_s = tick_seconds
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(ValueError, OSError):
                signal.signal(sig, lambda *_a: self.request_stop())

    def run_once(self, plugins: Iterable[LocalPlugin] | None = None) -> None:
        """Una pasada: dispara los plugins cuyo `next_run_at` ya venció."""
        now = time.monotonic()
        if plugins is None:
            disc = discover_plugins(self._plugins_root)
            plugins_dict = disc.plugins
        else:
            plugins_dict = {p.name: p for p in plugins}
        runtimes = _build_runtimes(plugins_dict, self._state, now)
        self._tick(runtimes, now)

    def run_forever(self) -> None:
        _log.info("memex_local_client.scheduler.start")
        # Los runtimes VIVEN a través de los ticks: así el `next_run_at` que `_tick` avanza tras
        # cada corrida persiste y se respeta el intervalo. Reconstruirlos en cada tick (lo que
        # hacía `run_once`) reseteaba el timing y disparaba en cada tick (martilleo).
        runtimes: dict[str, _PluginRuntime] = {}
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                disc = discover_plugins(self._plugins_root)
                runtimes = _build_runtimes(disc.plugins, self._state, now, prev=runtimes)
                self._tick(runtimes, now)
            except Exception as e:
                _log.exception("memex_local_client.scheduler.tick_failed", exc=str(e))
            self._stop.wait(self._tick_s)
        _log.info("memex_local_client.scheduler.stop")

    def _tick(self, runtimes: dict[str, _PluginRuntime], now: float) -> None:
        for name, rt in runtimes.items():
            if now < rt.next_run_at:
                continue
            log = _log.bind(plugin=name)
            try:
                execute_plugin(
                    rt.plugin,
                    state=self._state,
                    gateway_url=self._gateway_url,
                    api_token=self._api_token,
                    plugins_root=self._plugins_root,
                )
                rt.failure_count = 0
                rt.next_run_at = now + rt.interval_s
            except Exception as e:
                rt.failure_count += 1
                backoff = backoff_seconds(rt.failure_count)
                rt.next_run_at = now + backoff
                log.warning(
                    "memex_local_client.scheduler.plugin_unhealthy",
                    failures=rt.failure_count,
                    backoff_s=backoff,
                    error=str(e),
                )
