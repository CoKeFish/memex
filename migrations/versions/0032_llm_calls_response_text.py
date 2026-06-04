"""llm_calls.response_text

Agrega response_text (TEXT, nullable) a llm_calls: guarda el TEXTO CRUDO que devolvió el LLM en
cada llamada. Antes el log solo tenía tokens/costo/metadata y el contenido se parseaba y se
descartaba (orchestrator `_extract_module`/`_extract_group`); ahora el log queda completo
(fecha + texto extraído + costo). NULL = no capturado; '' = completion vacía. Mismo patrón que
`summaries.content` / `media_assets.ocr_text` (texto del modelo como columna de primera clase).
Sin índice: columna de auditoría/detalle, no se filtra ni agrega por ella.

Revision ID: 0032
Revises: 0031

Numeración (migration-numbering-worktrees): 0032 verificado libre en los 3 worktrees y todas las
ramas (0027 en `infra-relaciones-dominios` es la v1 abandonada de `relation_edges`, no mergea);
`down_revision='0031'`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE llm_calls ADD COLUMN response_text TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE llm_calls DROP COLUMN IF EXISTS response_text;")
