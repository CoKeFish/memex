"""accounts.kind admite `notes` (cuentas de apps de notas: Notion)

Revision ID: 0098_accounts_kind_notes
Revises: 0097_notifications_delivery
Create Date: 2026-09-10

`SourceKind` gana `notes` (apuntes propios del usuario; primer ingestor: Notion, tipo `notion`). El
token de la integración vive en el vault de una cuenta, y `accounts.kind` tenía un CHECK cerrado a
email|chat|social. `sources.type` no tiene CHECK (no necesita migración).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0098_accounts_kind_notes"
down_revision: str | None = "0097_notifications_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_kind_check;
        ALTER TABLE accounts ADD CONSTRAINT accounts_kind_check
            CHECK (kind IN ('email', 'chat', 'social', 'notes'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM accounts WHERE kind = 'notes';
        ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_kind_check;
        ALTER TABLE accounts ADD CONSTRAINT accounts_kind_check
            CHECK (kind IN ('email', 'chat', 'social'));
        """
    )
