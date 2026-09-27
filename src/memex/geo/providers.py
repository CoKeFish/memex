"""Registry lazy de proveedores de mapas.

Mapea nombre de proveedor → builder que construye el cliente concreto a partir de una
`GeoConfig`. Lazy igual que `memex.modules.calendar.providers.resolve`: importar el registry
NO importa httpx ni el código de cada proveedor (eso pasa solo al construir uno).

Los callers SIEMPRE tipan contra `GeoProvider` (Protocol), nunca contra el cliente concreto.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from memex.geo.client import GeoProvider
from memex.geo.config import GeoConfig

#: Builder: GeoConfig → cliente que cumple `GeoProvider`.
ProviderBuilder = Callable[[GeoConfig], GeoProvider]


def _google_builder(config: GeoConfig) -> GeoProvider:
    from memex.geo.google import GoogleMapsProvider

    return GoogleMapsProvider(config)


def _ors_builder(config: GeoConfig) -> GeoProvider:
    from memex.geo.ors import OpenRouteServiceProvider

    return OpenRouteServiceProvider(config)


_LAZY_BUILDERS: dict[str, Callable[[], ProviderBuilder]] = {
    "google": lambda: _google_builder,
    "ors": lambda: _ors_builder,
}


def resolve(name: str) -> ProviderBuilder:
    """Return el builder para `name`, cargando el módulo perezosamente. `KeyError` si no existe."""
    if name not in _LAZY_BUILDERS:
        raise KeyError(
            f"no geo provider registered for name={name!r}. Known: {sorted(_LAZY_BUILDERS)}"
        )
    return _LAZY_BUILDERS[name]()


def known_providers() -> list[str]:
    """Lista de proveedores resolvibles."""
    return sorted(_LAZY_BUILDERS)


def build_provider_from_env(
    env: Mapping[str, str] | None = None, *, provider: str | None = None
) -> GeoProvider:
    """Resuelve `GeoConfig.from_env` y construye el proveedor concreto. Atajo para CLI/callers."""
    config = GeoConfig.from_env(env, provider=provider)
    return resolve(config.provider)(config)
