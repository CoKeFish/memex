"""`CalendarEventItem` — la forma de un evento/fecha extraído (extraction_schema de calendar).

Extiende `ExtractionItem` (atribución `source_inbox_ids` + `evidence`) con los campos de un
evento. `extra="forbid"`: un campo que el LLM invente fuera de este shape invalida el item
(se descarta + loguea) — mitigación de alucinación (ADR-015 §10).

Fecha/hora NAIVE (sin timezone) a propósito (ver migración 0010): `starts_on` es el ancla
(siempre presente); `start_time None` ⇒ sin hora específica. `ends_on` cubre eventos multi-día.
"""

from __future__ import annotations

from datetime import date, time

from pydantic import ConfigDict

from memex.modules.contract import ExtractionItem


class CalendarEventItem(ExtractionItem):
    """Un evento: título + fecha (+ hora/fin/lugar opcionales)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    starts_on: date
    ends_on: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    location: str = ""
    description: str = ""
