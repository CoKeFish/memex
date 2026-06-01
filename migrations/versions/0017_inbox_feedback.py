"""inbox_feedback — feedback manual rápido por mensaje (captura para calibrar)

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-01

Captura, por mensaje, que algo de la CALIDAD del procesamiento salió mal (categorías rápidas en
`kinds` + nota libre). Es señal de alto peso para que el usuario LUEGO se haga una idea (ajustar
parámetros, promover a individual, etc.); NO resuelve nada ni dispara auto-mejora (diferido).
`metadata` guarda un snapshot de lo observado al reportar (tier/módulos/conteos/remitente) para que
el feedback sea autocontenido al evaluar. UNA fila por `inbox_id` (se actualiza al re-reportar).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE inbox_feedback (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            inbox_id    BIGINT NOT NULL REFERENCES inbox(id) ON DELETE CASCADE,
            kinds       TEXT[] NOT NULL,
            note        TEXT,
            metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
            status      TEXT NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open','reviewed','dismissed')),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (inbox_id)
        );
        CREATE INDEX inbox_feedback_user_open
            ON inbox_feedback (user_id, created_at DESC) WHERE status = 'open';
        CREATE INDEX inbox_feedback_inbox ON inbox_feedback (inbox_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS inbox_feedback CASCADE;")
