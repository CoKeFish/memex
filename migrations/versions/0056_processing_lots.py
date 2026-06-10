"""processing_lots + processing_window_defaults: lote de procesamiento por ventanas de cantidad

Revision ID: 0056_processing_lots
Revises: 0055_apify_runs
Create Date: 2026-06-10

Espejo de `backfill_jobs` (0024) pero para PROCESAR lo ya ingerido: el usuario congela un lote
(snapshot de inbox_ids ordenado por `occurred_at, id`) y lo avanza en ventanas de N mensajes,
mirando el costo de cada una. Una fila por usuario (`UNIQUE(user_id)`, coherente con "una corrida
a la vez"): reconfigurar = upsert que resetea frontier+history; reset = DELETE. `frontier` es el
ÍNDICE de mensajes ya procesados dentro de `target_ids`; cada ventana ejecutada se appendea a
`history` (JSONB, con su `cost_usd`).

`processing_window_defaults` guarda el tamaño de ventana preferido POR MEDIO (email/chat/social):
sin CHECK sobre `kind` a propósito — los kinds válidos los conoce el código (`memex.sources`) y un
kind nuevo no debe requerir migración.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0056_processing_lots"
down_revision: str | None = "0055_apify_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE processing_lots (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            stages      TEXT[] NOT NULL,
            config      JSONB NOT NULL DEFAULT '{}'::jsonb,
            target_ids  BIGINT[] NOT NULL,
            frontier    INTEGER NOT NULL DEFAULT 0 CHECK (frontier >= 0),
            window_size INTEGER NOT NULL CHECK (window_size >= 1),
            status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','done')),
            history     JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT processing_lots_frontier_ck
                CHECK (frontier <= cardinality(target_ids))
        );
        """
    )
    op.execute("CREATE UNIQUE INDEX processing_lots_user_uq ON processing_lots (user_id);")
    op.execute(
        """
        CREATE TABLE processing_window_defaults (
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind        TEXT NOT NULL,
            window_size INTEGER NOT NULL CHECK (window_size >= 1),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, kind)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS processing_window_defaults CASCADE;")
    op.execute("DROP TABLE IF EXISTS processing_lots CASCADE;")
