"""Procesamiento automático de lo nuevo: ajustes del job `procesar_nuevos`.

Revision ID: 0107_auto_processing
Revises: 0106_whatsapp_chat_description

En dev el procesamiento era manual: un mensaje nuevo del grupo del curso entraba al inbox y no
generaba tareas, datos ni hitos hasta que alguien corría el lote. `auto_processing` guarda, por
user, cómo se procesa solo lo que llega: qué medios (chats elegidos y notas por default), desde
qué marca de agua (`inbox.received_at`), cuántos por corrida y el tope diario de gasto en LLM;
más el rastro de la última corrida para verlo en el dashboard.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0107_auto_processing"
down_revision: str | None = "0106_whatsapp_chat_description"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auto_processing (
            user_id            BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            enabled            BOOLEAN NOT NULL DEFAULT TRUE,
            kinds              TEXT[] NOT NULL DEFAULT ARRAY['chat', 'notes'],
            watermark          TIMESTAMPTZ,
            max_per_run        INTEGER NOT NULL DEFAULT 40,
            daily_cost_cap_usd NUMERIC(10, 4) NOT NULL DEFAULT 1.0,
            last_run_at        TIMESTAMPTZ,
            last_targets       INTEGER NOT NULL DEFAULT 0,
            last_cost_usd      NUMERIC(12, 6) NOT NULL DEFAULT 0,
            last_skip_reason   TEXT NOT NULL DEFAULT '',
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auto_processing CASCADE;")
