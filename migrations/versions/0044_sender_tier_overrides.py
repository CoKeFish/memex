"""calidad: sender_tier_overrides — "no procesar" un remitente (tier forzado hacia adelante)

Revision ID: 0044_sender_tier_overrides
Revises: 0043_relevance_marks
Create Date: 2026-06-08

Acción asistida del sistema de calidad: el usuario decide que un remitente es de bajo valor y manda
sus mensajes FUTUROS a un tier fijo (típicamente `blacklist` = "no procesar": se guardan en inbox pero
no se les gasta resumen/extracción LLM). NO borra (eso es `filter_rules ignore`); conserva la memoria.
El classifier consulta esta tabla ANTES de `classify()` y usa el tier del override si el remitente
matchea por email. UNIQUE(user_id, sender_email): un override por remitente, se actualiza al re-setear.
Solo afecta mensajes AÚN NO clasificados (el worker inserta con ON CONFLICT DO NOTHING) — prospectivo.

`downgrade` dropea la tabla.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0044_sender_tier_overrides"
down_revision: str | None = "0043_relevance_marks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sender_tier_overrides (
            id           BIGSERIAL PRIMARY KEY,
            user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            sender_email TEXT NOT NULL,
            tier         TEXT NOT NULL CHECK (tier IN ('blacklist','batch','individual')),
            reason       TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, sender_email)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sender_tier_overrides CASCADE;")
