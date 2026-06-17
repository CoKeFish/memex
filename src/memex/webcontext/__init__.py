"""Subsistema `webcontext` — contexto web para entidades (org/producto, NUNCA personas).

Servicio TRANSVERSAL (lo usan identidades/finanzas/calendario y Hermes), no un módulo de dominio:
dado un nombre + tipo, busca en la web y devuelve un perfil GARANTIZADO (`EntityProfile`)
+ procedencia. Seam de proveedor (`WebContextProvider`) con dos implementaciones — codex (web search
built-in, $0) y firecrawl (search + scrape) — y fallback codex→firecrawl. Calca la FORMA de
`memex.geo`; no toca DB ni dominios.

API pública: el Protocol + tipos + errores, `EntityProfile` + helpers de schema,
`build_provider_from_env`/`resolve`, y `search_entity` (la orquestación). Los callers tipan contra
`WebContextProvider`, nunca contra el proveedor concreto.
"""

from memex.webcontext.client import (
    EntityKind,
    ProfileResult,
    WebContextConfigError,
    WebContextError,
    WebContextFormatError,
    WebContextNotFoundError,
    WebContextProvider,
    WebContextProviderError,
    WebContextQuotaError,
    WebContextUsage,
)
from memex.webcontext.config import WebContextConfig, known_providers, resolve_chain
from memex.webcontext.providers import build_provider_from_env, resolve
from memex.webcontext.schema import (
    EntityProfile,
    entity_profile_schema,
    validate_profile,
    validate_profile_data,
)
from memex.webcontext.service import search_entity

__all__ = [
    "EntityKind",
    "EntityProfile",
    "ProfileResult",
    "WebContextConfig",
    "WebContextConfigError",
    "WebContextError",
    "WebContextFormatError",
    "WebContextNotFoundError",
    "WebContextProvider",
    "WebContextProviderError",
    "WebContextQuotaError",
    "WebContextUsage",
    "build_provider_from_env",
    "entity_profile_schema",
    "known_providers",
    "resolve",
    "resolve_chain",
    "search_entity",
    "validate_profile",
    "validate_profile_data",
]
