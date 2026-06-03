"""`IdentityItem` — una mención de identidad extraída (extraction_schema de identidades).

Extiende `ExtractionItem` (atribución `source_inbox_ids` + `evidence`) con los campos de una
identidad mencionada. `extra="forbid"`: un campo que el LLM invente fuera de este shape invalida
el item (se descarta + loguea) — mitigación de alucinación (ADR-015 §10).

`kind` se elige de una lista cerrada (default 'unknown' si el LLM omite o devuelve algo fuera de la
lista, así no se descarta la mención); `email` se normaliza a minúsculas; `confidence` se acota a
[0, 1].
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, field_validator

from memex.modules.contract import ExtractionItem

#: Tipos válidos de identidad mencionada (espejo de `mentioned_kind` en la migración 0027).
IDENTITY_KINDS: tuple[str, ...] = ("persona", "organizacion", "producto", "agente", "unknown")
_KIND_SET = frozenset(IDENTITY_KINDS)

IdentityKind = Literal["persona", "organizacion", "producto", "agente", "unknown"]


class IdentityItem(ExtractionItem):
    """Una identidad mencionada: nombre + tipo (+ email/handle/org/rol opcionales)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: IdentityKind = "unknown"
    email: str | None = None
    handle: str | None = None
    org: str | None = None
    role: str | None = None
    confidence: float = 0.5

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, v: object) -> str:
        """Tipo fuera de la lista → 'unknown' (no descarta la mención por un tipo inválido)."""
        s = str(v or "").strip().lower()
        return s if s in _KIND_SET else "unknown"

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, v: object) -> str | None:
        """Email a minúsculas sin espacios; vacío → None."""
        s = str(v or "").strip().lower()
        return s or None

    @field_validator("handle", "org", "role", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> str | None:
        """Strings vacíos → None (el LLM a veces devuelve '')."""
        s = str(v or "").strip()
        return s or None

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v: object) -> float:
        """Acota a [0, 1]; valores no numéricos → 0.5 (no descarta la mención)."""
        if isinstance(v, bool):
            return 0.5
        if isinstance(v, (int, float)):
            f = float(v)
        elif isinstance(v, str):
            try:
                f = float(v.strip())
            except ValueError:
                return 0.5
        else:
            return 0.5
        return max(0.0, min(1.0, f))
