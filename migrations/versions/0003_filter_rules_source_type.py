"""filter_rules: source_type + enabled + lookup index

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-28

Habilita el filtro pre-ingest (Fase 1):

- `source_type TEXT NULL`  — permite reglas que apliquen a todos los sources
  de un tipo (ej. todos los `imap`) en vez de a un `source_id` concreto.
  NULL = aplica a cualquier source_type (regla global del user).
- `enabled BOOLEAN NOT NULL DEFAULT true` — permite desactivar reglas sin
  borrarlas (audit + rollback rápido).
- Index parcial `filter_rules_lookup` para el query del applier: ordenado
  por prioridad descendente, solo reglas habilitadas.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE filter_rules
            ADD COLUMN source_type TEXT,
            ADD COLUMN enabled     BOOLEAN NOT NULL DEFAULT TRUE;

        CREATE INDEX filter_rules_lookup
            ON filter_rules (user_id, source_type, source_id, priority DESC)
            WHERE enabled;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS filter_rules_lookup;
        ALTER TABLE filter_rules
            DROP COLUMN IF EXISTS enabled,
            DROP COLUMN IF EXISTS source_type;
        """
    )
