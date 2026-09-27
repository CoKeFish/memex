"""Reglas determinísticas de clasificación post-ingest (tier por mensaje).

Sin LLM: mira el payload de un record y decide su `tier` (ADR-002):
  - `blacklist` — bulk / lista de correo / automático: no merece gasto LLM, solo se
    registra que llegó.
  - `batch` — default: se resume barato y en grupo más tarde.

`individual` (1 mensaje → 1 llamada) NO se asigna por reglas todavía: el default es
`batch` y la promoción dinámica a individual es vía LLM (ADR-002), fuera de alcance acá.

Las reglas operan sobre claves del payload y son kind-agnósticas: los marcadores de bulk
son de email (`list_id`, `list_unsubscribe`, `precedence`, `auto_submitted`); chat/social
no las traen y caen en `batch`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Tiers de ADR-002 — deben coincidir con el CHECK de `classifications` (migración 0005).
TIER_BLACKLIST = "blacklist"
TIER_BATCH = "batch"

#: Valores de la cabecera `Precedence` que marcan correo masivo / lista.
BULK_PRECEDENCE = frozenset({"bulk", "list", "junk"})


@dataclass(frozen=True)
class ClassificationResult:
    """Tier asignado + por qué (el `metadata` va a `classifications.metadata`)."""

    tier: str
    reason: str
    metadata: dict[str, Any]


def classify(payload: dict[str, Any]) -> ClassificationResult:
    """Asigna un tier a un mensaje según marcadores determinísticos de su payload.

    Precedencia de reglas (primera que matchea gana):
      1. `list_id` presente             → mailing list
      2. `list_unsubscribe` presente    → bulk con opt-out
      3. `precedence` ∈ BULK_PRECEDENCE → masivo declarado
      4. `auto_submitted` ≠ "no"/vacío  → auto-generado

    Cualquiera de las anteriores → `blacklist`. Si ninguna matchea → `batch` (default).
    """
    if _nonempty(payload.get("list_id")):
        return _blacklist("list_id")
    if _nonempty(payload.get("list_unsubscribe")):
        return _blacklist("list_unsubscribe")

    precedence = _as_str(payload.get("precedence")).lower()
    if precedence in BULK_PRECEDENCE:
        return _blacklist("precedence", value=precedence)

    auto = _as_str(payload.get("auto_submitted")).lower()
    if auto and auto != "no":
        return _blacklist("auto_submitted", value=auto)

    return ClassificationResult(tier=TIER_BATCH, reason="default", metadata={"rule": "default"})


def _blacklist(rule: str, *, value: str | None = None) -> ClassificationResult:
    metadata: dict[str, Any] = {"rule": rule}
    if value is not None:
        metadata["value"] = value
    return ClassificationResult(tier=TIER_BLACKLIST, reason=rule, metadata=metadata)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
