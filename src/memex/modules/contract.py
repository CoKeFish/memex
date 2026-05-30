"""Contrato de los módulos de extracción por intereses (ADR-015).

`InterestModule` es un Protocol estructural (`@runtime_checkable`) calcado de `Source`
(`memex.core.source`): ClassVars declarativos introspectables sin instanciar + métodos async.
El orquestador SIEMPRE tipa contra `InterestModule`, nunca contra la clase concreta (mismo
test de disciplina que con `Source`). Un módulo concreto vive en `memex/modules/<slug>/`.

Aislamiento (ADR-001): un módulo NUNCA importa db/llm/observability directo. El core le
inyecta lo que necesita vía `ModuleContext` (igual que un ingestor solo conoce
`memex.core.source`).

`capabilities` es un **set ABIERTO** de strings (no un enum): un módulo puede declarar una
capacidad que el core aún no conoce sin tocar este contrato. Las constantes `CAP_*` dan
typo-safety sin cerrar el conjunto.

Mitigación de alucinación (ADR-015 §10): todo item extraído extiende `ExtractionItem`, que
obliga `source_inbox_ids` (atribución por-mensaje) + `evidence` (cita). `validate_item` valida
cada item crudo del LLM contra el `extraction_schema` del módulo y descarta —sin romper el
run— lo inválido o con atribución fuera del lote (alucinada).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

from memex.core.source import HealthResult, SourceKind
from memex.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

    from memex.llm import LLMClient

_log = get_logger("memex.modules.contract")

# --- Capabilities (set ABIERTO; ver docstring) -------------------------------------- #
CAP_EXTRACT = "extract"
#: Futuras, declaradas pero SIN flujo en este slice (las ejercitan calendar/hackathones):
CAP_PROVIDE_DOMAIN = "provide_domain"
CAP_CONTRIBUTE_DOMAIN = "contribute_domain"


class ExtractionItem(BaseModel):
    """Base de TODO item extraído: atribución por-mensaje + evidencia (ADR-015 §10).

    Las subclases (ej. `ExpenseItem` de finance) agregan sus campos de dominio. `source_inbox_ids`
    deben existir en el lote de la ventana (lo verifica `validate_item`); `evidence` es una cita
    textual del mensaje original (chequeo substring barato, solo log).
    """

    model_config = ConfigDict(frozen=True)

    source_inbox_ids: tuple[int, ...]
    evidence: str = ""


@dataclass(frozen=True)
class ModuleContext:
    """Lo que el core inyecta a un módulo para una ventana (un lote de mensajes).

    - `conn`: conexión con tx ABIERTA por el orquestador; `persist` escribe acá (NO abre otra),
      para que filas + cursor de idempotencia sean atómicos por ventana.
    - `llm`: el Protocol `LLMClient` (inyectable en tests), nunca el cliente concreto.
    - `deps`: handles tipados de las dependencias del módulo. VACÍO ({}) en este slice (seam).
    - `summary_id`: None en este slice (la extracción no depende del resumen; ADR-015 §9).
    - `inbox_ids`: los ids del lote — base de la atribución (`source_inbox_ids ⊆ inbox_ids`).
    """

    user_id: int
    conn: Connection
    llm: LLMClient
    deps: Mapping[str, object]
    summary_id: int | None
    inbox_ids: tuple[int, ...]


@runtime_checkable
class InterestModule(Protocol):
    """Contrato estructural de un módulo de extracción. Calca `Source` (ADR-009)."""

    slug: ClassVar[str]
    """id único; key del registry y prefijo de las tablas `mod_<slug>_`."""

    interest: ClassVar[str]
    """Texto natural que se inyecta en el ruteo (Etapa A). Si ningún módulo activo lo declara,
    ese dato no se extrae."""

    extraction_schema: ClassVar[type[ExtractionItem]]
    """Modelo Pydantic (frozen) que describe el JSON que el LLM devuelve por item."""

    extraction_prompt: ClassVar[str]
    """System prompt de extracción del módulo (hand-tuned; ej. finance reusa el del spike de
    gastos). El orquestador arma el bloque de mensajes genérico y usa esto como system."""

    capabilities: ClassVar[frozenset[str]]
    """Set ABIERTO de capacidades. Todo extractor declara al menos `CAP_EXTRACT`."""

    consumes_kinds: ClassVar[frozenset[SourceKind]]
    """Filtro barato pre-LLM por categoría de fuente (email/chat/social)."""

    depends_on: ClassVar[tuple[str, ...]]
    """Slugs requeridos; el orquestador hace cierre + topo-sort. finance: ()."""

    async def persist(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        """Escribe `items` (ya validados contra `extraction_schema`) en SUS tablas `mod_<slug>_*`
        usando `ctx.conn`. Devuelve cuántas filas insertó. NO abre conexión propia."""
        ...

    async def health_check(self) -> HealthResult:
        """Igual que `Source.health_check`: nunca lanza; error → HealthResult unhealthy."""
        ...


# --- Parseo + validación del output del LLM ----------------------------------------- #


def parse_items(content: str) -> list[dict[str, Any]]:
    """Parsea defensivamente la respuesta del LLM (`{"items": [...]}`) a una lista de dicts.

    Calca `parse_facts` del spike: JSON inválido, `items` ausente o no-lista, o elementos
    no-dict → se ignoran (lista vacía / se saltean), nunca rompe.
    """
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return []
    raw = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _normalize(text: str) -> str:
    """casefold + colapso de whitespace, para el chequeo substring de `evidence`."""
    return " ".join(text.casefold().split())


def validate_item(
    schema: type[ExtractionItem],
    raw: dict[str, Any],
    *,
    lote: frozenset[int],
    rendered_by_id: Mapping[int, str] | None = None,
) -> ExtractionItem | None:
    """Valida un item crudo del LLM. Devuelve la instancia, o None (descartado + log) si:

    - no valida contra `schema` (`extra="forbid"` / tipos / requeridos),
    - `source_inbox_ids` está vacío o tiene ids FUERA del lote (atribución alucinada).

    `evidence` que no aparece (substring) en el texto de sus mensajes citados se LOGUEA pero
    NO descarta el item (evitar perder datos por acentos/normalización; default del slice).
    """
    try:
        item = schema.model_validate(raw)
    except ValidationError as exc:
        _log.warning("extract.item.invalid", errors=exc.error_count(), schema=schema.__name__)
        return None

    if not item.source_inbox_ids:
        _log.warning("extract.attribution_empty", schema=schema.__name__)
        return None
    outside = [i for i in item.source_inbox_ids if i not in lote]
    if outside:
        _log.warning("extract.attribution_miss", outside=outside, schema=schema.__name__)
        return None

    if rendered_by_id is not None and item.evidence.strip():
        needle = _normalize(item.evidence)
        haystack = _normalize(" ".join(rendered_by_id.get(i, "") for i in item.source_inbox_ids))
        if needle not in haystack:
            _log.warning(
                "extract.evidence_miss",
                schema=schema.__name__,
                source_inbox_ids=list(item.source_inbox_ids),
            )

    return item
