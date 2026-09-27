"""asuntos: fecha de vencimiento (`due_on`) del próximo paso; vencida sin señal nueva → cierre automático

Revision ID: 0096_asuntos_due_on
Revises: 0095_asuntos_priority
Create Date: 2026-09-10

Caso del dueño (2026-09-10): «agent-bazaar-mcp» quedó abierto con prioridad alta por un plazo de 48
horas que había vencido hacía dos semanas. El único cierre por fecha que existía cubría eventos y
hackatones con hito; un plazo («elimínalo antes de…», «actualiza la facturación antes del 14»,
«formaliza la matrícula hasta el 11») no cerraba nada, del tipo que fuera.

`due_on` es la fecha límite del PRÓXIMO paso tal como la lee el asignador del texto (o la fija el
usuario: `memex asuntos vence`). Regla del sistema: `due_on` pasada hace más de la gracia y sin
señal posterior → `cerrado` «venció el <fecha> sin novedad». También alimenta la prioridad por plazo
cercano. La palabra del usuario (`metadata.due_source = user`) no la pisa el modelo.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0096_asuntos_due_on"
down_revision: str | None = "0095_asuntos_priority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_asuntos ADD COLUMN due_on DATE;
        CREATE INDEX ix_mod_asuntos_user_due
            ON mod_asuntos (user_id, due_on) WHERE merged_into_id IS NULL AND due_on IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS ix_mod_asuntos_user_due;
        ALTER TABLE mod_asuntos DROP COLUMN IF EXISTS due_on;
        """
    )
