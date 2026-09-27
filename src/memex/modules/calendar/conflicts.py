"""Detección de CONFLICTOS de calendario (ADR-015 §4, slice 4): función PURA.

Distinto del dedup (que pregunta "¿son el MISMO evento?"). Acá: dos eventos CONSOLIDADOS
DISTINTOS que **chocan en el horario** Y **ambos son de alta importancia** → se marcan
"pendiente de revisión" para que la persona decida (NUNCA se fusionan ni se descarta uno).
Reusa el solape temporal del dedup (`temporal_overlap`), sin la similitud de texto.

Un conflicto exige que AMBOS sean eventos CON HORA que se solapen de verdad (tolerancia 0,
`timed_only=True`): un evento todo-el-día (cumpleaños, feriado) o multi-día no bloquea un horario
concreto, así que NO choca con una reunión de las 10:00 — si no, un solo cumpleaños generaría un
conflicto contra cada clase del día. (El dedup sí mira los todo-el-día, pero ahí el gate de
similitud de texto evita el ruido; acá no hay gate de texto.)

"Alta importancia" = `protected` o `priority_rank >= HIGH_IMPORTANCE_RANK` (los eventos manuales,
rank 100, cuentan; los de rank 0 por default, no). Así dos clases protegidas que chocan, o una
clase y un turno médico manual, se encolan; pero dos recordatorios triviales que coinciden, no.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta

from memex.modules.calendar.dedup import DedupRow, temporal_overlap

#: Un conflicto de horario es solape REAL (sin el margen difuso de 30 min del dedup, que sirve para
#: emparejar el mismo evento a horas algo distintas, no para decidir si dos cosas se pisan).
_CONFLICT_TOLERANCE = timedelta(0)

#: Umbral de importancia para que un solape cuente como conflicto a revisar.
HIGH_IMPORTANCE_RANK = 50


@dataclass(frozen=True)
class ConflictEvent:
    """Un evento consolidado con lo necesario para detectar conflictos de horario + importancia."""

    consolidated_id: int
    starts_on: date
    ends_on: date | None
    start_time: time | None
    end_time: time | None
    priority_rank: int
    protected: bool
    #: Día concreto de la ocurrencia cuando el consolidado es un maestro recurrente expandido
    #: (None = evento único: el día es `starts_on`).
    occurs_on: date | None = None
    #: Estado (confirmado|provisional|anuncio): confirmado y provisional cuentan como importantes.
    commitment: str = "anuncio"

    @property
    def day(self) -> date:
        return self.occurs_on or self.starts_on


def _is_high_importance(e: ConflictEvent) -> bool:
    """Entra en conflicto lo que importa: lo fijado, lo de rank alto y lo que la persona tiene
    confirmado o provisional; un anuncio (convocatoria abierta) no bloquea la agenda."""
    return (
        e.protected
        or e.priority_rank >= HIGH_IMPORTANCE_RANK
        or e.commitment in ("confirmado", "provisional")
    )


def _as_row(e: ConflictEvent) -> DedupRow:
    # title/location vacíos: el conflicto NO mira similitud de texto, solo el solape temporal.
    if e.occurs_on is not None:
        # Ocurrencia de un recurrente: un solo día (la duración multi-día no aplica por ocurrencia).
        return DedupRow(
            event_id=e.consolidated_id,
            title="",
            location="",
            starts_on=e.occurs_on,
            ends_on=None,
            start_time=e.start_time,
            end_time=e.end_time,
        )
    return DedupRow(
        event_id=e.consolidated_id,
        title="",
        location="",
        starts_on=e.starts_on,
        ends_on=e.ends_on,
        start_time=e.start_time,
        end_time=e.end_time,
    )


def find_conflicts_dated(events: Sequence[ConflictEvent]) -> list[tuple[int, int, date]]:
    """Choques `(a_id < b_id, día)` entre eventos consolidados de alta importancia. Un maestro
    recurrente entra EXPANDIDO (una `ConflictEvent` por ocurrencia, con `occurs_on`): dos
    ocurrencias del MISMO consolidado nunca chocan entre sí. Por par se conserva el primer día."""
    high = [e for e in events if _is_high_importance(e)]
    first_day: dict[tuple[int, int], date] = {}
    for i in range(len(high)):
        for j in range(i + 1, len(high)):
            a, b = high[i], high[j]
            if a.consolidated_id == b.consolidated_id:
                continue
            if temporal_overlap(
                _as_row(a), _as_row(b), tolerance=_CONFLICT_TOLERANCE, timed_only=True
            ):
                lo, hi = sorted((a.consolidated_id, b.consolidated_id))
                day = max(a.day, b.day)
                key = (lo, hi)
                if key not in first_day or day < first_day[key]:
                    first_day[key] = day
    return sorted((a, b, d) for (a, b), d in first_day.items())


def find_conflicts(events: Sequence[ConflictEvent]) -> list[tuple[int, int]]:
    """Pares canónicos `(a_id < b_id)` de eventos consolidados de alta importancia que se solapan.

    Pura y determinista (ordenada). Solo compara los de alta importancia entre sí (los triviales
    no generan conflicto a revisar)."""
    return [(a, b) for a, b, _ in find_conflicts_dated(events)]
