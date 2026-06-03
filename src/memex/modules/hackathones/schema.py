"""`HackathonItem` — la forma de un hackatón extraído (extraction_schema de hackathones).

Extiende `ExtractionItem` (atribución `source_inbox_ids` + `evidence`) con los campos de un
hackatón / competencia de programación. `extra="forbid"`: un campo que el LLM invente fuera de este
shape invalida el item (se descarta + loguea) — mitigación de alucinación (ADR-015 §10).

Decisiones de forma:
- Las fechas son OPCIONALES: los anuncios suelen liderar con el deadline de inscripción y a veces
  omiten la fecha del evento; un hackatón con solo `name` + deadline + url sigue siendo útil (el
  enriquecimiento futuro completa huecos). `name` es el único campo de dominio obligatorio.
- `modality` se elige de una lista cerrada (default `desconocido` si el LLM omite o devuelve algo
  fuera de la lista, así no se descarta el hackatón) — espejo del normalizador de `category` de
  finance.
- `technologies`/`prizes`/`requirements` son texto libre en v1 (como los campos de
  finance/calendar); estructurarlos (listas, montos) queda para un slice posterior.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import ConfigDict, field_validator

from memex.modules.contract import ExtractionItem

#: Modalidades válidas. El LLM debe elegir una; fuera de la lista → 'desconocido'.
HACKATHON_MODALITIES: tuple[str, ...] = ("presencial", "online", "hibrido", "desconocido")
_MODALITY_SET = frozenset(HACKATHON_MODALITIES)

HackathonModality = Literal["presencial", "online", "hibrido", "desconocido"]


class HackathonItem(ExtractionItem):
    """Un hackatón: nombre + fechas/modalidad/lugar + tecnologías/premios/requisitos."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    starts_on: date | None = None
    ends_on: date | None = None
    registration_deadline: date | None = None
    modality: HackathonModality = "desconocido"
    location: str = ""
    url: str = ""
    organizer: str = ""
    technologies: str = ""
    prizes: str = ""
    requirements: str = ""
    description: str = ""

    @field_validator("modality", mode="before")
    @classmethod
    def _normalize_modality(cls, v: object) -> str:
        """Modalidad fuera de la lista → 'desconocido' (no descarta el hackatón por eso)."""
        s = str(v or "").strip().lower()
        return s if s in _MODALITY_SET else "desconocido"
