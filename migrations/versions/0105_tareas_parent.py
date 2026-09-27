"""Tareas: pasos de un proyecto (`parent_id`).

Revision ID: 0105_tareas_parent
Revises: 0104_tareas_vencida

Un proyecto de curso («el proyecto de Canva») se pide por pasos en mensajes distintos. Cada paso es
una tarea; el proyecto es una tarea PADRE (`metadata.is_parent`) y los pasos le cuelgan por
`parent_id`. Así se ve «proyecto: 1 de 3 pasos» en vez de tres tareas sueltas. Un nivel: una hija no
tiene hijas. Borrar el padre deja a las hijas sueltas (SET NULL), no las borra.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0105_tareas_parent"
down_revision: str | None = "0104_tareas_vencida"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_tareas
            ADD COLUMN parent_id BIGINT REFERENCES mod_tareas(id) ON DELETE SET NULL;
        CREATE INDEX mod_tareas_parent ON mod_tareas (parent_id) WHERE parent_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS mod_tareas_parent;
        ALTER TABLE mod_tareas DROP COLUMN parent_id;
        """
    )
