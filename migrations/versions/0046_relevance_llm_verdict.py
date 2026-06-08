"""calidad: llm_verdict en relevance_candidates (juez LLM de zona gris, opcional)

Revision ID: 0046_relevance_llm_verdict
Revises: 0045_relevance_candidates
Create Date: 2026-06-08

El juez LLM (opcional, `MEMEX_QUALITY_LLM`, default off) lee una muestra de los mensajes de un
candidato y emite un veredicto ADVISORY de relevancia que se guarda acá. No acciona nada (la decisión
sigue siendo del humano); solo informa la cola. La detección determinista NO toca esta columna (se
preserva entre corridas). NULL = sin juzgar.

`downgrade` dropea la columna.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0046_relevance_llm_verdict"
down_revision: str | None = "0045_relevance_candidates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE relevance_candidates ADD COLUMN llm_verdict JSONB;")


def downgrade() -> None:
    op.execute("ALTER TABLE relevance_candidates DROP COLUMN IF EXISTS llm_verdict;")
