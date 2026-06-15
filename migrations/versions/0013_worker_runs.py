"""worker_runs: una fila por corrida de cada job del scheduler server-side

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-31

El daemon `memex-scheduler` corre los workers idempotentes (classify/extract/calendar)
en intervalos y deja rastro acá: el Dashboard (backlog) lo lee, y un post-mortem ve qué job falló
y cuándo. `status='running'` inicial → un daemon que muere a media corrida deja una fila huérfana
VISIBLE (no un hueco silencioso). `stats` JSONB = `dataclasses.asdict` del stats del worker.

Distinto de `mod_calendar_sync_runs` (auditoría granular por pull/push del DOMINIO calendar, que el
scheduler NO toca): acá va el roll-up del CICLO de calendar (job='calendar'). Los contadores se
solapan a propósito (visibilidad) — no sumar entre tablas.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE worker_runs (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            job         TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running', 'ok', 'error')),
            stats       JSONB NOT NULL DEFAULT '{}'::jsonb,
            error       TEXT,
            started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ
        );
        CREATE INDEX worker_runs_user_job_started
            ON worker_runs (user_id, job, started_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS worker_runs CASCADE;")
