"""Config del daemon de ingesta (env-driven, solo bootstrap).

A diferencia del scheduler de procesamiento, acá NO hay un `enabled_jobs` en el env: las fuentes a
agendar salen siempre de la DB (`sources.fetch_schedule` + `ingest_scheduler_settings`). El env solo
fija el `user_id` que vigila el daemon y el `tick_seconds` del loop. Prefijo propio
(`MEMEX_INGEST_SCHEDULER_`) para no chocar con `memex.config.Settings` ni con `MEMEX_SCHEDULER_`.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class IngestSchedulerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMEX_INGEST_SCHEDULER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    user_id: int = 1
    tick_seconds: float = 5.0
