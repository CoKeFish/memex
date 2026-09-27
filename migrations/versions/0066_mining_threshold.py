"""relevance_gate_settings.mining_min_messages: umbral de acumulación de la minería

Revision ID: 0066_mining_threshold
Revises: 0065_relevance_gate
Create Date: 2026-06-12

La minería de reglas NO debe activarse por un solo correo malo: solo propone reglas cuando una
clase de correos (remitente) acumuló N+ no-relevantes (decisión del dueño). El umbral vive en
los settings del gate para que el job diario y la corrida on-demand lo respeten igual, y sea
ajustable en runtime (API/CLI/UI) sin redeploy. Con ningún remitente en el umbral, la corrida
es no-op SIN llamada LLM.

Numeración (migration-numbering-worktrees): 0066 verificado libre en todas las ramas/worktrees;
head lineal = 0065_relevance_gate.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0066_mining_threshold"
down_revision: str | None = "0065_relevance_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE relevance_gate_settings
            ADD COLUMN mining_min_messages INT NOT NULL DEFAULT 5
                CHECK (mining_min_messages >= 1);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE relevance_gate_settings DROP COLUMN IF EXISTS mining_min_messages;
        """
    )
