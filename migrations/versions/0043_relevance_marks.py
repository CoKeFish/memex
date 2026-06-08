"""calidad: relevance_marks — override manual de relevancia por mensaje

Revision ID: 0043_relevance_marks
Revises: 0042_module_item_count
Create Date: 2026-06-08

El usuario marca UN mensaje como relevante o no relevante; ese juicio gana sobre la heurística
determinista para ESE mensaje (override duro, ver `quality.relevance`). Marcar uno NO condena a todo
el remitente — la marca es por-mensaje. `is_relevant` es bidireccional: FALSE = "esto no debió
procesarse / es ruido"; TRUE = "sí importaba aunque no extrajo un hecho de dominio". UNIQUE(inbox_id):
una marca por mensaje, se actualiza al re-marcar. Análogo a `inbox_feedback` pero otro eje (relevancia,
no calidad de procesamiento).

`downgrade` dropea la tabla.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0043_relevance_marks"
down_revision: str | None = "0042_module_item_count"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE relevance_marks (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            inbox_id    BIGINT NOT NULL REFERENCES inbox(id) ON DELETE CASCADE,
            is_relevant BOOLEAN NOT NULL,
            reason      TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (inbox_id)
        );
        CREATE INDEX relevance_marks_user ON relevance_marks (user_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS relevance_marks CASCADE;")
