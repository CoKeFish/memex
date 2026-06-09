"""calidad: relevance_candidates — cola de candidatos a filtrar (detección automática "por métricas")

Revision ID: 0051_relevance_candidates
Revises: 0050_sender_tier_overrides
Create Date: 2026-06-08

El job `relevance` (apagado por default) llena esta cola con los remitentes EMAIL ruidosos
(volumen alto + poca relevancia) que aún no fueron accionados, para que el usuario los confirme o
descarte sin tener que ir a buscarlos. Es el "analyzer costo-vs-valor" diferido, en versión asistida:
NUNCA auto-aplica una acción (la confirma el humano, Fase 3). `snapshot` guarda una foto del metric
+ `sample_inbox_ids` para validar antes de confirmar. `status` open→confirmed/dismissed; el job
refresca las métricas de las filas existentes pero NO toca su `status` (un descartado no re-abre).
UNIQUE(user_id, sender_key).

`downgrade` dropea la tabla.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0051_relevance_candidates"
down_revision: str | None = "0050_sender_tier_overrides"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE relevance_candidates (
            id            BIGSERIAL PRIMARY KEY,
            user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            sender_key    TEXT NOT NULL,
            sender_label  TEXT NOT NULL,
            email         TEXT,
            messages      INT NOT NULL,
            relevant      INT NOT NULL,
            inert         INT NOT NULL,
            relevance_pct NUMERIC,
            score         INT NOT NULL DEFAULT 0,
            status        TEXT NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open', 'confirmed', 'dismissed')),
            snapshot      JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, sender_key)
        );
        CREATE INDEX relevance_candidates_user_status
            ON relevance_candidates (user_id, status, score DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS relevance_candidates CASCADE;")
