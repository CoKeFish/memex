"""Helpers puros de cobertura temporal (timeline de días cubiertos por fuente).

Compartidos por los endpoints de cobertura — `GET /inbox/coverage` (ingesta) y
`GET /processing/coverage` (procesamiento) — que devuelven el mismo shape `CoverageOut`.
Operan sobre fechas puras (ya bucketeadas en la tz pedida por el caller) y devuelven los
dicts que esperan los schemas `CoverageRange`/`CoverageSpan`. `_resolve_tz` NO vive acá:
se copia inline en cada router a propósito (convención del repo, cf. logs.py/metrics.py).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def merge_day_buckets(buckets: list[tuple[date, int]], gap_days: int) -> list[dict[str, Any]]:
    """Funde días-bucket ORDENADOS en rangos; huecos de <= `gap_days` días se absorben.

    Días consecutivos difieren en 1; un hueco de `g` días faltantes da diferencia `g + 1`,
    de ahí el `<= gap_days + 1`.
    """
    ranges: list[dict[str, Any]] = []
    for day, n in buckets:
        if ranges and (day - ranges[-1]["end"]).days <= gap_days + 1:
            ranges[-1]["end"] = day
            ranges[-1]["count"] += n
        else:
            ranges.append({"start": day, "end": day, "count": n})
    for r in ranges:
        r["days"] = (r["end"] - r["start"]).days + 1
    return ranges


def merge_date_spans(spans: list[tuple[date, date]]) -> list[dict[str, Any]]:
    """Funde intervalos `[start, end)` que se solapen o sean adyacentes; salen INCLUSIVOS.

    Para tramos "reclamados" (barridos de ingesta, días parciales de procesamiento): son
    señales explícitas, sin tolerancia de huecos. La entrada se ordena acá; `end` exclusivo
    en la entrada (convención de backfill_jobs), inclusivo en la salida (lo que pinta el
    front, como CoverageRange).
    """
    merged: list[list[date]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [{"start": s, "end": e - timedelta(days=1), "days": (e - s).days} for s, e in merged]


def clip_date_spans(
    spans: list[tuple[date, date]], since: date | None, until: date | None
) -> list[tuple[date, date]]:
    """Recorta intervalos `[start, end)` a la ventana [since, until] (inclusiva) pedida."""
    if since is None and until is None:
        return spans
    hi_excl = until + timedelta(days=1) if until is not None else None
    out: list[tuple[date, date]] = []
    for s, e in spans:
        s2 = s if since is None else max(s, since)
        e2 = e if hi_excl is None else min(e, hi_excl)
        if e2 > s2:
            out.append((s2, e2))
    return out
