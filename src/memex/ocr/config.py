"""OcrConfig — configuración resuelta para un proveedor de OCR por visión.

Sigue la convención `from_env` de `LLMConfig`: el *nombre* de la env var de la key se conoce de
antemano, el *valor* nunca toca la DB y se envuelve en `SecretStr`. La key NO usa prefijo
`MEMEX_` (`OCR_API_KEY` es el nombre canónico, inyectado por Doppler), igual que `DEEPSEEK_API_KEY`.

`base_url` + `default_model` son los que indican **proveedor** y **modelo** de OCR. Se leen de
`MEMEX_OCR_BASE_URL` / `MEMEX_OCR_MODEL` (config del despliegue), y se pueden override por corrida
(`memex-ocr run --model ...`) vía el arg `model` de `from_env`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, SecretStr

from memex.ocr.client import OcrError

#: Nombre canónico de la env var con la API key del proveedor OCR (Doppler).
_DEFAULT_API_KEY_ENV = "OCR_API_KEY"
#: Fallback cuando OCR_API_KEY no está: el default base_url es OpenAI, así que aceptamos su
#: nombre canónico (mismo espíritu que DEEPSEEK_API_KEY). Para otro proveedor, setear OCR_API_KEY.
_FALLBACK_API_KEY_ENV = "OPENAI_API_KEY"
_BASE_URL_ENV = "MEMEX_OCR_BASE_URL"
_MODEL_ENV = "MEMEX_OCR_MODEL"

#: Defaults razonables para un proveedor OpenAI-compatible con visión. Override por env.
_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"


class OcrConfigError(OcrError):
    """Config inválida o falta la env var de la API key.

    Subclasea `OcrError` para que los callers atrapen la base genérica.
    """

    def __init__(self, message: str) -> None:
        super().__init__(0, message)


class OcrConfig(BaseModel):
    """Configuración resuelta para hablar con un proveedor OCR (visión, OpenAI-compatible).

    `api_key` es `SecretStr` → redactado en repr/logs/dumps. El cliente concreto usa
    `.get_secret_value()` en el borde HTTP.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key: SecretStr
    base_url: str = _DEFAULT_BASE_URL
    default_model: str = _DEFAULT_MODEL
    #: timeout_s aplica a read/write/pool (una transcripción densa puede tardar). connect_timeout_s
    #: es aparte y corto: un connect colgado falla rápido y se reintenta. Espeja LLMConfig.
    timeout_s: float = 120.0
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
        model: str | None = None,
    ) -> OcrConfig:
        """Resuelve la env var de la API key y construye una `OcrConfig` validada.

        Levanta `OcrConfigError` si la env var no está seteada o resuelve a vacío. `base_url` /
        `model` explícitos ganan sobre las env vars `MEMEX_OCR_BASE_URL` / `MEMEX_OCR_MODEL`
        (así el `--model` del CLI overridea sin tocar el resto).
        """
        env_map: Mapping[str, str] = env if env is not None else os.environ
        value = env_map.get(api_key_env, "").strip()
        resolved_env = api_key_env
        if not value and api_key_env == _DEFAULT_API_KEY_ENV:
            # Fallback al nombre canónico de OpenAI (el proveedor default).
            value = env_map.get(_FALLBACK_API_KEY_ENV, "").strip()
            if value:
                resolved_env = _FALLBACK_API_KEY_ENV
        if not value:
            raise OcrConfigError(
                f"env var {api_key_env!r} (ni {_FALLBACK_API_KEY_ENV!r}) está seteada"
                if api_key_env == _DEFAULT_API_KEY_ENV
                else f"env var {api_key_env!r} is not set or resolves to empty value"
            )

        resolved_base = base_url or env_map.get(_BASE_URL_ENV, "").strip() or _DEFAULT_BASE_URL
        resolved_model = model or env_map.get(_MODEL_ENV, "").strip() or _DEFAULT_MODEL

        return cls(
            api_key=SecretStr(value),
            api_key_env=resolved_env,
            base_url=resolved_base,
            default_model=resolved_model,
        )
