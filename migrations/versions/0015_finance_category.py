"""mod_finance_expenses.category

Agrega `category` (rubro) al gasto extraído: el módulo finance ahora pide al LLM elegir de una
lista cerrada de categorías (comida, transporte, ...), default 'otros'. Antes la categoría era
DERIVADA en la UI; ahora se persiste.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_finance_expenses
            ADD COLUMN category TEXT NOT NULL DEFAULT 'otros';
        CREATE INDEX mod_finance_expenses_category
            ON mod_finance_expenses (user_id, category);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS mod_finance_expenses_category;
        ALTER TABLE mod_finance_expenses DROP COLUMN IF EXISTS category;
        """
    )
