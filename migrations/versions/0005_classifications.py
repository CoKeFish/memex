"""classifications: tier (nivel) post-ingest por mensaje

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-29

Tabla base para el classifier post-ingest (ADR-002). Cada mensaje de `inbox`
recibe un tier de costo creciente:

- `blacklist`  — cero LLM, solo se registra el arribo (newsletters / notificaciones).
- `batch`      — N mensajes comparten una llamada al LLM (resumen agregado).
- `individual` — 1 mensaje → 1 llamada con extracción estructurada.

El classifier que la escribe todavía no existe (será un sistema dinámico); la tabla
queda lista como base, igual que `llm_calls` en la 0002. Una fila por mensaje
(`UNIQUE(inbox_id)`): re-clasificar es un UPDATE, no historial. `metadata` es el
bolsillo flexible mientras la forma del classifier se asienta.

Fuera de alcance (a propósito): `extracted_facts` —su forma depende de experimentar
primero con el LLM en modo batch (¿datos por-mensaje vs resumen agregado?)— y cualquier
cambio a `filter_rules` (el filtro pre-ingest de ADR-011 queda intacto).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE classifications (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            inbox_id    BIGINT NOT NULL REFERENCES inbox(id) ON DELETE CASCADE,
            tier        TEXT NOT NULL CHECK (tier IN ('blacklist','batch','individual')),
            metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (inbox_id)
        );
        CREATE INDEX classifications_user_tier ON classifications (user_id, tier);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS classifications CASCADE;")
