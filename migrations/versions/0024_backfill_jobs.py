"""backfill_jobs: estado de la importación masiva segmentada (frontera + history) por fuente

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-02

Backfill con frontera: el usuario importa `[range_start, range_end)` de una fuente de correo en
ventanas (día/semana/mes x N) y avanza la `frontier` apretando un botón. Una fila por fuente
(`UNIQUE(source_id)`): reconfigurar = upsert que resetea frontier+history; reset = DELETE. Cada
ventana ejecutada se appendea a `history` (JSONB). `range_end` se guarda EXCLUSIVO (como el `until`
del fetch e IMAP `BEFORE`); la UI manda/recibe la fecha inclusiva y el router convierte en el borde.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE backfill_jobs (
            id               BIGSERIAL PRIMARY KEY,
            user_id          BIGINT NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
            source_id        BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            range_start      DATE NOT NULL,
            range_end        DATE NOT NULL,
            frontier         DATE NOT NULL,
            window_unit      TEXT NOT NULL DEFAULT 'month'
                CHECK (window_unit IN ('day','week','month')),
            window_count     INTEGER NOT NULL DEFAULT 1 CHECK (window_count >= 1),
            per_window_limit INTEGER NOT NULL DEFAULT 2000 CHECK (per_window_limit >= 1),
            status           TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','done')),
            history          JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT backfill_jobs_range_ck    CHECK (range_end > range_start),
            CONSTRAINT backfill_jobs_frontier_ck CHECK (frontier >= range_start AND frontier <= range_end)
        );
        """
    )
    op.execute("CREATE UNIQUE INDEX backfill_jobs_source_uq ON backfill_jobs (source_id);")
    op.execute("CREATE INDEX backfill_jobs_user ON backfill_jobs (user_id);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS backfill_jobs CASCADE;")
