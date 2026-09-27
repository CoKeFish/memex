"""`mod_tareas.origin` admite `'calendar'`: tareas materializadas desde recordatorios del calendario.

Revision ID: 0114_tareas_origin_calendar
Revises: 0113_tareas_category

«Pagar arriendo y administración» vive en Google Calendar como evento recurrente de día completo
y no aparecía en tareas (2026-09-26). `tareas.recordatorios` materializa cada ocurrencia como
tarea con `origin = 'calendar'` (ni manual ni extraída de un mensaje): así la re-extracción no
la toca y la vista sabe de dónde salió.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0114_tareas_origin_calendar"
down_revision: str | None = "0113_tareas_category"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE mod_tareas DROP CONSTRAINT IF EXISTS mod_tareas_origin_check")
    op.execute(
        "ALTER TABLE mod_tareas ADD CONSTRAINT mod_tareas_origin_check "
        "CHECK (origin IN ('manual', 'extraction', 'calendar'))"
    )


def downgrade() -> None:
    op.execute("UPDATE mod_tareas SET origin = 'manual' WHERE origin = 'calendar'")
    op.execute("ALTER TABLE mod_tareas DROP CONSTRAINT IF EXISTS mod_tareas_origin_check")
    op.execute(
        "ALTER TABLE mod_tareas ADD CONSTRAINT mod_tareas_origin_check "
        "CHECK (origin IN ('manual', 'extraction'))"
    )
