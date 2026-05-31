"""Detección de CONFLICTOS de calendario (ADR-015 §4, slice 4): función PURA.

Distinto del dedup (que pregunta "¿son el MISMO evento?"). Acá: dos eventos CONSOLIDADOS
DISTINTOS que se **solapan en el tiempo** Y **ambos son de alta importancia** → se marcan
"pendiente de revisión" para que la persona decida (NUNCA se fusionan ni se descarta uno).
Reusa el solape temporal del dedup (`temporal_overlap`), sin la similitud de texto.

"Alta importancia" = `protected` o `priority_rank >= HIGH_IMPORTANCE_RANK` (los eventos manuales,
rank 100, cuentan; los de rank 0 por default, no). Así dos clases protegidas que chocan, o una
clase y un turno médico manual, se encolan; pero dos recordatorios triviales que coinciden, no.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, time

from memex.modules.calendar.dedup import DedupRow, temporal_overlap

#: Umbral de importancia para que un solape cuente como conflicto a revisar.
HIGH_IMPORTANCE_RANK = 50


@dataclass(frozen=True)
class ConflictEvent:
    """Un evento consolidado con lo necesario para detectar choques de horario + importancia."""

    consolidated_id: int
    starts_on: date
    ends_on: date | None
    start_time: time | None
    end_time: time | None
    priority_rank: int
    protected: bool


def _is_high_importance(e: ConflictEvent) -> bool:
    return e.protected or e.priority_rank >= HIGH_IMPORTANCE_RANK


def _as_row(e: ConflictEvent) -> DedupRow:
    # title/location vacíos: el conflicto NO mira similitud de texto, solo el solape temporal.
    return DedupRow(
        event_id=e.consolidated_id,
        title="",
        location="",
        starts_on=e.starts_on,
        ends_on=e.ends_on,
        start_time=e.start_time,
        end_time=e.end_time,
    )


def find_conflicts(events: Sequence[ConflictEvent]) -> list[tuple[int, int]]:
    """Pares canónicos `(a_id < b_id)` de eventos consolidados de alta importancia que se solapan.

    Pura y determinista (ordenada). Solo compara los de alta importancia entre sí (los triviales
    no generan conflicto a revisar)."""
    high = [e for e in events if _is_high_importance(e)]
    pairs: list[tuple[int, int]] = []
    for i in range(len(high)):
        for j in range(i + 1, len(high)):
            a, b = high[i], high[j]
            if temporal_overlap(_as_row(a), _as_row(b)):
                lo, hi = sorted((a.consolidated_id, b.consolidated_id))
                pairs.append((lo, hi))
    return sorted(pairs)
