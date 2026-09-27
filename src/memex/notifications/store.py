"""Acceso a la cola `notifications` — encolar (con colapso por dedup_key) y leer/gestionar.

Capa de datos pura (recibe un `Connection`, no abre conexiones): la usan el `PersistentNotifier`
(escribe) y el router del API (lee/marca). Modelo de cola por timestamps (sin columna `status`):
`read_at`/`dismissed_at`/`expires_at` definen el estado; ver la migración 0075.
"""

from __future__ import annotations

import json
from datetime import time
from typing import Any

from sqlalchemy import Connection, text

from memex.notifications.client import Notification

# Columnas que la vista necesita (omite user_id/dedup_key/updated_at: ruteo interno, no display).
_READ_COLS = (
    "id, kind, severity, audience, title, body, payload, deep_link, "
    "created_at, read_at, dismissed_at, expires_at"
)

# Un aviso está "activo" (visible en cola/campana) si no fue descartado y no venció.
_ACTIVE = "dismissed_at IS NULL AND (expires_at IS NULL OR expires_at > NOW())"


def enqueue(conn: Connection, n: Notification) -> int:
    """Encola la `Notification` y devuelve su id. Idempotente por `(user_id, dedup_key)`:

    si ya existe, refresca el contenido (title/body/severity/payload/deep_link/expires_at) y
    `updated_at`, pero PRESERVA `created_at`/`read_at`/`dismissed_at` (leído/descartado pegajoso:
    una re-emisión idéntica no resucita un aviso ya atendido).
    """
    return int(
        conn.execute(
            text(
                """
            INSERT INTO notifications
                (user_id, kind, severity, audience, title, body, dedup_key, payload, deep_link,
                 created_at, updated_at, expires_at)
            VALUES
                (:uid, :kind, :severity, :audience, :title, :body, :dedup_key,
                 CAST(:payload AS JSONB), :deep_link, :created_at, NOW(), :expires_at)
            ON CONFLICT (user_id, dedup_key) DO UPDATE SET
                kind       = EXCLUDED.kind,
                severity   = EXCLUDED.severity,
                audience   = EXCLUDED.audience,
                title      = EXCLUDED.title,
                body       = EXCLUDED.body,
                payload    = EXCLUDED.payload,
                deep_link  = EXCLUDED.deep_link,
                expires_at = EXCLUDED.expires_at,
                updated_at = NOW()
            RETURNING id
            """
            ),
            {
                "uid": n.user_id,
                "kind": n.kind,
                "severity": n.severity,
                "audience": n.audience,
                "title": n.title,
                "body": n.body,
                "dedup_key": n.dedup_key,
                "payload": json.dumps(n.payload),
                "deep_link": n.deep_link,
                "created_at": n.created_at,
                "expires_at": n.expires_at,
            },
        ).scalar_one()
    )


