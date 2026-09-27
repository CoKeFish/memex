"""WebContextConfig — configuración resuelta para un proveedor de contexto web.

Calca la convención `from_env` de `memex.geo.config.GeoConfig`: el NOMBRE de la env var de la key se
conoce de antemano, su valor nunca toca la DB y se envuelve en `SecretStr`. A diferencia de geo,
codex NO necesita key (es un subproceso con sesión, no HTTP) → `api_key` es opcional. La selección
por defecto es una CADENA (codex→firecrawl), resuelta por `resolve_chain`; cada eslabón se construye
con `from_env`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, SecretStr

from memex.webcontext.client import WebContextConfigError

#: provider → (NOMBRE de la env var de la key | None si no usa, base_url por default | None).
#: Las keys viven en Doppler (config shared). codex no usa key ni base_url (subproceso con sesión).
_PROVIDER_DEFAULTS: dict[str, tuple[str | None, str | None]] = {
    "codex": (None, None),
    "firecrawl": ("FIRECRAWL_API_KEY", "https://api.firecrawl.dev"),
}
#: Cadena por defecto: codex primario ($0), firecrawl de fallback (se descarta si no hay key).
_DEFAULT_CHAIN: tuple[str, ...] = ("codex", "firecrawl")
#: Env var opcional para fijar la cadena (coma-separada) sin pasar `--provider`.
_PROVIDER_ENV = "MEMEX_WEBCONTEXT_PROVIDER"
#: Env var opcional para override de base_url (tests / proxy).
_BASE_URL_ENV = "MEMEX_WEBCONTEXT_BASE_URL"


def known_providers() -> list[str]:
    """Proveedores configurables. Útil para validación de CLI / mensajes de error."""
    return sorted(_PROVIDER_DEFAULTS)


def resolve_chain(
    env: Mapping[str, str] | None = None, *, provider: str | None = None
) -> list[str]:
    """Resuelve la cadena ORDENADA de proveedores a intentar.

    `provider` explícito (CLI `--provider`) → cadena de uno. Si no: `MEMEX_WEBCONTEXT_PROVIDER`
    (coma-separada) > `_DEFAULT_CHAIN`. `WebContextConfigError` si un nombre es desconocido.
    """
    env_map: Mapping[str, str] = env if env is not None else os.environ
    if provider is not None:
        names = [provider]
    else:
        raw = env_map.get(_PROVIDER_ENV, "").strip()
        names = [n.strip() for n in raw.split(",") if n.strip()] or list(_DEFAULT_CHAIN)
    for name in names:
        if name not in _PROVIDER_DEFAULTS:
            raise WebContextConfigError(
                f"unknown webcontext provider {name!r}; known: {known_providers()}"
            )
    return names


class WebContextConfig(BaseModel):
    """Config resuelta de un proveedor. `api_key` (si aplica) es `SecretStr` (redactado)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    api_key: SecretStr | None = None
    base_url: str | None = None
    #: Carry el NOMBRE de la env var (no el valor) para logging/debugging.
    api_key_env: str = ""
    timeout_s: float = 90.0  # codex tarda ~46-68s (probe); la latencia no es criterio
    connect_timeout_s: float = 10.0
    max_retries: int = 3  # retry HTTP de firecrawl (5xx/red)
    backoff_base: float = 0.5
    format_retries: int = 1  # retry a nivel proveedor ante salida que no valida
    search_limit: int = 5  # candidatos de firecrawl /search
    scrape_attempts: int = 2  # cuántas URLs candidatas scrapear (acota costo/latencia)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        provider: str,
        base_url: str | None = None,
    ) -> WebContextConfig:
        """Config de UN proveedor. `WebContextConfigError` si el proveedor es desconocido o su env
        var de key requerida no está seteada (firecrawl). codex no requiere key."""
        env_map: Mapping[str, str] = env if env is not None else os.environ
        if provider not in _PROVIDER_DEFAULTS:
            raise WebContextConfigError(
                f"unknown webcontext provider {provider!r}; known: {known_providers()}"
            )
        key_env, default_base = _PROVIDER_DEFAULTS[provider]
        api_key: SecretStr | None = None
        if key_env is not None:
            value = env_map.get(key_env, "").strip()
            if not value:
                raise WebContextConfigError(
                    f"env var {key_env!r} is not set or resolves to empty value"
                )
            api_key = SecretStr(value)
        resolved_base = base_url or env_map.get(_BASE_URL_ENV, "").strip() or default_base
        return cls.model_validate(
            {
                "provider": provider,
                "api_key": api_key,
                "api_key_env": key_env or "",
                "base_url": resolved_base,
            }
        )
