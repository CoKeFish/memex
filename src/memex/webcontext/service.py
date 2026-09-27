"""Función de servicio del subsistema webcontext — orquestación pura, tipada contra el Protocol.

Sin argparse ni stdout: la consumen el CLI (`memex-webcontext`) y cualquier futuro caller — módulos
de dominio (identidades/finanzas/calendario, que la usarán para ANOTAR sus vértices org/producto) o
Hermes como tool tipada. Es el seam de integración: el subsistema NO conoce a sus consumidores.

El fallback codex→firecrawl, si está configurado, lo provee `build_provider_from_env` como un
`FallbackWebContextProvider` que cumple el mismo Protocol — `search_entity` no se entera.
"""

from __future__ import annotations

from memex.webcontext.client import EntityKind, ProfileResult, WebContextProvider


async def search_entity(provider: WebContextProvider, name: str, kind: EntityKind) -> ProfileResult:
    """Busca el contexto web de una entidad (org/producto) y devuelve su perfil validado."""
    return await provider.search(name, kind)
