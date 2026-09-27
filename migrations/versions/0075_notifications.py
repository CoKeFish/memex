"""notifications: cola persistida de avisos al usuario (servicio de notificaciones general)

Respalda el seam `memex.notifications.Notifier` con persistencia real. Hoy el único emisor es el
daemon de transporte (`kind='transport.leave_by'`), pero la tabla es de USO GENERAL: cualquier
emisor encola su `kind` y el `PersistentNotifier` colapsa repetidos por `(user_id, dedup_key)`.

Modelo de cola por timestamps (no hay columna `status`):
- encolado     → `read_at`/`dismissed_at` NULL.
- leído        → `read_at` (sale del conteo de no-leídas).
- descartado   → `dismissed_at` (sale de la cola activa / la vista por defecto).
- vencido      → `expires_at <= now()` (oculto en lectura; purgable con `memex-notifications purge`).
`UNIQUE (user_id, dedup_key)` es la clave de idempotencia: el daemon re-emite el MISMO aviso cada
~10 min y `INSERT ... ON CONFLICT DO UPDATE` refresca el contenido preservando created_at/read_at/
dismissed_at (leído/descartado pegajoso).

Índices: `notifications_active` (parcial, no-descartados) sirve el listado newest-first por usuario;
`notifications_expiry` (parcial) acelera la purga. OJO: `NOW()` no es IMMUTABLE → el vencimiento se
filtra en la query, NO puede ir en el predicado de un índice parcial.

Numeración (migration-numbering-worktrees): 0075 verificado libre en los 3 worktrees
(main + identidades-resolution-v3 @ 0074, relevancia-unificada @ 0071); head lineal = 0074.

DOWNGRADE: borra la tabla (los avisos persistidos se pierden; el seam vuelve a un notifier sin cola).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0075_notifications"  # <= 32 chars (alembic_version.version_num)
down_revision: str | None = "0074_identidades_desconocido"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE notifications (
            id           BIGSERIAL PRIMARY KEY,
            user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind         TEXT NOT NULL CHECK (length(btrim(kind)) > 0),
            severity     TEXT NOT NULL CHECK (severity IN ('info','alta','critica')),
            title        TEXT NOT NULL,
            body         TEXT NOT NULL DEFAULT '',
            dedup_key    TEXT NOT NULL CHECK (length(btrim(dedup_key)) > 0),
            payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
            deep_link    TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            read_at      TIMESTAMPTZ,
            dismissed_at TIMESTAMPTZ,
            expires_at   TIMESTAMPTZ,
            UNIQUE (user_id, dedup_key)
        );
        CREATE INDEX notifications_active
            ON notifications (user_id, id DESC)
            WHERE dismissed_at IS NULL;
        CREATE INDEX notifications_expiry
            ON notifications (expires_at)
            WHERE expires_at IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS notifications CASCADE;")
