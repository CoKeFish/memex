"""Registry lazy de proveedores de contactos (módulo identidades).

Mapea nombre de proveedor → builder que construye el cliente concreto a partir de la config + el
access token YA resuelto (por `memex.modules.identidades.providers.oauth`). Lazy igual
que el registry de calendar: importar el registry no importa httpx ni las deps de cada
proveedor. Slice 1: solo `google`.

El worker (`memex.modules.identidades.sync`) SIEMPRE tipa contra `ContactsProvider` (Protocol),
nunca contra el cliente concreto.
"""

from __future__ import annotations

from collections.abc import Callable

from memex.modules.identidades.providers.base import (
    ContactsProvider,
    ContactsProviderError,
    ContactsSyncTokenExpired,
    ProviderContact,
    ProviderContactsPage,
)
from memex.modules.identidades.providers.config import ContactsSyncConfig

#: Builder: (config, access_token) → cliente que cumple `ContactsProvider`.
ProviderBuilder = Callable[[ContactsSyncConfig, str], ContactsProvider]


def _google_builder(config: ContactsSyncConfig, access_token: str) -> ContactsProvider:
    from memex.modules.identidades.providers.google import GooglePeopleClient

    return GooglePeopleClient(config, access_token)


_LAZY_BUILDERS: dict[str, Callable[[], ProviderBuilder]] = {
    "google": lambda: _google_builder,
}


def resolve(name: str) -> ProviderBuilder:
    """Return el builder para `name`, cargando el módulo perezosamente. `KeyError` si no existe."""
    if name not in _LAZY_BUILDERS:
        raise KeyError(
            f"no contacts provider registered for name={name!r}. Known: {known_providers()}"
        )
    return _LAZY_BUILDERS[name]()


def known_providers() -> list[str]:
    """Lista de proveedores resolvibles. Útil para validación de config / CLI."""
    return sorted(_LAZY_BUILDERS.keys())


__all__ = [
    "ContactsProvider",
    "ContactsProviderError",
    "ContactsSyncConfig",
    "ContactsSyncTokenExpired",
    "ProviderBuilder",
    "ProviderContact",
    "ProviderContactsPage",
    "known_providers",
    "resolve",
]
