"""Config del daemon de transporte (env-driven) → valores resueltos.

`TransportSettings` lee el entorno (prefijo `MEMEX_TRANSPORT_`) en minutos/horas/strings;
`TransportConfig.from_env` los resuelve a `timedelta`/`TravelMode`/`ZoneInfo` para que el resto del
subsistema reciba tipos ya cocinados. Prefijo de env propio (no choca con `memex.config.Settings`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

from memex.geo.client import TravelMode


class TransportSettings(BaseSettings):
    """Perillas crudas del daemon de transporte (env `MEMEX_TRANSPORT_*`)."""

    model_config = SettingsConfigDict(
        env_prefix="MEMEX_TRANSPORT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mode: str = "driving"  # modo de viaje por default (un valor de TravelMode)
    buffer_min: int = 10  # colchón de llegada: querés estar N min antes del inicio
    lead_min: int = 30  # ventana de aviso: avisar cuando falten <= N min para salir
    compute_window_min: int = 120  # NO se llama a Maps si el evento está más lejos que esto
    horizon_hours: int = 24  # hasta dónde adelante se busca el próximo evento
    tz: str = "America/Bogota"  # huso para interpretar la hora local de los eventos


@dataclass(frozen=True)
class TransportConfig:
    """Config resuelta del daemon (tipos ya cocinados)."""

    mode: TravelMode
    buffer: timedelta
    lead_window: timedelta
    compute_window: timedelta
    horizon: timedelta
    tz: ZoneInfo

    @classmethod
    def from_env(cls, settings: TransportSettings | None = None) -> TransportConfig:
        """Resuelve los settings crudos (o los del entorno si no se pasan) a tipos del dominio."""
        s = settings if settings is not None else TransportSettings()
        return cls(
            mode=TravelMode(s.mode),
            buffer=timedelta(minutes=s.buffer_min),
            lead_window=timedelta(minutes=s.lead_min),
            compute_window=timedelta(minutes=s.compute_window_min),
            horizon=timedelta(hours=s.horizon_hours),
            tz=ZoneInfo(s.tz),
        )
