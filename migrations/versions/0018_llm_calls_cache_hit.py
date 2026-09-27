"""llm_calls.cache_hit_tokens

Agrega cache_hit_tokens (tokens de prompt servidos desde el cache del proveedor,
más baratos) a llm_calls. Hasta ahora solo se guardaba prompt_tokens total, así
que la eficiencia de cache no se podía calcular post-hoc. cache_miss queda
derivable como (prompt_tokens - cache_hit_tokens). DEFAULT 0 mantiene las filas
viejas válidas y los eventos sin usage (filtered/error) en cero.

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE llm_calls
            ADD COLUMN cache_hit_tokens INT NOT NULL DEFAULT 0;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE llm_calls DROP COLUMN IF EXISTS cache_hit_tokens;
        """
    )
