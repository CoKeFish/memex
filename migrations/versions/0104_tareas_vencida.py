"""Tareas: estado `vencida`.

Revision ID: 0104_tareas_vencida
Revises: 0103_tareas_extraccion

Una tarea atada a una sesión («preparación para la próxima clase») o de un asunto de tipo
`programa` (un curso) deja de valer cuando pasa su fecha: la clase ya ocurrió. Antes quedaba
`pendiente` y «vencida» para siempre, como si aún se pudiera hacer. `vencida` es un cierre distinto
de `completada` (no dice que se hizo) y de `cancelada` (no fue decisión de la persona). Se puede
reabrir (`pendiente`). Decisión del dueño, 2026-09-13.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0104_tareas_vencida"
down_revision: str | None = "0103_tareas_extraccion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_tareas DROP CONSTRAINT mod_tareas_status_check;
        ALTER TABLE mod_tareas
            ADD CONSTRAINT mod_tareas_status_check
                CHECK (status IN ('pendiente','completada','cancelada','vencida'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE mod_tareas SET status = 'pendiente' WHERE status = 'vencida';
        ALTER TABLE mod_tareas DROP CONSTRAINT mod_tareas_status_check;
        ALTER TABLE mod_tareas
            ADD CONSTRAINT mod_tareas_status_check
                CHECK (status IN ('pendiente','completada','cancelada'));
        """
    )
