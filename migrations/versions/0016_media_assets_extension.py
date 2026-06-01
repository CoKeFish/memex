"""media_assets.extension — extensión normalizada del adjunto

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-31

Guarda la extensión del adjunto (sin punto, lowercase: 'pdf', 'png', 'zip') como campo propio,
para filtrar/mostrar por tipo sin parsear `filename`/`object_key` en cada query. Nullable: hay
adjuntos sin nombre. Backfill best-effort desde `filename` (último segmento tras el punto).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE media_assets ADD COLUMN extension TEXT;
        UPDATE media_assets
           SET extension = lower(reverse(split_part(reverse(filename), '.', 1)))
         WHERE filename LIKE '%.%';
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE media_assets DROP COLUMN IF EXISTS extension;")
