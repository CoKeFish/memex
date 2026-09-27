"""Vectores semánticos: un embedding por registro (y modelo) para el bloqueo semántico compartido.

Revision ID: 0112_semantic_vectors
Revises: 0111_finance_counterparty_rules

La detección de «misma cosa» era léxica y dejaba pasar sinónimos («reclamar» / «recoger» el
pasaporte) sin que el juez LLM los viera. `semantic_vectors` guarda el vector del texto de cada
registro (`slug` = módulo dueño, `ref_id` = su id) con el modelo que lo calculó y el hash del
texto (para no re-embeber lo que no cambió). Los vecinos se calculan en Python sobre alcances
chicos (las tareas vivas de un asunto): sin pgvector a propósito. Ver `memex.semantic`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0112_semantic_vectors"
down_revision: str | None = "0111_finance_counterparty_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE semantic_vectors (
            id         BIGSERIAL PRIMARY KEY,
            user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            slug       TEXT NOT NULL,
            ref_id     BIGINT NOT NULL,
            model      TEXT NOT NULL,
            text_hash  TEXT NOT NULL,
            dim        INTEGER NOT NULL,
            vec        REAL[] NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, slug, ref_id, model)
        )
        """
    )
    op.execute(
        "CREATE INDEX semantic_vectors_scope_idx ON semantic_vectors (user_id, slug, model, ref_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE semantic_vectors")
