"""LLMConfig — configuración resuelta para un proveedor LLM.

Sigue la convención `from_env` de los ingestors (`SocialConfig.from_source_config`,
`social/config.py`): el *nombre* de la env var se conoce de antemano, el *valor*
nunca toca la DB y se envuelve en `SecretStr` para que no aparezca en logs.

A diferencia de los ingestors, la key NO usa el prefijo `MEMEX_`: DeepSeek expone
`DEEPSEEK_API_KEY` como nombre canónico (es el que usan sus propios docs) y se
inyecta vía Doppler. Por eso se lee directo de `os.environ`, no del `Settings`
global (que solo lee `MEMEX_*`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, SecretStr

from memex.llm.client import LLMError

#: Nombre canónico de la env var con la API key de DeepSeek (Doppler, config shared).
_DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
_DEFAULT_BASE_URL = "https://api.deepseek.com"
#: deepseek-chat = alias legacy → v4-flash (no-thinking), el modelo más barato.
#: Override-able por llamada vía `LLMClient.complete(..., model=...)`.
_DEFAULT_MODEL = "deepseek-chat"


class LLMConfigError(LLMError):
    """Config inválida o falta la env var de la API key.

    Subclasea `LLMError` para que los callers atrapen la base genérica y traten
    cualquier fallo de la capa LLM uniformemente.
    """

    def __init__(self, message: str) -> None:
        super().__init__(0, message)


class LLMConfig(BaseModel):
    """Configuración resuelta para hablar con un proveedor LLM.

    `api_key` es `SecretStr` → redactado en repr(), str(), f-strings y
    model_dump/json. El cliente concreto usa `.get_secret_value()` en el borde HTTP.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key: SecretStr
    base_url: str = _DEFAULT_BASE_URL
    default_model: str = _DEFAULT_MODEL
    #: timeout_s aplica a read/write/pool (la completion puede tardar). connect_timeout_s es
    #: aparte y corto: un connect colgado falla rápido y se reintenta, sin comerse el budget de
    #: la generación. Para extracciones muy grandes (max_tokens alto), subir timeout_s.
    timeout_s: float = 60.0
    connect_timeout_s: float = 10.0
    max_retries: int = 3
    backoff_base: float = 0.5

    # Carry el *nombre* de la env var (no el valor) para logging / debugging.
    api_key_env: str = ""

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        api_key_env: str = _DEFAULT_API_KEY_ENV,
        base_url: str | None = None,
        default_model: str | None = None,
    ) -> LLMConfig:
        """Resuelve la env var de la API key y construye una `LLMConfig` validada.

        Levanta `LLMConfigError` si la env var no está seteada o resuelve a vacío.
        `base_url` / `default_model` permiten override sin tocar el resto de defaults.
        """
        env_map: Mapping[str, str] = env if env is not None else os.environ
        value = env_map.get(api_key_env, "").strip()
        if not value:
            raise LLMConfigError(f"env var {api_key_env!r} is not set or resolves to empty value")

        fields: dict[str, object] = {
            "api_key": SecretStr(value),
            "api_key_env": api_key_env,
        }
        if base_url is not None:
            fields["base_url"] = base_url
        if default_model is not None:
            fields["default_model"] = default_model
        return cls.model_validate(fields)
