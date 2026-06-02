"""worker_runs: run_type + run_config (corridas de reprocess on-demand desde el API)

Revision ID: 0021
Revises: 0019
Create Date: 2026-06-02

`worker_runs` nació como log de las corridas del daemon (job='classify'|'summarize'|…). Ahora el API
encola corridas MANUALES por lote (POST /processing/run) reusando la misma tabla como cola+log:

- `run_type` distingue el origen: 'job' (un worker del scheduler) vs 'reprocess' (corrida manual por
  lote disparada desde la UI). El panel Scheduler de /procesamiento filtra `run_type='job'`; las
  corridas reprocess se listan aparte por GET /processing/runs.
- `run_config` guarda los parámetros de la corrida reprocess (`stages`, `targets`, `force`, `filters`)
  para auditoría y para que el feed sea autocontenido.

NOTA DE NUMERACIÓN (cabeza única de Alembic): `0020` lo usa la rama `worktree-logs-advanced`
(`0020_log_events`, aún sin mergear). Esta migración salta a `0021` y, en dev, encadena a `0019`. Antes
de mergear a main, repuntar `down_revision` al head real (`alembic heads`) para no dejar dos hijos de
`0019` (= multi-head).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0019"
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
