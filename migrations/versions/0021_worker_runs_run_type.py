"""worker_runs: run_type + run_config (corridas de reprocess on-demand desde el API)

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-02

`worker_runs` nació como log de las corridas del daemon (job='classify'|'summarize'|…). Ahora el API
encola corridas MANUALES por lote (POST /processing/run) reusando la misma tabla como cola+log:

- `run_type` distingue el origen: 'job' (un worker del scheduler) vs 'reprocess' (corrida manual por
  lote disparada desde la UI). El panel Scheduler de /procesamiento filtra `run_type='job'`; las
  corridas reprocess se listan aparte por GET /processing/runs.
- `run_config` guarda los parámetros de la corrida reprocess (`stages`, `targets`, `force`, `filters`)
  para auditoría y para que el feed sea autocontenido.

NOTA DE NUMERACIÓN (cabeza única de Alembic): `0020` (`0020_log_events`, rama logs) ya entró a main;
esta migración encadena sobre él (`down_revision = "0020"`) para mantener el historial lineal con una
sola cabeza (`0019 → 0020 → 0021 → 0022 → 0023`).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE worker_runs
            ADD COLUMN run_type TEXT NOT NULL DEFAULT 'job'
                CHECK (run_type IN ('job', 'reprocess')),
            ADD COLUMN run_config JSONB NOT NULL DEFAULT '{}'::jsonb;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE worker_runs DROP COLUMN IF EXISTS run_config;")
    op.execute("ALTER TABLE worker_runs DROP COLUMN IF EXISTS run_type;")
