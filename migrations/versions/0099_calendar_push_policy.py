"""Política de write-back por cuenta de calendario: qué estados salen y desde cuándo

Revision ID: 0099_calendar_push_policy
Revises: 0098_accounts_kind_notes
Create Date: 2026-09-11

El push (write-back) empujaba TODA la vista consolidada a la cuenta externa: anuncios, eventos
provisionales y el histórico completo. Lo que sale al calendario real del usuario debe ser lo que
él da por cierto: por default solo los eventos `confirmado` y solo los que todavía no terminaron.

- `push_commitments`: estados que se escriben en el proveedor (subconjunto de
  confirmado|provisional|anuncio). Default `{confirmado}`.
- `push_past_days`: piso temporal. Sale un evento cuya ÚLTIMA ocurrencia cae en
  `hoy - push_past_days` o después (0 = desde hoy; NULL = sin piso, todo el histórico). Una serie
  sin fin siempre sale. Default 0.

Un evento ya escrito que deja de cumplir la política (se degrada a provisional, vence el piso) se
BORRA del proveedor en el siguiente push (misma vía que el tombstone).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0099_calendar_push_policy"
down_revision: str | None = "0098_accounts_kind_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_calendar_provider_accounts
            ADD COLUMN push_commitments TEXT[] NOT NULL DEFAULT '{confirmado}',
            ADD COLUMN push_past_days   INTEGER DEFAULT 0,
            ADD CONSTRAINT mod_calendar_provider_accounts_push_commitments_check
                CHECK (push_commitments <@ ARRAY['confirmado', 'provisional', 'anuncio']::TEXT[]),
            ADD CONSTRAINT mod_calendar_provider_accounts_push_past_days_check
                CHECK (push_past_days IS NULL OR push_past_days >= 0);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_calendar_provider_accounts
            DROP CONSTRAINT IF EXISTS mod_calendar_provider_accounts_push_past_days_check,
            DROP CONSTRAINT IF EXISTS mod_calendar_provider_accounts_push_commitments_check,
            DROP COLUMN IF EXISTS push_past_days,
            DROP COLUMN IF EXISTS push_commitments;
        """
    )
