"""Primitivas de scheduling compartidas: duración ISO-8601 → segundos + backoff exponencial.

Funciones puras (sin I/O ni acoplamiento a plugins). Las usan tanto el daemon de plugins del
cliente local (`memex_local_client.scheduler`) como el daemon server-side de workers
(`memex.scheduler.daemon`). Vive en `memex.core` a propósito: ADR-001 permite que el cliente
local importe `memex.core` (solo veda `memex.db/api/core.inbox/core.checkpoint`).
"""

from __future__ import annotations

import re

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


def backoff_seconds(failures: int) -> float:
    """Backoff exponencial con techo de 1h."""
    return float(min(60 * (2 ** min(failures, 6)), 3600))
