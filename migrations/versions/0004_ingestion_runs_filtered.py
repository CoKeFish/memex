"""ingestion_runs: contador filtered (drops pre-ingest)

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-29

Persiste el volumen de descarte del filtro pre-ingest (ADR-011, drop puro) en la
state table de runs (ADR-007). Hasta ahora `ingestion_runs` guardaba
`posted/inserted/duplicates/errors` pero NO cuántos records dropeó el filtro: ese
dato solo vivía en un structlog efímero agregado por `rule_id`. Sin él, el feedback
loop de costo/valor (ADR-002/011) no puede medir cuánto ahorra cada regla.

- `filtered INT NOT NULL DEFAULT 0` — nº de records descartados por `filter_rules`
  antes de tocar `inbox`, por run. Simétrico con los otros contadores (columna de
  primera clase, no `metadata`), para que sea consultable por SQL.

Invariante esperado por run: `posted = inserted + duplicates + errors + filtered`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ingestion_runs
            ADD COLUMN filtered INT NOT NULL DEFAULT 0;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE ingestion_runs
            DROP COLUMN IF EXISTS filtered;
        """
    )
