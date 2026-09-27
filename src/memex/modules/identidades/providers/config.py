"""`ContactsSyncConfig` — config resuelta para hablar con la API de un proveedor de contactos.

Sigue la convención `from_env` de `CalendarSyncConfig`/`OcrConfig`. NO hay `api_key`/`SecretStr`: el
secreto (token OAuth) NO entra a este modelo — se resuelve en runtime desde el VAULT de la cuenta
del dashboard (`memex.modules.identidades.providers.oauth`). Esta config solo lleva endpoint del
proveedor + parámetros de HTTP, leídos de `MEMEX_CONTACTS_BASE_URL` (config del despliegue).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from memex.modules.identidades.providers.base import ContactsProviderError

_BASE_URL_ENV = "MEMEX_CONTACTS_BASE_URL"
#: Default: Google People API v1. Override por env para tests/otros despliegues.
_DEFAULT_BASE_URL = "https://people.googleapis.com/v1"


class ContactsSyncConfigError(ContactsProviderError):
    """Config inválida. Subclasea `ContactsProviderError` para que los callers atrapen la base."""

    def __init__(self, message: str) -> None:
        super().__init__(0, message)


class ContactsSyncConfig(BaseModel):
    """Configuración resuelta para el cliente HTTP de un proveedor de contactos."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = _DEFAULT_BASE_URL
    timeout_s: float = 30.0
    max_retries: int = 3
    backoff_base: float = 0.5
    #: Tope de contactos por página pedido al proveedor (People API permite hasta 1000).
    page_size: int = 1000

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        base_url: str | None = None,
    ) -> ContactsSyncConfig:
        """Construye una `ContactsSyncConfig`. `base_url` sale de `MEMEX_CONTACTS_BASE_URL`; default
        Google People API v1."""
        env_map: Mapping[str, str] = env if env is not None else os.environ
        resolved_base = base_url or env_map.get(_BASE_URL_ENV, "").strip() or _DEFAULT_BASE_URL
        return cls(base_url=resolved_base)