def list_active(
    conn: Connection, *, user_id: int, limit: int, cursor: int | None = None
) -> list[dict[str, Any]]:
    """Avisos activos del usuario, newest-first (id DESC). Excluye descartados y vencidos.

    Paginación por `cursor` (id de la última fila de la página previa): `id < :cursor`. El idiom
    `:cursor IS NULL OR ...` mantiene el SQL estático (sin armado dinámico).
    """
    rows = (
        conn.execute(
            text(
                f"""
            SELECT {_READ_COLS}
            FROM notifications
            WHERE user_id = :uid
              AND {_ACTIVE}
              AND (CAST(:cursor AS BIGINT) IS NULL OR id < CAST(:cursor AS BIGINT))
            ORDER BY id DESC
            LIMIT :limit
            """
            ),
            {"uid": user_id, "limit": limit, "cursor": cursor},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def count_unread(conn: Connection, *, user_id: int) -> int:
    """Cuántos avisos activos sin leer tiene el usuario (para el badge de la campana)."""
    return int(
        conn.execute(
            text(
                f"""
            SELECT COUNT(*) FROM notifications
            WHERE user_id = :uid AND read_at IS NULL AND {_ACTIVE}
            """
            ),
            {"uid": user_id},
        ).scalar_one()
    )


def mark_read(conn: Connection, *, notification_id: int, user_id: int) -> bool:
    """Marca leído (sale del conteo de no-leídas). False si el id no es del usuario (404)."""
    found = conn.execute(
        text(
            """
            UPDATE notifications SET read_at = COALESCE(read_at, NOW()), updated_at = NOW()
            WHERE id = :id AND user_id = :uid
            RETURNING id
            """
        ),
        {"id": notification_id, "uid": user_id},
    ).scalar()
    return found is not None


def dismiss(conn: Connection, *, notification_id: int, user_id: int) -> bool:
    """Descarta (sale de la cola activa). Devuelve False si el id no es del usuario (404)."""
    found = conn.execute(
        text(
            """
            UPDATE notifications
            SET dismissed_at = COALESCE(dismissed_at, NOW()), updated_at = NOW()
            WHERE id = :id AND user_id = :uid
            RETURNING id
            """
        ),
        {"id": notification_id, "uid": user_id},
    ).scalar()
    return found is not None


def mark_all_read(conn: Connection, *, user_id: int) -> int:
    """Marca leídos todos los avisos activos sin leer del usuario. Devuelve cuántos cambió."""
    return int(
        conn.execute(
            text(
                f"""
            UPDATE notifications SET read_at = NOW(), updated_at = NOW()
            WHERE user_id = :uid AND read_at IS NULL AND {_ACTIVE}
            """
            ),
            {"uid": user_id},
        ).rowcount
    )


def purge_expired(conn: Connection) -> int:
    """Borra físicamente los avisos vencidos (housekeeping). Devuelve cuántos borró.

    La lectura ya oculta los vencidos; esto solo recupera espacio. Es global (todos los usuarios).
    """
    return int(
        conn.execute(
            text("DELETE FROM notifications WHERE expires_at IS NOT NULL AND expires_at <= NOW()")
        ).rowcount
    )


def exists(conn: Connection, *, user_id: int, dedup_key: str) -> bool:
    """¿Ya hay un aviso con esa clave (activo o no)? Los productores lo consultan ANTES de redactar
    con LLM: un aviso ya encolado no se vuelve a pagar ni a refrescar."""
    return (
        conn.execute(
            text("SELECT 1 FROM notifications WHERE user_id = :uid AND dedup_key = :k"),
            {"uid": user_id, "k": dedup_key},
        ).scalar()
        is not None
    )


# ----- canales de entrega (0097) -------------------------------------------------------- #

_CHANNEL_COLS = (
    "id, user_id, channel, target, enabled, subscriptions, quiet_from, quiet_until, config, "
    "created_at, updated_at"
)


def list_channels(
    conn: Connection, *, user_id: int, enabled_only: bool = False
) -> list[dict[str, Any]]:
    rows = (
        conn.execute(
            text(
                f"""
            SELECT {_CHANNEL_COLS} FROM notification_channels
            WHERE user_id = :uid AND (NOT :only OR enabled)
            ORDER BY id
            """
            ),
            {"uid": user_id, "only": enabled_only},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def get_channel(conn: Connection, *, user_id: int, channel_id: int) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(
                f"SELECT {_CHANNEL_COLS} FROM notification_channels "
                "WHERE user_id = :uid AND id = :id"
            ),
            {"uid": user_id, "id": channel_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def upsert_channel(
    conn: Connection,
    *,
    user_id: int,
    channel: str,
    target: str,
    subscriptions: dict[str, str],
    quiet_from: time | None = None,
    quiet_until: time | None = None,
    config: dict[str, Any] | None = None,
) -> int:
    """Alta (o reemplazo) de un canal por `(user_id, channel, target)`; devuelve su id. Un canal
    re-dado de alta conserva `created_at` (los avisos anteriores a su alta no se le entregan)."""
    return int(
        conn.execute(
            text(
                """
            INSERT INTO notification_channels
                (user_id, channel, target, enabled, subscriptions, quiet_from, quiet_until, config)
            VALUES (:uid, :channel, :target, TRUE, CAST(:subs AS JSONB), :qf, :qu,
                    CAST(:config AS JSONB))
            ON CONFLICT (user_id, channel, target) DO UPDATE SET
                enabled = TRUE,
                subscriptions = EXCLUDED.subscriptions,
                quiet_from = EXCLUDED.quiet_from,
                quiet_until = EXCLUDED.quiet_until,
                config = EXCLUDED.config,
                updated_at = NOW()
            RETURNING id
            """
            ),
            {
                "uid": user_id,
                "channel": channel,
                "target": target,
                "subs": json.dumps(subscriptions),
                "qf": quiet_from,
                "qu": quiet_until,
                "config": json.dumps(config or {}),
            },
        ).scalar_one()
    )


def update_channel(
    conn: Connection,
    *,
    user_id: int,
    channel_id: int,
    enabled: bool | None = None,
    subscriptions: dict[str, str] | None = None,
    quiet: tuple[time | None, time | None] | None = None,
) -> bool:
    """Cambios parciales de un canal (activo, suscripciones, silencio). False si no es del user."""
    current = get_channel(conn, user_id=user_id, channel_id=channel_id)
    if current is None:
        return False
    qf, qu = quiet if quiet is not None else (current["quiet_from"], current["quiet_until"])
    conn.execute(
        text(
            """
            UPDATE notification_channels
               SET enabled = :enabled, subscriptions = CAST(:subs AS JSONB),
                   quiet_from = :qf, quiet_until = :qu, updated_at = NOW()
             WHERE user_id = :uid AND id = :id
            """
        ),
        {
            "uid": user_id,
            "id": channel_id,
            "enabled": current["enabled"] if enabled is None else enabled,
            "subs": json.dumps(
                current["subscriptions"] if subscriptions is None else subscriptions
            ),
            "qf": qf,
            "qu": qu,
        },
    )
    return True


def remove_channel(conn: Connection, *, user_id: int, channel_id: int) -> bool:
    found = conn.execute(
        text("DELETE FROM notification_channels WHERE user_id = :uid AND id = :id RETURNING id"),
        {"uid": user_id, "id": channel_id},
    ).scalar()
    return found is not None


# ----- entregas (0097) ------------------------------------------------------------------ #


def pending_deliveries(
    conn: Connection, *, user_id: int, max_attempts: int = 3, retry_after_s: int = 300
) -> list[dict[str, Any]]:
    """Pares (aviso, canal) que faltan entregar: aviso activo, NO leído ni descartado (si el usuario
    ya lo atendió en el dashboard no se le empuja), posterior al alta del canal, cuya audiencia el
    canal suscribe, y sin entrega `enviada` (las `fallida` reintentan hasta `max_attempts`, con
    `retry_after_s` entre intentos). La severidad mínima y las horas de silencio se resuelven en
    Python (`deliver.py`) porque dependen de la zona horaria del usuario."""
    rows = (
        conn.execute(
            text(
                """
            SELECT n.id AS notification_id, n.kind, n.severity, n.audience, n.title, n.body,
                   n.payload, n.deep_link, n.created_at, n.expires_at,
                   c.id AS channel_id, c.channel, c.target, c.subscriptions, c.quiet_from,
                   c.quiet_until, c.config, COALESCE(d.attempts, 0) AS attempts
            FROM notifications n
            JOIN notification_channels c ON c.user_id = n.user_id AND c.enabled
            LEFT JOIN notification_deliveries d
                   ON d.notification_id = n.id AND d.channel_id = c.id
            WHERE n.user_id = :uid
              AND n.read_at IS NULL AND n.dismissed_at IS NULL
              AND (n.expires_at IS NULL OR n.expires_at > NOW())
              AND n.created_at >= c.created_at
              AND jsonb_exists(c.subscriptions, n.audience)
              AND (d.id IS NULL
                   OR (d.status = 'fallida' AND d.attempts < :max_attempts
                       AND d.updated_at < NOW() - make_interval(secs => :retry)))
            ORDER BY n.id, c.id
            """
            ),
            {"uid": user_id, "max_attempts": max_attempts, "retry": retry_after_s},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def record_delivery(
    conn: Connection,
    *,
    notification_id: int,
    channel_id: int,
    status: str,
    external_ref: str | None = None,
    error: str | None = None,
) -> None:
    """Registra el resultado de un intento (upsert por (aviso, canal); `attempts` acumula)."""
    conn.execute(
        text(
            """
            INSERT INTO notification_deliveries
                (notification_id, channel_id, status, attempts, external_ref, error, sent_at)
            VALUES (:nid, :cid, :status, 1, :ref, :error,
                    CASE WHEN :status = 'enviada' THEN NOW() END)
            ON CONFLICT (notification_id, channel_id) DO UPDATE SET
                status = EXCLUDED.status,
                attempts = notification_deliveries.attempts + 1,
                external_ref = COALESCE(EXCLUDED.external_ref,
                                        notification_deliveries.external_ref),
                error = EXCLUDED.error,
                sent_at = COALESCE(EXCLUDED.sent_at, notification_deliveries.sent_at),
                updated_at = NOW()
            """
        ),
        {
            "nid": notification_id,
            "cid": channel_id,
            "status": status,
            "ref": external_ref,
            "error": error[:1000] if error else None,
        },
    )


def list_deliveries(conn: Connection, *, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Últimas entregas del usuario (para `memex-notifications deliveries` y el panel)."""
    rows = (
        conn.execute(
            text(
                """
            SELECT d.id, d.notification_id, d.channel_id, c.channel, c.target, d.status,
                   d.attempts, d.external_ref, d.error, d.sent_at, d.updated_at,
                   n.kind, n.audience, n.severity, n.title
            FROM notification_deliveries d
            JOIN notification_channels c ON c.id = d.channel_id
            JOIN notifications n ON n.id = d.notification_id
            WHERE c.user_id = :uid
            ORDER BY d.id DESC
            LIMIT :limit
            """
            ),
            {"uid": user_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]
