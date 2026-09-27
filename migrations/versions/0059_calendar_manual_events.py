"""calendar: origin 'manual' (CRUD del agente) + procedencia del tombstone del consolidado

Revision ID: 0059_calendar_manual_events
Revises: 0058_ingest_swept_ranges
Create Date: 2026-06-11

Dos piezas para el CRUD de eventos por CLI y el saneamiento de consolidados:

- `mod_calendar_events.origin` admite `'manual'`: eventos que el agente/usuario crea directo por
  `memex calendario add` (rank 100, como los manuales del proveedor — decisión 7). El CHECK de
  0011 se reemplaza in-place (nombre auto-asignado por Postgres, verificado en la DB).
- `mod_calendar_consolidated.deleted_source`: QUIÉN tombstoneó el consolidado. `'merge'` (fusión
  de grupos — el único camino que existía), `'orphaned'` (sin miembros vivos: links borrados o
  todos cancelados en el proveedor — el saneamiento automático nuevo de la consolidación) y
  `'user'` (borrado explícito vía CLI — la consolidación NUNCA lo resucita y el push lo propaga
  como delete). El CHECK de coherencia `(deleted_source IS NULL) = (NOT deleted)` es estricto a
  propósito: cualquier writer de `deleted` que olvide declarar la fuente revienta en tests.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0059_calendar_manual_events"
down_revision: str | None = "0058_ingest_swept_ranges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_calendar_events
            DROP CONSTRAINT mod_calendar_events_origin_check;
        ALTER TABLE mod_calendar_events
            ADD CONSTRAINT mod_calendar_events_origin_check
            CHECK (origin IN ('extraction','provider','module','manual'));
        """
    )
    op.execute(
        """
        ALTER TABLE mod_calendar_consolidated
            ADD COLUMN deleted_source TEXT
                CHECK (deleted_source IN ('merge','orphaned','user'));
        UPDATE mod_calendar_consolidated SET deleted_source = 'merge' WHERE deleted;
        ALTER TABLE mod_calendar_consolidated
            ADD CONSTRAINT mod_calendar_consolidated_deleted_coherent_ck
            CHECK ((deleted_source IS NULL) = (NOT deleted));
        """
    )


def downgrade() -> None:
    # OJO: falla si ya existen filas con origin='manual' (mismo best-effort que otros downgrades).
    op.execute(
        """
        ALTER TABLE mod_calendar_consolidated
            DROP CONSTRAINT IF EXISTS mod_calendar_consolidated_deleted_coherent_ck;
        ALTER TABLE mod_calendar_consolidated
            DROP COLUMN IF EXISTS deleted_source;
        """
    )
    op.execute(
        """
        ALTER TABLE mod_calendar_events
            DROP CONSTRAINT mod_calendar_events_origin_check;
        ALTER TABLE mod_calendar_events
            ADD CONSTRAINT mod_calendar_events_origin_check
            CHECK (origin IN ('extraction','provider','module'));
        """
    )
