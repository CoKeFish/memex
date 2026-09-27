"""Categoría de la tarea: de qué clase de cosa se trata, para ordenar y agrupar los pendientes.

Revision ID: 0113_tareas_category
Revises: 0112_semantic_vectors

Pedido del dueño (2026-09-26): los avisos de seguridad de sus cuentas (intento de acceso,
contraseña, 2FA) sí deben quedar como tarea «por si acaso», y los pendientes deben poder verse
por prioridad o por categoría. `category` es un vocabulario fijo (`tareas.module.CATEGORIES`):
seguridad, entrega, tramite, pago, inscripcion, dinamica, otro. Backfill: las tareas con puntos
(`metadata.puntos`) son dinámicas; el resto queda `otro` hasta que se re-extraigan.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0113_tareas_category"
down_revision: str | None = "0112_semantic_vectors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE mod_tareas ADD COLUMN category TEXT NOT NULL DEFAULT 'otro'")
    op.execute(
        "ALTER TABLE mod_tareas ADD CONSTRAINT mod_tareas_category_chk CHECK (category IN "
        "('seguridad','entrega','tramite','pago','inscripcion','dinamica','otro'))"
    )
    op.execute("UPDATE mod_tareas SET category = 'dinamica' WHERE metadata ? 'puntos'")
    op.execute("CREATE INDEX mod_tareas_category_idx ON mod_tareas (user_id, category)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS mod_tareas_category_idx")
    op.execute("ALTER TABLE mod_tareas DROP CONSTRAINT IF EXISTS mod_tareas_category_chk")
    op.execute("ALTER TABLE mod_tareas DROP COLUMN IF EXISTS category")
