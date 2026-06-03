"""ingest_scheduler: agenda de ingesta por fuente (cada cuánto se trae) + master toggle del daemon

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-02

Hermana de `scheduler_settings` (0022) pero para INGESTA, no procesamiento. El daemon nuevo
`memex-ingest-scheduler` lee estas dos cosas en runtime y dispara `run_fetch_window(mode=incremental,
trigger='daemon')` por fuente:

- `sources.fetch_schedule` (TEXT, ISO-8601 duration: 'PT15M', 'PT1H', 'P1D'…): cada cuánto se agenda
  esa fuente. NULL = no se agenda (default). Sin CHECK: se valida con `parse_duration` en la capa API
  (igual que `scheduler/config.py`, que saltea intervalos malos en vez de fallar duro).
- `ingest_scheduler_settings`: una fila por usuario, `daemon_enabled` master. Filosofía "apagado por
  default": sin fila o `daemon_enabled=FALSE` → el daemon idlea aunque haya fuentes con schedule.

El daemon solo agenda fuentes `enabled=TRUE AND fetch_schedule IS NOT NULL` (compone con el toggle
`sources.enabled`: una fuente apagada no se trae ni manual ni agendada).

Limpieza de origen legado: el fetch manual marcaba `ingestion_runs.trigger='dashboard'`; se renombra a
'manual' (taxonomía manual/daemon/backfill/agent/cli). `trigger` es TEXT sin CHECK (ver 0002), así que
sumar valores nuevos no toca constraints.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE sources ADD COLUMN fetch_schedule TEXT;")
    op.execute(
        """
        CREATE TABLE ingest_scheduler_settings (
            user_id        BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            daemon_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    # Renombra el origen del fetch manual ('dashboard' → 'manual') en el historial existente.
    op.execute("UPDATE ingestion_runs SET trigger = 'manual' WHERE trigger = 'dashboard';")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ingest_scheduler_settings CASCADE;")
    op.execute("ALTER TABLE sources DROP COLUMN IF EXISTS fetch_schedule;")
