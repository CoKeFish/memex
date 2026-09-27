"""notificaciones: audiencia (personal|sistema), canales de entrega, entregas y ajustes del daemon

Revision ID: 0097_notifications_delivery
Revises: 0096_asuntos_due_on
Create Date: 2026-09-10

Hasta acá la cola `notifications` (0075) era SOLO de lectura desde el dashboard: nadie la empujaba a
ningún lado. Pedido del dueño (2026-09-10): un daemon de notificaciones modular que entregue por
canales (Telegram primero) y que distinga dos audiencias: lo INTERNO de memex (errores, corridas
fallidas, fuentes paradas) y lo PERSONAL (resumen matutino, recordatorios de eventos, plazos).

- `notifications.audience`: `personal` (default; lo que ya emitía transporte lo es) | `sistema`.
- `notification_channels`: a dónde se entrega (canal + destino, p. ej. telegram + chat_id), qué
  audiencias recibe con qué severidad mínima (`subscriptions` JSONB: {"personal": "info",
  "sistema": "critica"}), horas de silencio (solo lo crítico pasa) y config del canal.
- `notification_deliveries`: una fila por (aviso, canal) → envío EXACTAMENTE una vez; las fallidas
  reintentan con tope. Lo leído o descartado en el dashboard antes de entregarse NO se envía.
- `notification_settings`: control runtime del daemon (apagado por default, la DB manda) + zona
  horaria del usuario para horas y plazos.
- `notification_producers`: por productor (resumen_diario, recordatorio_evento, …) si está activo,
  cómo se redacta (`plantilla` determinista o `llm`) y sus tiempos (`config` JSONB: hora,
  anticipación…). Sin fila → los defaults que declara el productor en código.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0097_notifications_delivery"
down_revision: str | None = "0096_asuntos_due_on"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE notifications
            ADD COLUMN audience TEXT NOT NULL DEFAULT 'personal'
                CHECK (audience IN ('personal', 'sistema'));

        CREATE TABLE notification_settings (
            user_id        BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            daemon_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            timezone       TEXT    NOT NULL DEFAULT 'America/Bogota',
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE notification_producers (
            user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            producer   TEXT   NOT NULL CHECK (length(btrim(producer)) > 0),
            enabled    BOOLEAN NOT NULL DEFAULT TRUE,
            render     TEXT   NOT NULL DEFAULT 'plantilla' CHECK (render IN ('plantilla', 'llm')),
            config     JSONB  NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, producer)
        );

        CREATE TABLE notification_channels (
            id            BIGSERIAL PRIMARY KEY,
            user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            channel       TEXT   NOT NULL CHECK (length(btrim(channel)) > 0),
            target        TEXT   NOT NULL DEFAULT '',
            enabled       BOOLEAN NOT NULL DEFAULT TRUE,
            subscriptions JSONB  NOT NULL DEFAULT '{"personal": "info", "sistema": "critica"}'::jsonb,
            quiet_from    TIME,
            quiet_until   TIME,
            config        JSONB  NOT NULL DEFAULT '{}'::jsonb,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, channel, target)
        );

        CREATE TABLE notification_deliveries (
            id              BIGSERIAL PRIMARY KEY,
            notification_id BIGINT NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
            channel_id      BIGINT NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
            status          TEXT   NOT NULL CHECK (status IN ('enviada', 'fallida')),
            attempts        INTEGER NOT NULL DEFAULT 1,
            external_ref    TEXT,
            error           TEXT,
            sent_at         TIMESTAMPTZ,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (notification_id, channel_id)
        );
        CREATE INDEX notification_deliveries_channel ON notification_deliveries (channel_id, id DESC);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS notification_deliveries CASCADE;
        DROP TABLE IF EXISTS notification_channels CASCADE;
        DROP TABLE IF EXISTS notification_producers CASCADE;
        DROP TABLE IF EXISTS notification_settings CASCADE;
        ALTER TABLE notifications DROP COLUMN IF EXISTS audience;
        """
    )
