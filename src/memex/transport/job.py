"""Wrapper del daemon de transporte para el scheduler: arma el contexto y corre una pasada.

Adapta `run_transport_for_user` a la firma uniforme del Job (`async (user_id) -> stats`): construye
`cfg`/`now`/`provider`/`notifier`, delega y cierra el provider. Best-effort ante errores del
proveedor de mapas (`GeoError`): los loguea y devuelve stats con la razón, sin tumbar el daemon
(calca `run_calendar_cycle`). El `Notifier` sale de `build_notifier()` → hoy el stub que loguea;
cuando exista el servicio real, esa factory cambia y este job no se toca.
"""

from __future__ import annotations

from datetime import datetime

from memex.geo.client import GeoError
from memex.geo.providers import build_provider_from_env
from memex.logging import get_logger
from memex.notifications import build_notifier
from memex.transport.config import TransportConfig
from memex.transport.service import TransportStats, run_transport_for_user

_log = get_logger("memex.transport.job")


async def run_transport_check(user_id: int) -> TransportStats:
    """Una pasada del daemon de transporte para `user_id` (la corrida que agenda el scheduler)."""
    cfg = TransportConfig.from_env()
    now = datetime.now(cfg.tz)
    provider = build_provider_from_env()
    notifier = build_notifier()
    try:
        return await run_transport_for_user(
            user_id=user_id, provider=provider, notifier=notifier, cfg=cfg, now=now
        )
    except GeoError as e:  # best-effort: un fallo del proveedor no debe tumbar el daemon
        _log.warning("transport.provider_error", error=str(e))
        return TransportStats(reason="provider_error")
    finally:
        await provider.aclose()
