"""Registry lazy de proveedores de contexto web + armado de la cadena de fallback.

Mapea nombre → builder que construye el proveedor concreto a partir de una `WebContextConfig`. Lazy
igual que `memex.geo.providers`: importar el registry NO importa httpx ni el binario de codex.
`build_provider_from_env` resuelve la cadena (`resolve_chain`) y construye cada eslabón, descartando
el que no tenga credenciales mientras quede otro (calca `memex.llm.registry.build_llm_client`); con
≥2 eslabones devuelve un `FallbackWebContextProvider`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from memex.logging import get_logger
from memex.webcontext.client import WebContextConfigError, WebContextProvider
from memex.webcontext.config import WebContextConfig, resolve_chain

#: Builder: WebContextConfig → proveedor que cumple `WebContextProvider`.
ProviderBuilder = Callable[[WebContextConfig], WebContextProvider]

_log = get_logger("memex.webcontext.providers")


def _codex_builder(config: WebContextConfig) -> WebContextProvider:
    from memex.webcontext.codex import CodexWebContextProvider

    return CodexWebContextProvider(config)


def _firecrawl_builder(config: WebContextConfig) -> WebContextProvider:
    from memex.webcontext.firecrawl import FirecrawlProvider

    return FirecrawlProvider(config)


_LAZY_BUILDERS: dict[str, Callable[[], ProviderBuilder]] = {
    "codex": lambda: _codex_builder,
    "firecrawl": lambda: _firecrawl_builder,
}


def resolve(name: str) -> ProviderBuilder:
    """Return el builder para `name`, cargando el módulo perezosamente. `KeyError` si no existe."""
    if name not in _LAZY_BUILDERS:
        raise KeyError(
            f"no webcontext provider registered for name={name!r}. Known: {sorted(_LAZY_BUILDERS)}"
        )
    return _LAZY_BUILDERS[name]()


def known_providers() -> list[str]:
    """Lista de proveedores resolvibles."""
    return sorted(_LAZY_BUILDERS)


def build_provider_from_env(
    env: Mapping[str, str] | None = None, *, provider: str | None = None
) -> WebContextProvider:
    """Construye el proveedor (o la cadena de fallback) a partir del entorno.

    `provider` fuerza uno solo (CLI `--provider`); si no, usa la cadena `resolve_chain` (default
    codex→firecrawl). Un eslabón sin credenciales (firecrawl sin key) se descarta mientras quede
    otro; si es el único pedido, propaga. ≥2 eslabones → fallback (`WebContextConfigError` si 0).
    """
    names = resolve_chain(env, provider=provider)
    built: list[tuple[str, WebContextProvider]] = []
    for name in names:
        try:
            config = WebContextConfig.from_env(env, provider=name)
            built.append((name, resolve(name)(config)))
        except WebContextConfigError:
            if len(names) == 1:
                raise
            _log.warning("webcontext.provider.unavailable", provider=name)

    if not built:
        raise WebContextConfigError(
            "ningún proveedor de webcontext disponible (¿FIRECRAWL_API_KEY? ¿sesión de codex?)"
        )
    if len(built) == 1:
        return built[0][1]

    from memex.webcontext.fallback import FallbackWebContextProvider

    return FallbackWebContextProvider(built)
