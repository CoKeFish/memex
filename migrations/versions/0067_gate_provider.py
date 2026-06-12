"""relevance_gate_settings.provider + codex_model: proveedor del gate seleccionable

Revision ID: 0067_gate_provider
Revises: 0066_mining_threshold
Create Date: 2026-06-12

Codex (vía `codex exec`, suscripción del dueño) se promueve de flag experimental del CLI a
proveedor de primera clase del gate: el worker construye su cliente según
`settings.provider`. `codex_model` es el modelo de codex (NULL = el default del CLI); la
columna `model` existente sigue siendo del path Anthropic. Restricción operativa documentada
(no de schema): codex solo funciona host-side (binario + `codex login` en la máquina), no en
el contenedor — con provider='codex' las corridas dentro del contenedor fallan con error
accionable.

Numeración (migration-numbering-worktrees): 0067 verificado libre; head lineal = 0066.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0067_gate_provider"
down_revision: str | None = "0066_mining_threshold"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE relevance_gate_settings
            ADD COLUMN provider TEXT NOT NULL DEFAULT 'anthropic'
                CHECK (provider IN ('anthropic', 'codex')),
            ADD COLUMN codex_model TEXT;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE relevance_gate_settings
            DROP COLUMN IF EXISTS codex_model,
            DROP COLUMN IF EXISTS provider;
        """
    )
