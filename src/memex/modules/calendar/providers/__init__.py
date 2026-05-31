"""Registry lazy de proveedores de calendario (ADR-015 §4 enmendado).

Mapea nombre de proveedor → builder que construye el cliente concreto a partir de la config + el
access token YA resuelto (por `memex.modules.calendar.providers.oauth`). Lazy igual que el
registry de OAuth de IMAP (`memex.ingestors.imap.oauth.resolve`): importar el registry no importa
httpx ni las deps de cada proveedor. Slice 1: solo `google` (Outlook/MS Graph llega después).

El worker (`memex.modules.calendar.sync`) SIEMPRE tipa contra `CalendarProvider` (Protocol), nunca
contra el cliente concreto.
"""

from __future__ import annotations

from collections.abc import Callable

from memex.modules.calendar.providers.base import (
    CalendarProvider,
    CalendarProviderError,
    CalendarSyncTokenExpired,
    ProviderEvent,
    ProviderEventRef,
    ProviderEventWrite,
    ProviderPage,
)
from memex.modules.calendar.providers.config import CalendarSyncConfig

#: Builder: (config, access_token, calendar_id) → cliente que cumple `CalendarProvider`.
ProviderBuilder = Callable[[CalendarSyncConfig, str, str], CalendarProvider]


def _google_builder(
    config: CalendarSyncConfig, access_token: str, calendar_id: str
) -> CalendarProvider:
    from memex.modules.calendar.providers.google import GoogleCalendarClient

    return GoogleCalendarClient(config, access_token, calendar_id=calendar_id)


_LAZY_BUILDERS: dict[str, Callable[[], ProviderBuilder]] = {
    "google": lambda: _google_builder,
}


def resolve(name: str) -> ProviderBuilder:
    """Return el builder para `name`, cargando el módulo perezosamente. `KeyError` si no existe."""
    if name not in _LAZY_BUILDERS:
        raise KeyError(
            f"no calendar provider registered for name={name!r}. Known: {known_providers()}"
        )
    return _LAZY_BUILDERS[name]()


def known_providers() -> list[str]:
    """Lista de proveedores resolvibles. Útil para validación de config / CLI."""
    return sorted(_LAZY_BUILDERS.keys())


__all__ = [
    "CalendarProvider",
    "CalendarProviderError",
    "CalendarSyncConfig",
    "CalendarSyncTokenExpired",
    "ProviderBuilder",
    "ProviderEvent",
    "ProviderEventRef",
    "ProviderEventWrite",
    "ProviderPage",
    "known_providers",
    "resolve",
]
