"""Dedup determinista FASE 1 de calendar (ADR-015 §4): función PURA, sin DB ni LLM.

Marca PARES de eventos que podrían ser el mismo (posibles duplicados) para que la FASE 2
(desambiguación LLM por par ambiguo, diferida) los resuelva. NUNCA fusiona ni borra: ambos
eventos coexisten; acá solo se decide si forman un par candidato.

Criterio (conservador — marca de más, nunca de menos): un par es candidato si hay
**solapamiento temporal** Y (**título similar** O **lugar similar**). El solapamiento temporal
solo NO alcanza ("mismo horario ≠ mismo evento"): siempre se exige además similitud de texto.

La similitud reusa `memex.modules.contract.normalize` (casefold + colapso de whitespace) +
`difflib.SequenceMatcher` (stdlib, determinista, cero dependencias).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from difflib import SequenceMatcher

from memex.modules.contract import normalize

DEFAULT_OVERLAP_TOLERANCE = timedelta(minutes=30)
DEFAULT_EVENT_DURATION = timedelta(minutes=60)
DEFAULT_TITLE_THRESHOLD = 0.85
DEFAULT_LOCATION_THRESHOLD = 0.85


@dataclass(frozen=True)
class DedupRow:
    """Un evento a comparar. Fecha/hora naive; `start_time None` ⇒ sin hora (todo el día)."""

    event_id: int
    title: str
    location: str
    starts_on: date
    ends_on: date | None
    start_time: time | None
    end_time: time | None


@dataclass(frozen=True)
class DedupPair:
    """Par candidato canónico (`a_id < b_id`) con la razón que lo disparó y su score."""

    a_id: int
    b_id: int
    reason: str  # 'time+title' | 'time+location'
    score: float


def _date_range(row: DedupRow) -> tuple[date, date]:
    return row.starts_on, row.ends_on or row.starts_on


def _is_single_day_timed(row: DedupRow) -> bool:
    return row.ends_on is None and row.start_time is not None


def _timed_interval(row: DedupRow, default_duration: timedelta) -> tuple[datetime, datetime]:
    assert row.start_time is not None  # garantizado por _is_single_day_timed
    start = datetime.combine(row.starts_on, row.start_time)
    if row.end_time is not None:
        end = datetime.combine(row.starts_on, row.end_time)
        if end <= start:  # end < start (datos raros / cruza medianoche) → duración por defecto
            end = start + default_duration
    else:
        end = start + default_duration
    return start, end


def _temporal_overlap(
    a: DedupRow, b: DedupRow, *, tolerance: timedelta, default_duration: timedelta
) -> bool:
    """¿Se solapan en el tiempo? Días disjuntos ⇒ no. Si ambos son de un solo día CON hora,
    se refina con los intervalos horarios (± tolerancia); si alguno es todo-el-día o multi-día,
    alcanza con que se toquen los rangos de fecha."""
    a_lo, a_hi = _date_range(a)
    b_lo, b_hi = _date_range(b)
    if a_lo > b_hi or b_lo > a_hi:  # rangos de fecha disjuntos
        return False

    if _is_single_day_timed(a) and _is_single_day_timed(b):
        a_start, a_end = _timed_interval(a, default_duration)
        b_start, b_end = _timed_interval(b, default_duration)
        return a_start <= b_end + tolerance and b_start <= a_end + tolerance

    return True  # todo-el-día o multi-día: alcanza el solape de fechas


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def _evaluate_pair(
    a: DedupRow,
    b: DedupRow,
    *,
    tolerance: timedelta,
    default_duration: timedelta,
    title_threshold: float,
    location_threshold: float,
) -> DedupPair | None:
    if not _temporal_overlap(a, b, tolerance=tolerance, default_duration=default_duration):
        return None

    title_sim = _similarity(a.title, b.title)
    title_match = title_sim >= title_threshold

    location_sim = 0.0
    if a.location.strip() and b.location.strip():
        location_sim = _similarity(a.location, b.location)
    location_match = location_sim >= location_threshold and location_sim > 0.0

    if not (title_match or location_match):
        return None

    reason = "time+title" if title_match else "time+location"
    score = max(title_sim, location_sim)
    lo, hi = (a, b) if a.event_id < b.event_id else (b, a)
    return DedupPair(a_id=lo.event_id, b_id=hi.event_id, reason=reason, score=round(score, 3))


def mark_duplicates(
    new_rows: Sequence[DedupRow],
    existing_rows: Sequence[DedupRow],
    *,
    overlap_tolerance: timedelta = DEFAULT_OVERLAP_TOLERANCE,
    default_duration: timedelta = DEFAULT_EVENT_DURATION,
    title_threshold: float = DEFAULT_TITLE_THRESHOLD,
    location_threshold: float = DEFAULT_LOCATION_THRESHOLD,
) -> list[DedupPair]:
    """Devuelve los pares candidatos de duplicado, comparando los eventos NUEVOS entre sí y
    contra los EXISTENTES (nunca existentes contra existentes — ya se compararon en su corrida).

    Pura y determinista: pares canónicos (`a_id < b_id`), ordenados por `(a_id, b_id)`.
    """
    pairs: list[DedupPair] = []

    def evaluate(a: DedupRow, b: DedupRow) -> None:
        pair = _evaluate_pair(
            a,
            b,
            tolerance=overlap_tolerance,
            default_duration=default_duration,
            title_threshold=title_threshold,
            location_threshold=location_threshold,
        )
        if pair is not None:
            pairs.append(pair)

    for i in range(len(new_rows)):
        for j in range(i + 1, len(new_rows)):
            evaluate(new_rows[i], new_rows[j])
        for existing in existing_rows:
            evaluate(new_rows[i], existing)

    pairs.sort(key=lambda p: (p.a_id, p.b_id))
    return pairs
