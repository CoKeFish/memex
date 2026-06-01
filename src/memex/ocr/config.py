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
from memex.ocr.pdf import PdfCaps
from memex.ocr.zip import ZipCaps

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

#: Topes de PDF (conservadores) — acotan el fan-out de costo (cada imagen/página = 1 llamada de
#: visión). Override por env. El default de `raster_dpi` vive en `PdfCaps` (no amerita env var aún).
_PDF_MAX_IMAGES_ENV = "MEMEX_OCR_PDF_MAX_IMAGES"
_PDF_MAX_PAGES_ENV = "MEMEX_OCR_PDF_MAX_PAGES"
_PDF_MIN_IMAGE_PX_ENV = "MEMEX_OCR_PDF_MIN_IMAGE_PX"
_PDF_TEXT_MIN_CHARS_ENV = "MEMEX_OCR_PDF_TEXT_MIN_CHARS"
_DEFAULT_PDF_MAX_IMAGES = 5
_DEFAULT_PDF_MAX_PAGES = 5
_DEFAULT_PDF_MIN_IMAGE_PX = 200
_DEFAULT_PDF_TEXT_MIN_CHARS = 32

#: Topes de ZIP (conservadores) — acotan el fan-out de costo y protegen de zip-bombs. Override por
#: env (los de tamaño se expresan en MiB para legibilidad y se convierten a bytes en `zip_caps`).
_ZIP_MAX_ENTRIES_ENV = "MEMEX_OCR_ZIP_MAX_ENTRIES"
_ZIP_MAX_TOTAL_MB_ENV = "MEMEX_OCR_ZIP_MAX_TOTAL_MB"
_ZIP_MAX_ENTRY_MB_ENV = "MEMEX_OCR_ZIP_MAX_ENTRY_MB"
_DEFAULT_ZIP_MAX_ENTRIES = 20
_DEFAULT_ZIP_MAX_TOTAL_MB = 50
_DEFAULT_ZIP_MAX_ENTRY_MB = 15

#: Pool de contraseñas para adjuntos encriptados (ZIP/PDF). SECRETO (suelen ser nº de documento de
#: identidad): viene de Doppler, separado por comas, redactado en repr/logs (`SecretStr`). Nunca en
#: la DB ni en logs/errores. Vacío por default → los adjuntos encriptados quedan `error`.
_PASSWORDS_ENV = "MEMEX_ATTACHMENT_PASSWORDS"


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

    #: Topes de procesamiento de PDF (ver `pdf_caps`). Defaults conservadores; override por env.
    pdf_max_images: int = _DEFAULT_PDF_MAX_IMAGES
    pdf_max_pages: int = _DEFAULT_PDF_MAX_PAGES
    pdf_min_image_px: int = _DEFAULT_PDF_MIN_IMAGE_PX
    pdf_text_min_chars: int = _DEFAULT_PDF_TEXT_MIN_CHARS

    #: Topes de ZIP (ver `zip_caps`). Los de tamaño en bytes (resueltos desde MiB en `from_env`).
    zip_max_entries: int = _DEFAULT_ZIP_MAX_ENTRIES
    zip_max_total_bytes: int = _DEFAULT_ZIP_MAX_TOTAL_MB * 1024 * 1024
    zip_max_entry_bytes: int = _DEFAULT_ZIP_MAX_ENTRY_MB * 1024 * 1024

    #: Pool de contraseñas para adjuntos encriptados (ZIP/PDF). `SecretStr` → redactado en logs.
    attachment_passwords: tuple[SecretStr, ...] = ()

    # Carry el *nombre* de la env var (no el valor) para logging / debugging.
    api_key_env: str = ""

    def pdf_caps(self) -> PdfCaps:
        """Topes de PDF resueltos, listos para `memex.ocr.pdf.extract_pdf`."""
        return PdfCaps(
            max_images=self.pdf_max_images,
            max_pages=self.pdf_max_pages,
            min_image_px=self.pdf_min_image_px,
            text_min_chars=self.pdf_text_min_chars,
        )

    def zip_caps(self) -> ZipCaps:
        """Topes de ZIP resueltos, listos para `memex.ocr.zip.unpack_zip`."""
        return ZipCaps(
            max_entries=self.zip_max_entries,
            max_total_bytes=self.zip_max_total_bytes,
            max_entry_bytes=self.zip_max_entry_bytes,
        )

    def password_pool(self) -> tuple[str, ...]:
        """Contraseñas en claro para destrabar adjuntos encriptados. NO loguear el resultado."""
        return tuple(p.get_secret_value() for p in self.attachment_passwords)

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
            pdf_max_images=_pos_int_env(env_map, _PDF_MAX_IMAGES_ENV, _DEFAULT_PDF_MAX_IMAGES),
            pdf_max_pages=_pos_int_env(env_map, _PDF_MAX_PAGES_ENV, _DEFAULT_PDF_MAX_PAGES),
            pdf_min_image_px=_pos_int_env(
                env_map, _PDF_MIN_IMAGE_PX_ENV, _DEFAULT_PDF_MIN_IMAGE_PX
            ),
            pdf_text_min_chars=_pos_int_env(
                env_map, _PDF_TEXT_MIN_CHARS_ENV, _DEFAULT_PDF_TEXT_MIN_CHARS
            ),
            zip_max_entries=_pos_int_env(env_map, _ZIP_MAX_ENTRIES_ENV, _DEFAULT_ZIP_MAX_ENTRIES),
            zip_max_total_bytes=_pos_int_env(
                env_map, _ZIP_MAX_TOTAL_MB_ENV, _DEFAULT_ZIP_MAX_TOTAL_MB
            )
            * 1024
            * 1024,
            zip_max_entry_bytes=_pos_int_env(
                env_map, _ZIP_MAX_ENTRY_MB_ENV, _DEFAULT_ZIP_MAX_ENTRY_MB
            )
            * 1024
            * 1024,
            attachment_passwords=tuple(
                SecretStr(p.strip())
                for p in env_map.get(_PASSWORDS_ENV, "").split(",")
                if p.strip()
            ),
        )


def _pos_int_env(env_map: Mapping[str, str], name: str, default: int) -> int:
    """Lee un entero > 0 de la env var `name`, o `default` si está vacía.

    Levanta `OcrConfigError` si el valor no es un entero o es <= 0 (config inválida = falla rápido,
    igual que una API key faltante; nunca cae a un default silencioso ante un valor malo).
    """
    raw = env_map.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise OcrConfigError(f"env var {name!r} debe ser un entero, no {raw!r}") from e
    if value <= 0:
        raise OcrConfigError(f"env var {name!r} debe ser > 0, no {value}")
    return value
