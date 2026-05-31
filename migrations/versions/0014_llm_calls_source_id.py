"""llm_calls.source_id

Agrega source_id (FK sources, nullable) a llm_calls para atribución de costo por
source — además de inbox_id, que no sirve para llamadas batch (1 llm_call cubre
muchas filas inbox). Index parcial para agregación por source.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE llm_calls
            ADD COLUMN source_id BIGINT REFERENCES sources(id) ON DELETE SET NULL;
        CREATE INDEX llm_calls_source_created
            ON llm_calls (source_id, created_at DESC) WHERE source_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS llm_calls_source_created;
        ALTER TABLE llm_calls DROP COLUMN IF EXISTS source_id;
        """
    )
