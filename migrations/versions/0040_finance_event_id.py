"""finance: event_id en mod_finance_transactions (correlación cross-module por agente)

Revision ID: 0040_finance_event_id
Revises: 0039_bienestar_habits
Create Date: 2026-06-06

`event_id` correlaciona una transacción con otros hechos del MISMO mensaje del agente (Hermes lo pasa
con `--event`): la capa de relaciones teje la arista (productor `event`) entre la transacción y, p.ej.,
un registro de bienestar del mismo evento. NULL = transacción sin correlación (las de extracción de
correos la dejan NULL); lo setea SOLO la entrada determinista por agente (`finance.register`).

`downgrade` dropea la columna + el índice.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0040_finance_event_id"
down_revision: str | None = "0039_bienestar_habits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE mod_finance_transactions ADD COLUMN event_id TEXT;")
    op.execute(
        "CREATE INDEX mod_finance_transactions_user_event "
        "ON mod_finance_transactions (user_id, event_id);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS mod_finance_transactions_user_event;")
    op.execute("ALTER TABLE mod_finance_transactions DROP COLUMN IF EXISTS event_id;")
