"""GeoConfig — configuración resuelta para un proveedor de mapas.

Sigue la convención `from_env` de `memex.llm.config.LLMConfig`: el *nombre* de la env var
de la key se conoce de antemano, el *valor* nunca toca la DB y se envuelve en `SecretStr`
para que no aparezca en logs ni repr. Cada proveedor tiene su nombre canónico de env var
(el que usan sus propios docs), inyectado vía Doppler; por eso se leen directo de
`os.environ`, no del `Settings` global (que solo lee `MEMEX_*`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, SecretStr

from memex.geo.client import GeoConfigError

#: provider → (NOMBRE de la env var de la key, base_url por default).
#: Las keys viven en Doppler (config shared), como `DEEPSEEK_API_KEY`.
_PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
    "google": ("GMAPS_API_KEY", "https://maps.googleapis.com"),
    "ors": ("OPENROUTE_API_KEY", "https://api.openrouteservice.org"),
}
_DEFAULT_PROVIDER = "google"
#: Env var opcional para elegir proveedor sin pasar `--provider`.
_PROVIDER_ENV = "MEMEX_GEO_PROVIDER"
#: Env var opcional para override de base_url (tests / proxy).
_BASE_URL_ENV = "MEMEX_GEO_BASE_URL"


def known_providers() -> list[str]:
    """Proveedores configurables. Útil para validación de CLI / mensajes de error."""
    return sorted(_PROVIDER_DEFAULTS)


class GeoConfig(BaseModel):
    """Configuración resuelta para hablar con un proveedor de mapas.

    `api_key` es `SecretStr` → redactado en repr(), str(), f-strings y model_dump/json.
    El cliente concreto usa `.get_secret_value()` solo en el borde HTTP.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    api_key: SecretStr
    base_url: str
    #: Carry el *nombre* de la env var (no el valor) para logging / debugging.
    api_key_env: str = ""
    timeout_s: float = 30.0
    connect_timeout_s: float = 10.0
    max_retries: int = 3
    backoff_base: float = 0.5

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        provider: str | None = None,
        base_url: str | None = None,
    ) -> GeoConfig:
        """Resuelve proveedor + env var de la key y construye una `GeoConfig` validada.

        Proveedor: arg explícito > `MEMEX_GEO_PROVIDER` > default `"google"`.
        Levanta `GeoConfigError` si el proveedor es desconocido o su env var de la key
        no está seteada / resuelve a vacío.
        """
        env_map: Mapping[str, str] = env if env is not None else os.environ

        resolved_provider = provider or env_map.get(_PROVIDER_ENV, "").strip() or _DEFAULT_PROVIDER
        if resolved_provider not in _PROVIDER_DEFAULTS:
            raise GeoConfigError(
                f"unknown geo provider {resolved_provider!r}; known: {known_providers()}"
            )

        key_env, default_base = _PROVIDER_DEFAULTS[resolved_provider]
        value = env_map.get(key_env, "").strip()
        if not value:
            raise GeoConfigError(f"env var {key_env!r} is not set or resolves to empty value")

        resolved_base = base_url or env_map.get(_BASE_URL_ENV, "").strip() or default_base
        return cls.model_validate(
            {
                "provider": resolved_provider,
                "api_key": SecretStr(value),
                "api_key_env": key_env,
                "base_url": resolved_base,
            }
        )
