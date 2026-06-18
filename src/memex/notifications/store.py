"""Acceso a la cola `notifications` — encolar (con colapso por dedup_key) y leer/gestionar.

Capa de datos pura (recibe un `Connection`, no abre conexiones): la usan el `PersistentNotifier`
(escribe) y el router del API (lee/marca). Modelo de cola por timestamps (sin columna `status`):
`read_at`/`dismissed_at`/`expires_at` definen el estado; ver la migración 0075.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, text

from memex.notifications.client import Notification

# Columnas que la vista necesita (omite user_id/dedup_key/updated_at: ruteo interno, no display).
_READ_COLS = (
    "id, kind, severity, title, body, payload, deep_link, "
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
                (user_id, kind, severity, title, body, dedup_key, payload, deep_link,
                 created_at, updated_at, expires_at)
            VALUES
                (:uid, :kind, :severity, :title, :body, :dedup_key, CAST(:payload AS JSONB),
                 :deep_link, :created_at, NOW(), :expires_at)
            ON CONFLICT (user_id, dedup_key) DO UPDATE SET
                kind       = EXCLUDED.kind,
                severity   = EXCLUDED.severity,
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
