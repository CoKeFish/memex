"""`CalendarSyncConfig` — configuración resuelta para hablar con la API de un proveedor.

Sigue la convención `from_env` de `OcrConfig`/`LLMConfig`, con una diferencia importante: acá NO
hay `api_key`/`SecretStr`. El secreto (token OAuth) NO entra a este modelo — se resuelve en
runtime desde el archivo en disco apuntado por la env var nombrada en
`mod_calendar_provider_accounts.token_path_env` (ADR-015 §7: en la DB solo va la referencia). Esta
config solo lleva endpoint del proveedor + parámetros de HTTP (timeout/retries), leídos de
`MEMEX_CALENDAR_BASE_URL` (config del despliegue).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from memex.modules.calendar.providers.base import CalendarProviderError

_BASE_URL_ENV = "MEMEX_CALENDAR_BASE_URL"
#: Default: Google Calendar API v3. Override por env para tests/otros despliegues.
_DEFAULT_BASE_URL = "https://www.googleapis.com/calendar/v3"
_TIME_ZONE_ENV = "MEMEX_CALENDAR_TIME_ZONE"
#: TZ con la que se PUSHEAN los eventos con hora (las fechas/horas se guardan naive — decisión
#: 0010). Default UTC; configurable a la zona del calendario para que el write-back no corra la
#: hora. Solo afecta egress (slice 5); el ingress descarta tz a hora local.
_DEFAULT_TIME_ZONE = "UTC"

_PAST_DAYS_ENV = "MEMEX_CALENDAR_SYNC_PAST_DAYS"
_FUTURE_DAYS_ENV = "MEMEX_CALENDAR_SYNC_FUTURE_DAYS"
#: Ventana del FULL sync (días hacia atrás/adelante desde hoy). Acota la expansión de eventos
#: recurrentes (Google con singleEvents=true los expande en instancias; sin ventana llega a
#: ~2001-2099). Default ~6 meses atrás / ~12 adelante. Solo aplica al full sync (el incremental
#: por syncToken NO admite timeMin/timeMax).
_DEFAULT_PAST_DAYS = 183
_DEFAULT_FUTURE_DAYS = 365


class CalendarSyncConfigError(CalendarProviderError):
    """Config inválida. Subclasea `CalendarProviderError` para que los callers atrapen la base."""

    def __init__(self, message: str) -> None:
        super().__init__(0, message)


class CalendarSyncConfig(BaseModel):
    """Configuración resuelta para el cliente HTTP de un proveedor de calendario."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = _DEFAULT_BASE_URL
    timeout_s: float = 30.0
    max_retries: int = 3
    backoff_base: float = 0.5
    #: Tope de eventos por página pedido al proveedor (Google permite hasta 2500; 250 es prudente).
    max_results: int = 250
    #: TZ con la que se pushean los eventos con hora en el write-back (ver _DEFAULT_TIME_ZONE).
    time_zone: str = _DEFAULT_TIME_ZONE
    #: Ventana del full sync (días atrás/adelante; ver _DEFAULT_PAST_DAYS).
    sync_past_days: int = _DEFAULT_PAST_DAYS
    sync_future_days: int = _DEFAULT_FUTURE_DAYS

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        base_url: str | None = None,
    ) -> CalendarSyncConfig:
        """Construye una `CalendarSyncConfig`. `base_url`/`time_zone`/ventana salen de las env vars
        `MEMEX_CALENDAR_*`; defaults Google Calendar v3 + UTC + 183/365 días."""
        env_map: Mapping[str, str] = env if env is not None else os.environ
        resolved_base = base_url or env_map.get(_BASE_URL_ENV, "").strip() or _DEFAULT_BASE_URL
        resolved_tz = env_map.get(_TIME_ZONE_ENV, "").strip() or _DEFAULT_TIME_ZONE

        def _int_env(name: str, default: int) -> int:
            raw = env_map.get(name, "").strip()
            try:
                return int(raw) if raw else default
            except ValueError:
                return default

        return cls(
            base_url=resolved_base,
            time_zone=resolved_tz,
            sync_past_days=_int_env(_PAST_DAYS_ENV, _DEFAULT_PAST_DAYS),
            sync_future_days=_int_env(_FUTURE_DAYS_ENV, _DEFAULT_FUTURE_DAYS),
        )
