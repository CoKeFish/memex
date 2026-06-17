"""Contrato provider-agnóstico del subsistema `webcontext` (contexto web de entidades).

Define el Protocol `WebContextProvider` (la abstracción contra la que tipan los callers) y los
tipos que viajan por él (`EntityKind`, `ProfileResult`, `WebContextUsage`). Un proveedor concreto
(`CodexWebContextProvider`, `FirecrawlProvider`) implementa este Protocol; los callers NUNCA tipan
contra la clase concreta — calca la convención de `memex.geo.client.GeoProvider` /
`memex.llm.client.LLMClient`.

El subsistema es TRANSVERSAL: lo consumirán varios módulos (identidades, finanzas, calendario) y
Hermes. Por eso NO importa ni conoce ningún dominio; su moneda de cambio es `EntityProfile`
(genérico, definido en `schema.py`). Acotado a orgs/productos — NUNCA personas.

`ProfileResult` separa el perfil (el dato, `EntityProfile`, que ya lleva sus `sources`) de la
metadata operativa (qué proveedor respondió, latencia, tokens, salida cruda para debug), igual que
`GeocodeResult`/`LLMResult` separan dato y metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from memex.webcontext.schema import EntityProfile

#: Tipos de entidad que el subsistema perfila. Cerrado a propósito: NUNCA personas
#: (privacidad/desperdicio). El caller manda el `kind`; el proveedor lo respeta.
EntityKind = Literal["organizacion", "producto"]


@dataclass(frozen=True)
class WebContextUsage:
    """Tokens reportados por el proveedor, si los hay (telemetría best-effort).

    Tipo propio (NO `memex.llm.client.LLMUsage`) para no acoplar webcontext a la capa LLM.
    `cached_input_tokens` es subconjunto de `input_tokens`; `reasoning_tokens` ya está incluido en
    `output_tokens`. Firecrawl no reporta tokens del LLM de extracción → queda `None` en el result.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class ProfileResult:
    """Perfil de una entidad + metadata operativa de cómo se obtuvo.

    `profile` ya viene VALIDADO contra `EntityProfile` (la garantía de formato). `provider` es el
    nombre del proveedor que sirvió (útil con la cadena de fallback). `raw` es la salida cruda
    acotada, solo para debug.
    """

    profile: EntityProfile
    provider: str
    latency_ms: int
    tokens: WebContextUsage | None = None
    raw: str | None = None


class WebContextError(Exception):
    """Base de todos los errores del subsistema — los callers la atrapan genérica.

    `status_code` es el HTTP status cuando aplica, o 0 para errores lógicos / de configuración.
    Mismo shape que `GeoError`/`LLMError`.
    """

    def __init__(self, status_code: int, message: str, body: str | None = None) -> None:
        super().__init__(f"webcontext error {status_code}: {message}")
        self.status_code = status_code
        self.body = body


class WebContextConfigError(WebContextError):
    """Config inválida o falta la env var de la API key (p. ej. firecrawl sin `FIRECRAWL_API_KEY`).

    `status_code=0`. Es de build-time: el chain-builder la usa para descartar un proveedor sin
    credenciales mientras quede otro en la cadena (calca `registry.build_llm_client`).
    """

    def __init__(self, message: str) -> None:
        super().__init__(0, message)


class WebContextProviderError(WebContextError):
    """Fallo REAL del backend: codex exit!=0/timeout/sesión muerta, o firecrawl 4xx-no-cuota/red."""


class WebContextQuotaError(WebContextProviderError):
    """Cuota/rate-limit del proveedor agotada (HTTP 429 / límite de suscripción) — NO reintentable.

    Calca `GeoQuotaError`/`LLMQuotaError`: justifica saltar al siguiente proveedor de la cadena.
    """


class WebContextNotFoundError(WebContextError):
    """No se encontró contexto de la entidad — vacío legítimo, no un fallo de operación.

    Se distingue de `WebContextProviderError` (calca `GeoNotFoundError`). `status_code=0`.
    """

    def __init__(self, query: str) -> None:
        super().__init__(0, f"no web context for {query!r}")
        self.query = query


class WebContextFormatError(WebContextProviderError):
    """La salida del proveedor no validó contra `EntityProfile` (garantía de formato rota).

    El proveedor reintenta una vez (`format_retries`) antes de propagarla; los errores de validación
    de Pydantic van truncados en `body`. Subclasea `ProviderError`: fue el backend quien rompió el
    contrato.
    """


@runtime_checkable
class WebContextProvider(Protocol):
    """Proveedor de contexto web, agnóstico. `name` lo identifica (logging/registry)."""

    name: ClassVar[str]

    async def search(self, name: str, kind: EntityKind) -> ProfileResult:
        """Busca la entidad en la web y devuelve su perfil VALIDADO contra `EntityProfile`.

        `WebContextNotFoundError` si no hay entidad; `WebContextQuotaError` si la cuota se agotó;
        `WebContextProviderError` ante fallo real del backend; `WebContextFormatError` si la salida
        no valida tras el retry interno.
        """
        ...

    async def aclose(self) -> None:
        """Cierra recursos (el cliente HTTP de firecrawl). codex es no-op."""
        ...
