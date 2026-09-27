"""Dedup determinista FASE 1 de calendar (ADR-015 §4): función PURA, sin DB ni LLM.

Marca PARES de eventos que podrían ser el mismo (posibles duplicados) para que la FASE 2
(desambiguación LLM por par ambiguo, diferida) los resuelva. NUNCA fusiona ni borra: ambos
eventos coexisten; acá solo se decide si forman un par candidato.

Criterio (conservador — marca de más, nunca de menos): un par es candidato si hay
**solapamiento temporal** Y (**título similar** O **lugar similar**). El solapamiento temporal
solo NO alcanza ("mismo horario ≠ mismo evento"): siempre se exige además similitud de texto.

Recalibración (2026-09-08, caso Britano): tres correos del mismo instituto produjeron «Inicio de
clases intensivo de inglés», «Inicio de clases de inglés» e «Inicio de clases Intensivo» el 2 de
septiembre a las 15:10 y NINGUNO llegó al juez porque los títulos daban 0,84 (< 0,85) y los lugares
estaban escritos distinto. La compuerta debe marcar DE MÁS y dejar que la FASE 2 (codex) decida:
- **Misma hora** (mismo día, `start_time` a ≤ 30 min): alcanza un texto DÉBIL — título o lugar
  ≥ `weak_text_threshold` (0,5) o UN token distintivo en común (≥ 5 letras, no numérico:
  «clases», «inglés», «vuelo»). Razón `same-start+weak-text`.
- **Mismo día, uno todo-el-día**: título ≥ `allday_title_threshold` (0,7), o DOS tokens distintivos
  en común, o UN token + lugar parecido (≥ 0,5). Razón `same-day+weak-title`.
«Dentista» vs «Cena con Ana» a la misma hora sigue sin ser par (ni texto ni tokens).

La similitud reusa `memex.modules.contract.normalize` (casefold + colapso de whitespace) +
`difflib.SequenceMatcher` (stdlib, determinista, cero dependencias).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from difflib import SequenceMatcher

from memex.modules.calendar.recurrence import is_recurring, occurs_on
from memex.modules.contract import normalize

DEFAULT_OVERLAP_TOLERANCE = timedelta(minutes=30)
DEFAULT_EVENT_DURATION = timedelta(minutes=60)
DEFAULT_TITLE_THRESHOLD = 0.85
DEFAULT_LOCATION_THRESHOLD = 0.85
#: Texto DÉBIL que alcanza cuando la coincidencia temporal es exacta (mismo día y misma hora).
DEFAULT_WEAK_TEXT_THRESHOLD = 0.5
#: Título mínimo cuando coinciden el día pero uno de los dos es todo-el-día (sin hora que comparar).
DEFAULT_ALLDAY_TITLE_THRESHOLD = 0.7
#: Largo mínimo de un token para contar como «distintivo» («clases», «inglés»; no «de», «la»).
_DISTINCTIVE_TOKEN_LEN = 5
_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


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
    #: Líneas RFC 5545 del MAESTRO recurrente (vacío = evento único). Un maestro se compara
    #: PROYECTADO a la fecha del otro evento (si tiene ocurrencia ese día); dos maestros, por su
    #: primera ocurrencia y la misma regla.
    recurrence: tuple[str, ...] = ()


def _rule_key(rec: Sequence[str]) -> str:
    return ";".join(sorted(str(x).strip().upper() for x in rec))


def project_pair(a: DedupRow, b: DedupRow) -> tuple[DedupRow, DedupRow] | None:
    """Cómo comparar dos filas cuando alguna es un maestro recurrente:
    - ninguno recurrente → tal cual;
    - uno recurrente → si tiene ocurrencia el día del otro, se proyecta a ese día (misma hora);
      si no, NO son par;
    - ambos recurrentes → par solo si comparten la regla (misma RRULE, misma hora): se comparan
      por su primera ocurrencia."""
    ra, rb = is_recurring(a.recurrence), is_recurring(b.recurrence)
    if not ra and not rb:
        return a, b
    if ra and rb:
        if _rule_key(a.recurrence) != _rule_key(b.recurrence):
            return None
        return replace(a, recurrence=()), replace(b, recurrence=())
    master, single = (a, b) if ra else (b, a)
    if not occurs_on(
        master.starts_on, master.recurrence, single.starts_on, start_time=master.start_time
    ):
        return None
    projected = replace(master, starts_on=single.starts_on, ends_on=None, recurrence=())
    return (projected, single) if ra else (single, projected)


@dataclass(frozen=True)
class DedupPair:
    """Par candidato canónico (`a_id < b_id`) con la razón que lo disparó y su score."""

    a_id: int
    b_id: int
    reason: str  # 'time+title' | 'time+location' | 'same-start+weak-text' | 'same-day+weak-title'
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
    a: DedupRow,
    b: DedupRow,
    *,
    tolerance: timedelta,
    default_duration: timedelta,
    timed_only: bool = False,
) -> bool:
    """¿Se solapan en el tiempo? Días disjuntos ⇒ no. Si ambos son de un solo día CON hora,
    se refina con los intervalos horarios (± tolerancia).

    Para el caso todo-el-día / multi-día el comportamiento depende de `timed_only`:
    - `False` (dedup): alcanza con que se toquen los rangos de FECHA (marca de más a propósito; el
      dedup además exige similitud de texto, así que no genera ruido).
    - `True` (conflictos): NO cuenta como solape. Un evento todo-el-día (cumpleaños, feriado) o
      multi-día no bloquea un horario concreto, así que no "choca" con una reunión de las 10:00."""
    a_lo, a_hi = _date_range(a)
    b_lo, b_hi = _date_range(b)
    if a_lo > b_hi or b_lo > a_hi:  # rangos de fecha disjuntos
        return False

    if _is_single_day_timed(a) and _is_single_day_timed(b):
        a_start, a_end = _timed_interval(a, default_duration)
        b_start, b_end = _timed_interval(b, default_duration)
        if timed_only:
            # Choque real: dos eventos back-to-back que solo se TOCAN en el borde (una clase de
            # 10-11 y otra de 11-12) no se pisan → comparación ESTRICTA, sin tolerancia difusa.
            return a_start < b_end and b_start < a_end
        return a_start <= b_end + tolerance and b_start <= a_end + tolerance

    return not timed_only  # todo-el-día / multi-día: solape de fechas salvo que se exija hora


def temporal_overlap(
    a: DedupRow,
    b: DedupRow,
    *,
    tolerance: timedelta = DEFAULT_OVERLAP_TOLERANCE,
    default_duration: timedelta = DEFAULT_EVENT_DURATION,
    timed_only: bool = False,
) -> bool:
    """Wrapper PÚBLICO de `_temporal_overlap` (lo reusa `conflicts.py` para detectar conflictos de
    horario entre eventos consolidados, sin la similitud de texto del dedup). `timed_only=True`
    excluye los eventos todo-el-día / multi-día (no bloquean un horario → no son un conflicto)."""
    return _temporal_overlap(
        a, b, tolerance=tolerance, default_duration=default_duration, timed_only=timed_only
    )


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def distinctive_tokens(text: str) -> frozenset[str]:
    """Tokens alfabéticos de ≥ `_DISTINCTIVE_TOKEN_LEN` letras, sin acentos ni mayúsculas
    («Inicio de clases intensivo de inglés» → {inicio, clases, intensivo, ingles})."""
    return frozenset(
        t
        for t in _TOKEN_RE.findall(_strip_accents(normalize(text)))
        if len(t) >= _DISTINCTIVE_TOKEN_LEN
    )


#: Dos copias del mismo evento pueden traer la hora con un corrimiento chico (la convocatoria
#: dice 08:30 «puertas» y la confirmación 09:00 «inicio»): hasta 30 min sigue siendo «misma hora».
NEAR_START_TOLERANCE = timedelta(minutes=30)


def _same_start(a: DedupRow, b: DedupRow) -> bool:
    """Mismo día y misma hora de inicio (ambos con hora), con tolerancia de `NEAR_START_TOLERANCE`.
    Caso real (2026-09-09): «[Bogotá] Full Day Hackathon: The Next Forge» 09:00 y «The Next
    Craft Bogotá» 08:30 nunca fueron par y aparecían como un CHOQUE entre sí."""
    if a.start_time is None or b.start_time is None or a.starts_on != b.starts_on:
        return False
    delta = abs(
        datetime.combine(a.starts_on, a.start_time) - datetime.combine(b.starts_on, b.start_time)
    )
    return delta <= NEAR_START_TOLERANCE


def _same_day_one_allday(a: DedupRow, b: DedupRow) -> bool:
    """Mismo día de inicio y uno de los dos sin hora (todo el día): no hay hora que comparar."""
    return a.starts_on == b.starts_on and (a.start_time is None) != (b.start_time is None)


def _evaluate_pair(
    a: DedupRow,
    b: DedupRow,
    *,
    tolerance: timedelta,
    default_duration: timedelta,
    title_threshold: float,
    location_threshold: float,
    weak_text_threshold: float = DEFAULT_WEAK_TEXT_THRESHOLD,
    allday_title_threshold: float = DEFAULT_ALLDAY_TITLE_THRESHOLD,
) -> DedupPair | None:
    projected = project_pair(a, b)
    if projected is None:
        return None
    a, b = projected
    if not _temporal_overlap(a, b, tolerance=tolerance, default_duration=default_duration):
        return None

    title_sim = _similarity(a.title, b.title)
    title_match = title_sim >= title_threshold

    location_sim = 0.0
    if a.location.strip() and b.location.strip():
        location_sim = _similarity(a.location, b.location)
    location_match = location_sim >= location_threshold and location_sim > 0.0

    reason: str | None = None
    score = max(title_sim, location_sim)
    if title_match or location_match:
        reason = "time+title" if title_match else "time+location"
    else:
        shared = distinctive_tokens(a.title) & distinctive_tokens(b.title)
        if _same_start(a, b) and (
            title_sim >= weak_text_threshold or location_sim >= weak_text_threshold or shared
        ):
            # Misma hora exacta + señal débil de texto: que decida el juez (FASE 2).
            reason = "same-start+weak-text"
            score = max(score, weak_text_threshold)
        elif _same_day_one_allday(a, b) and (
            title_sim >= allday_title_threshold
            or len(shared) >= 2
            or (shared and location_sim >= weak_text_threshold)
        ):
            # Mismo día, uno todo-el-día, un token distintivo en común Y el mismo lugar escrito
            # parecido («Candlelight… Teatro Colón» 19:00 vs «Concierto Candlelight en el Teatro
            # Colón» todo el día): que decida el juez.
            reason = "same-day+weak-title"
            score = max(score, allday_title_threshold)
    if reason is None:
        return None

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
