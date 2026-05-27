"""observability

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-27

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ingestion_runs (
            id             UUID PRIMARY KEY,
            user_id        BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_id      BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            trigger        TEXT NOT NULL,
            status         TEXT NOT NULL CHECK (status IN ('running','ok','failed','aborted')),
            started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ended_at       TIMESTAMPTZ,
            duration_ms    INT,
            posted         INT NOT NULL DEFAULT 0,
            inserted       INT NOT NULL DEFAULT 0,
            duplicates     INT NOT NULL DEFAULT 0,
            errors         INT NOT NULL DEFAULT 0,
            error_class    TEXT,
            error_message  TEXT,
            metadata       JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE INDEX ingestion_runs_user_source_started
            ON ingestion_runs (user_id, source_id, started_at DESC);
        CREATE INDEX ingestion_runs_status_started
            ON ingestion_runs (status, started_at DESC)
            WHERE status IN ('failed','aborted');
        """
    )

    op.execute(
        """
        CREATE TABLE llm_calls (
            id                 BIGSERIAL PRIMARY KEY,
            user_id            BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            request_id         TEXT,
            inbox_id           BIGINT REFERENCES inbox(id) ON DELETE SET NULL,
            purpose            TEXT NOT NULL,
            model              TEXT NOT NULL,
            prompt_tokens      INT NOT NULL,
            completion_tokens  INT NOT NULL,
            cost_usd           NUMERIC(10, 6) NOT NULL,
            latency_ms         INT NOT NULL,
            status             TEXT NOT NULL CHECK (status IN ('ok','error','filtered')),
            error_message      TEXT,
            metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX llm_calls_user_created   ON llm_calls (user_id, created_at DESC);
        CREATE INDEX llm_calls_purpose_model  ON llm_calls (purpose, model, created_at DESC);
        CREATE INDEX llm_calls_inbox          ON llm_calls (inbox_id) WHERE inbox_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS llm_calls CASCADE;")
    op.execute("DROP TABLE IF EXISTS ingestion_runs CASCADE;")
