"""apify_runs: trazabilidad de costo por run de actor (Apify) + agregado por corrida

Revision ID: 0055_apify_runs
Revises: 0054_cluster_blob_signature
Create Date: 2026-06-10

El costo real que reporta Apify por cada run de actor (`usageTotalUsd`) hoy solo se
loggeaba (structlog efímero) y se perdía. Un row de `apify_runs` = UNA corrida de actor
= (fuente, cuenta seguida, corrida) — también en error/timeout, porque un run fallido o
abortado pudo haber cobrado igual.

- `ingestion_run_id` NULLABLE: dry-run y `memex-social discover` gastan Apify real pero
  no abren `ingestion_runs`.
- `source_id` ON DELETE SET NULL: el gasto histórico sobrevive al borrado de la fuente.
- `charged_events` = desglose pay-per-event de Apify (`chargedEventCounts`: evento → n).
- `ingestion_runs.api_cost_usd` = agregado de la corrida (lo escribe el mismo writer),
  para listar costo por corrida sin JOIN.

Numeración (migration-numbering-worktrees): 0055 verificado libre en main y todos los
worktrees; head = 0054.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0055_apify_runs"
down_revision: str | None = "0054_cluster_blob_signature"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE apify_runs (
            id               BIGSERIAL PRIMARY KEY,
            user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_id        BIGINT REFERENCES sources(id) ON DELETE SET NULL,
            ingestion_run_id UUID REFERENCES ingestion_runs(id) ON DELETE SET NULL,
            platform         TEXT NOT NULL,
            account          TEXT NOT NULL,
            actor_id         TEXT NOT NULL,
            apify_run_id     TEXT,
            status           TEXT NOT NULL CHECK (status IN ('ok', 'error', 'timeout')),
            items_scraped    INT NOT NULL DEFAULT 0,
            items_kept       INT NOT NULL DEFAULT 0,
            cost_usd         NUMERIC(10, 6),
            charged_events   JSONB,
            started_at       TIMESTAMPTZ,
            finished_at      TIMESTAMPTZ,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX apify_runs_user_created ON apify_runs (user_id, created_at DESC);
        CREATE INDEX apify_runs_source ON apify_runs (source_id);
        CREATE INDEX apify_runs_ingestion_run ON apify_runs (ingestion_run_id);

        ALTER TABLE ingestion_runs ADD COLUMN api_cost_usd NUMERIC(10, 6);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE ingestion_runs DROP COLUMN IF EXISTS api_cost_usd;
        DROP TABLE IF EXISTS apify_runs;
        """
    )
