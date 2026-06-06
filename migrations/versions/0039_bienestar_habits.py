"""bienestar: mod_bienestar_habits (hábitos; la adherencia se calcula en lectura)

Revision ID: 0039_bienestar_habits
Revises: 0038_bienestar_registros
Create Date: 2026-06-06

Capa de hábitos del módulo bienestar: un hábito = un compromiso recurrente ("cepillarme 2x/día") que
el usuario define por CLI/agente. La ADHERENCIA y la racha NO se persisten — se calculan en lectura
contando los registros (`mod_bienestar_registros`) que matchean por `activity` (o `category`) por
ventana de cadencia, en la TZ de display. Determinista, sin LLM.

`downgrade` dropea la tabla.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0039_bienestar_habits"
down_revision: str | None = "0038_bienestar_registros"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mod_bienestar_habits (
            id           BIGSERIAL PRIMARY KEY,
            user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name         TEXT NOT NULL,
            activity     TEXT NOT NULL DEFAULT '',
            category     TEXT CHECK (category IS NULL OR category IN
                           ('comida','higiene','ejercicio','grooming','salud','otros')),
            cadence      TEXT NOT NULL CHECK (cadence IN ('daily','weekly')),
            target_count INT NOT NULL DEFAULT 1 CHECK (target_count >= 1),
            active       BOOLEAN NOT NULL DEFAULT TRUE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (activity <> '' OR category IS NOT NULL)
        );
        CREATE INDEX mod_bienestar_habits_user_active
            ON mod_bienestar_habits (user_id, active);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mod_bienestar_habits CASCADE;")
