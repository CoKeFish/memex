"""inbox_schema

Revision ID: 0001
Revises:
Create Date: 2026-05-23

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE users (
            id            BIGSERIAL PRIMARY KEY,
            email         TEXT UNIQUE NOT NULL,
            display_name  TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE sources (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name        TEXT NOT NULL,
            type        TEXT NOT NULL,
            enabled     BOOLEAN NOT NULL DEFAULT TRUE,
            config      JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, name)
        );
        CREATE INDEX sources_user ON sources (user_id);
        """
    )

    op.execute(
        """
        CREATE TABLE source_checkpoints (
            source_id   BIGINT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
            cursor      JSONB NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE inbox (
            id            BIGSERIAL PRIMARY KEY,
            user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_id     BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            external_id   TEXT NOT NULL,
            occurred_at   TIMESTAMPTZ NOT NULL,
            received_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            payload       JSONB NOT NULL,
            processed_at  TIMESTAMPTZ,
            process_error TEXT,
            attempts      INT NOT NULL DEFAULT 0,
            UNIQUE (source_id, external_id)
        );
        CREATE INDEX inbox_user_pending  ON inbox (user_id, source_id, id) WHERE processed_at IS NULL;
        CREATE INDEX inbox_user_occurred ON inbox (user_id, source_id, occurred_at DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE inbox_dedupe_keys (
            user_id   BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key       TEXT NOT NULL,
            inbox_id  BIGINT NOT NULL REFERENCES inbox(id) ON DELETE CASCADE,
            source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, key)
        );
        CREATE INDEX inbox_dedupe_inbox ON inbox_dedupe_keys (inbox_id);
        """
    )

    op.execute(
        """
        CREATE TABLE filter_rules (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_id   BIGINT REFERENCES sources(id) ON DELETE CASCADE,
            scope       JSONB NOT NULL,
            action      TEXT NOT NULL CHECK (action IN ('keep','ignore','archive')),
            priority    INT NOT NULL DEFAULT 100,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX filter_rules_user ON filter_rules (user_id);
        """
    )

    op.execute(
        """
        INSERT INTO users (id, email, display_name) VALUES (1, 'me@local', 'default');
        SELECT setval(pg_get_serial_sequence('users','id'), 1);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS filter_rules CASCADE;")
    op.execute("DROP TABLE IF EXISTS inbox_dedupe_keys CASCADE;")
    op.execute("DROP TABLE IF EXISTS inbox CASCADE;")
    op.execute("DROP TABLE IF EXISTS source_checkpoints CASCADE;")
    op.execute("DROP TABLE IF EXISTS sources CASCADE;")
    op.execute("DROP TABLE IF EXISTS users CASCADE;")
