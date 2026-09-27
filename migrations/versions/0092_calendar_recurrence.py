"""calendar: recurrencia al estilo Google (maestro con RRULE + excepciones por ocurrencia)

Revision ID: 0092_calendar_recurrence
Revises: 0091_tareas
Create Date: 2026-09-09

Antes una serie eran N filas-instancia que compartían `recurring_event_id` (y `--until` era
obligatorio: no había recurrencia sin fin). Ahora un evento recurrente es UNA fila —el maestro—
con `recurrence` = líneas RFC 5545 (`RRULE:…`, `EXDATE:…`, lo mismo que Google expone en
`event.recurrence`); las ocurrencias se EXPANDEN al leer dentro de la ventana consultada (nunca se
materializan), así «todos los jueves» sin fin es una regla y no mil filas. Una ocurrencia
modificada/cancelada a mano es una EXCEPCIÓN: fila propia con `recurring_event_id` = la serie del
maestro y `original_start_on`/`original_start_time` = la ocurrencia que reemplaza (el
`originalStartTime` de Google).

Las mismas columnas se espejan en la capa consolidada (el consolidado de un maestro conserva la
regla; el de una excepción, su `original_start_*`). `mod_calendar_conflicts.occurs_on` guarda el
día concreto del choque (un recurrente choca en una ocurrencia, no «en general»).

Las series viejas por instancias siguen siendo eventos sueltos válidos (recurrence vacío); la
data de dev es desechable, no se migran.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0092_calendar_recurrence"
down_revision: str | None = "0091_tareas"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_calendar_events
            ADD COLUMN recurrence          TEXT[] NOT NULL DEFAULT '{}'::text[],
            ADD COLUMN original_start_on   DATE,
            ADD COLUMN original_start_time TIME,
            ADD CONSTRAINT mod_calendar_events_exception_needs_series
                CHECK (original_start_on IS NULL OR recurring_event_id IS NOT NULL);
        CREATE INDEX mod_calendar_events_recurring_masters
            ON mod_calendar_events (user_id) WHERE cardinality(recurrence) > 0;

        ALTER TABLE mod_calendar_consolidated
            ADD COLUMN recurrence          TEXT[] NOT NULL DEFAULT '{}'::text[],
            ADD COLUMN series_id           TEXT,
            ADD COLUMN original_start_on   DATE,
            ADD COLUMN original_start_time TIME;
        CREATE INDEX mod_calendar_consolidated_recurring_masters
            ON mod_calendar_consolidated (user_id) WHERE cardinality(recurrence) > 0;
        CREATE INDEX mod_calendar_consolidated_series
            ON mod_calendar_consolidated (series_id) WHERE series_id IS NOT NULL;

        ALTER TABLE mod_calendar_conflicts ADD COLUMN occurs_on DATE;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_calendar_conflicts DROP COLUMN IF EXISTS occurs_on;
        DROP INDEX IF EXISTS mod_calendar_consolidated_series;
        DROP INDEX IF EXISTS mod_calendar_consolidated_recurring_masters;
        ALTER TABLE mod_calendar_consolidated
            DROP COLUMN IF EXISTS original_start_time,
            DROP COLUMN IF EXISTS original_start_on,
            DROP COLUMN IF EXISTS series_id,
            DROP COLUMN IF EXISTS recurrence;
        DROP INDEX IF EXISTS mod_calendar_events_recurring_masters;
        ALTER TABLE mod_calendar_events
            DROP CONSTRAINT IF EXISTS mod_calendar_events_exception_needs_series,
            DROP COLUMN IF EXISTS original_start_time,
            DROP COLUMN IF EXISTS original_start_on,
            DROP COLUMN IF EXISTS recurrence;
        """
    )
