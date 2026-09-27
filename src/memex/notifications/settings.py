"""Ajustes del daemon de notificaciones: env (bootstrap) + DB (manda en runtime).

Env `MEMEX_NOTIFICATIONS_*` (`.env`): identidad del proceso (user, tick) y credenciales de canal
(el token del bot de Telegram se toma del asistente si no hay uno propio: es el MISMO bot). Todo lo
que el dueño cambia en caliente vive en la DB (0097): `notification_settings` (daemon prendido,
zona horaria) y `notification_producers` (por productor: activo, redacción `plantilla`|`llm`, y sus
tiempos en `config`). Sin fila, el productor usa los defaults que declara en código.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Connection, text

RENDERS = ("plantilla", "llm")


class NotificationsEnv(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMEX_NOTIFICATIONS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    user_id: int = 1
    #: Cada cuánto corre un ciclo (productores + entregas). Los recordatorios de eventos dependen de
    #: esta granularidad: 30 s alcanza para «60 minutos antes».
    tick_seconds: float = 30.0
    #: Token del bot de Telegram para el canal `telegram`. Default: el del asistente (mismo bot;
    #: enviar no choca con el polling del asistente, solo `getUpdates` es exclusivo).
    telegram_bot_token: str = Field(
        default="",
        validation_alias=AliasChoices(
            "MEMEX_NOTIFICATIONS_TELEGRAM_BOT_TOKEN", "MEMEX_ASSISTANT_TELEGRAM_BOT_TOKEN"
        ),
    )
    #: Base pública del dashboard para volver `deep_link` un enlace en el mensaje. Vacío = sin link.
    dashboard_url: str = ""
    #: Zona horaria de bootstrap (la DB manda si hay fila).
    timezone: str = "America/Bogota"
    #: Reintentos de entrega por (aviso, canal) y espera entre intentos.
    max_attempts: int = 3
    retry_after_s: int = 300


@dataclass(frozen=True)
class NotificationSettings:
    daemon_enabled: bool = False
    timezone: str = "America/Bogota"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def get_settings(
    conn: Connection, user_id: int, *, default_tz: str = "America/Bogota"
) -> NotificationSettings:
    row = (
        conn.execute(
            text("SELECT daemon_enabled, timezone FROM notification_settings WHERE user_id = :uid"),
            {"uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return NotificationSettings(timezone=default_tz)
    return NotificationSettings(
        daemon_enabled=bool(row["daemon_enabled"]), timezone=str(row["timezone"])
    )


def upsert_settings(
    conn: Connection,
    user_id: int,
    *,
    daemon_enabled: bool | None = None,
    timezone: str | None = None,
) -> NotificationSettings:
    current = get_settings(conn, user_id)
    if timezone is not None:
        ZoneInfo(timezone)  # valida: zona inexistente → ZoneInfoNotFoundError
    resolved = NotificationSettings(
        daemon_enabled=current.daemon_enabled if daemon_enabled is None else daemon_enabled,
        timezone=current.timezone if timezone is None else timezone,
    )
    conn.execute(
        text(
            """
            INSERT INTO notification_settings (user_id, daemon_enabled, timezone)
            VALUES (:uid, :enabled, :tz)
            ON CONFLICT (user_id) DO UPDATE SET
                daemon_enabled = EXCLUDED.daemon_enabled, timezone = EXCLUDED.timezone,
                updated_at = NOW()
            """
        ),
        {"uid": user_id, "enabled": resolved.daemon_enabled, "tz": resolved.timezone},
    )
    return resolved


@dataclass(frozen=True)
class ProducerSettings:
    """Lo que el dueño configuró para un productor (o sus defaults si no hay fila)."""

    enabled: bool
    render: str
    config: dict[str, Any]
    from_db: bool = False


def get_producer_settings(
    conn: Connection,
    user_id: int,
    producer: str,
    *,
    default_enabled: bool,
    default_render: str,
    default_config: dict[str, Any],
) -> ProducerSettings:
    """Fila del productor (si existe) sobre sus defaults: `config` se MEZCLA clave a clave, así un
    productor que gana una clave nueva la usa sin tocar las filas viejas."""
    row = (
        conn.execute(
            text(
                "SELECT enabled, render, config FROM notification_producers "
                "WHERE user_id = :uid AND producer = :p"
            ),
            {"uid": user_id, "p": producer},
        )
        .mappings()
        .first()
    )
    if row is None:
        return ProducerSettings(default_enabled, default_render, dict(default_config))
    return ProducerSettings(
        enabled=bool(row["enabled"]),
        render=str(row["render"]),
        config={**default_config, **dict(row["config"] or {})},
        from_db=True,
    )


def coerce_config(default_config: dict[str, Any], changes: dict[str, str]) -> dict[str, Any]:
    """Convierte `clave=valor` del CLI/API al tipo del default del productor: int, float, bool
    («si/no/true/false»), lista (coma-separada) o texto. Clave desconocida → ValueError."""
    out: dict[str, Any] = {}
    for key, raw in changes.items():
        if key not in default_config:
            raise ValueError(f"clave desconocida: {key!r}; válidas: {sorted(default_config)}")
        default = default_config[key]
        value: Any
        if isinstance(default, bool):
            value = raw.strip().lower() in ("1", "si", "sí", "true", "on", "yes")
        elif isinstance(default, int):
            value = int(raw)
        elif isinstance(default, float):
            value = float(raw)
        elif isinstance(default, list):
            value = [p.strip() for p in raw.split(",") if p.strip()]
        else:
            value = raw.strip()
            if key.startswith("hora"):
                parse_hhmm(value)  # valida
        out[key] = value
    return out


def upsert_producer_settings(
    conn: Connection,
    user_id: int,
    producer: str,
    *,
    default_enabled: bool,
    default_render: str,
    default_config: dict[str, Any],
    enabled: bool | None = None,
    render: str | None = None,
    config_changes: dict[str, Any] | None = None,
) -> ProducerSettings:
    if render is not None and render not in RENDERS:
        raise ValueError(f"render inválido: {render!r}; válidos: {RENDERS}")
    current = get_producer_settings(
        conn,
        user_id,
        producer,
        default_enabled=default_enabled,
        default_render=default_render,
        default_config=default_config,
    )
    resolved = ProducerSettings(
        enabled=current.enabled if enabled is None else enabled,
        render=current.render if render is None else render,
        config={**current.config, **(config_changes or {})},
        from_db=True,
    )
    conn.execute(
        text(
            """
            INSERT INTO notification_producers (user_id, producer, enabled, render, config)
            VALUES (:uid, :p, :enabled, :render, CAST(:config AS JSONB))
            ON CONFLICT (user_id, producer) DO UPDATE SET
                enabled = EXCLUDED.enabled, render = EXCLUDED.render, config = EXCLUDED.config,
                updated_at = NOW()
            """
        ),
        {
            "uid": user_id,
            "p": producer,
            "enabled": resolved.enabled,
            "render": resolved.render,
            "config": json.dumps(resolved.config),
        },
    )
    return resolved


def parse_hhmm(value: str) -> time:
    """«07:00» → time. ValueError si no es HH:MM."""
    try:
        hh, mm = value.strip().split(":")
        return time(int(hh), int(mm))
    except Exception as e:
        raise ValueError(f"hora inválida: {value!r}; formato HH:MM") from e


def local_now(tz: ZoneInfo, now: datetime | None = None) -> datetime:
    """`now` (aware, cualquier zona) llevado a la zona del usuario."""
    base = now if now is not None else datetime.now(tz)
    return base.astimezone(tz)
