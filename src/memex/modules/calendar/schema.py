"""`CalendarEventItem` — la forma de un evento/fecha extraído (extraction_schema de calendar).

Extiende `ExtractionItem` (atribución `source_inbox_ids` + `evidence`) con los campos de un
evento. `extra="forbid"`: un campo que el LLM invente fuera de este shape invalida el item
(se descarta + loguea) — mitigación de alucinación (ADR-015 §10).

Fecha/hora NAIVE (sin timezone) a propósito (ver migración 0010): `starts_on` es el ancla
(siempre presente); `start_time None` ⇒ sin hora específica. `ends_on` cubre eventos multi-día.
Lo naive se interpreta como hora LOCAL del usuario; si el mensaje dice la zona (`tz`, p.ej. una
reunión «15:00 UTC»), el módulo la convierte a hora local ANTES de guardar (`memex.core.localtime`).

`commitment` (estado, 2026-09-09; antes `kind` 2026-09-05): `confirmado` | `provisional` son eventos
DE la persona (cita, clase, examen, vuelo, reunión a la que la invitan o se inscribió); `anuncio` =
convocatoria o evento ABIERTO con fecha que el mensaje anuncia (hackathon, conferencia, plazo de
inscripción, lanzamiento) aunque la persona no participe todavía. Los anuncios SÍ se extraen: un
correo relevante por un interés («hackathones») debe dejar su fecha en el mapa. Criterio general.
"""

from __future__ import annotations

from datetime import date, time

from pydantic import BaseModel, ConfigDict, field_validator

from memex.core.localtime import normalize_tz
from memex.modules.calendar.commitment import ANUNCIO, normalize_level
from memex.modules.calendar.recurrence import (
    RecurrenceError,
    RecurrenceSpec,
    normalize_day,
    normalize_freq,
    to_rrule,
)
from memex.modules.contract import ExtractionItem


class RecurrenceItem(BaseModel):
    """Regla de recurrencia que devuelve el extractor («clases de lunes a viernes hasta el 25»):
    `freq` daily|weekly|monthly|yearly, `byday` días (códigos MO..SU o nombres en español),
    `until` última fecha (opcional), `count` cantidad (opcional), `interval` cada N. Sin `until` ni
    `count` = serie SIN FIN. Se persiste como RRULE (RFC 5545), igual que Google."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    freq: str
    byday: tuple[str, ...] = ()
    until: date | None = None
    count: int | None = None
    interval: int = 1

    @field_validator("freq", mode="before")
    @classmethod
    def _norm_freq(cls, v: object) -> str:
        return normalize_freq(str(v or ""))

    @field_validator("byday", mode="before")
    @classmethod
    def _norm_days(cls, v: object) -> tuple[str, ...]:
        if v is None or v == "":
            return ()
        raw = v if isinstance(v, (list, tuple)) else str(v).replace(";", ",").split(",")
        out: list[str] = []
        for d in raw:
            code = normalize_day(str(d))
            if code not in out:
                out.append(code)
        return tuple(out)

    @field_validator("count", mode="before")
    @classmethod
    def _norm_count(cls, v: object) -> int | None:
        if v in (None, "", 0):
            return None
        return int(str(v))

    def to_spec(self) -> RecurrenceSpec:
        # until y count juntos: gana until (un fin explícito es más confiable que una cuenta).
        return RecurrenceSpec(
            freq=normalize_freq(self.freq),
            interval=max(1, int(self.interval or 1)),
            byday=self.byday,
            until=self.until,
            count=None if self.until is not None else self.count,
        )


class CalendarEventItem(ExtractionItem):
    """Un evento: título + fecha (+ hora/fin/lugar opcionales) (+ recurrencia opcional)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    starts_on: date
    ends_on: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    #: Zona EXPLÍCITA de las horas SOLO si el mensaje la dice; normalizada (`UTC`|`±HH:MM`|IANA).
    #: None = hora local del usuario.
    tz: str | None = None
    location: str = ""
    description: str = ""
    #: Regla de recurrencia (opcional). Si viene, `starts_on` es la PRIMERA ocurrencia y el evento
    #: se guarda como MAESTRO; las ocurrencias se expanden al leer (estilo Google Calendar).
    recurrence: RecurrenceItem | None = None
    #: Estado del evento para la persona, leído del TEXTO: confirmado (inscrita/pagó/confirmó),
    #: provisional (le hablan como participante) o anuncio (convocatoria abierta).
    #: Ver `commitment.py`. Faltante o raro → anuncio (no se descarta el evento).
    commitment: str = ANUNCIO
    commitment_evidence: str = ""

    @field_validator("commitment", mode="before")
    @classmethod
    def _norm_commitment(cls, v: object) -> str:
        return normalize_level(v)

    @field_validator("recurrence", mode="before")
    @classmethod
    def _norm_recurrence(cls, v: object) -> object:
        """Regla inválida o vacía → sin recurrencia (no se descarta el evento por eso)."""
        if v in (None, "", {}, []):
            return None
        if isinstance(v, dict):
            try:
                return RecurrenceItem.model_validate(v)
            except (RecurrenceError, ValueError):
                return None
        return v

    def recurrence_lines(self) -> tuple[str, ...]:
        """Las líneas RFC 5545 a persistir (vacío si el evento es único)."""
        if self.recurrence is None:
            return ()
        try:
            return (to_rrule(self.recurrence.to_spec()),)
        except RecurrenceError:
            return ()

    @field_validator("tz", mode="before")
    @classmethod
    def _normalize_tz(cls, v: object) -> str | None:
        """Zona no reconocible → None (se asume local; no se descarta el evento)."""
        return normalize_tz(v)
