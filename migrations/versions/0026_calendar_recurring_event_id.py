"""calendar: recurring_event_id en mod_calendar_events (serie recurrente del proveedor)

Revision ID: 0026
Revises: 0024
Create Date: 2026-06-03

Captura el `recurringEventId` que la API de Google entrega por instancia (sync con
`singleEvents=true`): identifica a qué SERIE recurrente pertenece cada evento. Se usa para agrupar
las instancias de una misma serie — p.ej. un choque entre dos series recurrentes se muestra como UN
conflicto, no uno por instancia. Aditivo (columna nullable + índice parcial); no rompe nada.

Numeración (migration-numbering-worktrees): chainea sobre 0024 (cabeza de `main` al crear el
worktree). 0025 lo tomó otro worktree (`ingest_scheduler`) sin mergear aún → al mergear a `main` se
re-apunta `down_revision` a la cabeza vigente para mantener la cadena lineal.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_calendar_events ADD COLUMN recurring_event_id TEXT;
        -- Listar/agrupar las instancias de una serie barato; parcial (la mayoría no es recurrente).
        CREATE INDEX mod_calendar_events_recurring
            ON mod_calendar_events (user_id, recurring_event_id)
            WHERE recurring_event_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS mod_calendar_events_recurring;
        ALTER TABLE mod_calendar_events DROP COLUMN IF EXISTS recurring_event_id;
        """
    )
