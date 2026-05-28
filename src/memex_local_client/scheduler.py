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

import re
import signal
import threading
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memex.logging import get_logger
from memex_local_client.discovery import discover_plugins
from memex_local_client.protocol import LocalPlugin
from memex_local_client.run import execute_plugin
from memex_local_client.state import State

_log = get_logger("memex_local_client.scheduler")

# ISO 8601 PnDTnHnMnS, subconjunto: PT5M, PT1H, PT24H, P1D, P1DT2H30M, etc.
_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_duration(s: str) -> float:
    """Convierte una duración ISO 8601 simple a segundos."""
    m = _DURATION_RE.match(s.strip())
    if not m or not any(m.group(g) for g in ("days", "hours", "minutes", "seconds")):
        raise ValueError(f"invalid duration: {s!r}")
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = int(m.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


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
) -> dict[str, _PluginRuntime]:
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
        runtimes[name] = _PluginRuntime(plugin=plugin, interval_s=interval, next_run_at=now)
    return runtimes


def _backoff_seconds(failures: int) -> float:
    """Backoff exponencial con techo de 1h."""
    return float(min(60 * (2 ** min(failures, 6)), 3600))


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
        while not self._stop.is_set():
            try:
                self.run_once()
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
                backoff = _backoff_seconds(rt.failure_count)
                rt.next_run_at = now + backoff
                log.warning(
                    "memex_local_client.scheduler.plugin_unhealthy",
                    failures=rt.failure_count,
                    backoff_s=backoff,
                    error=str(e),
                )
