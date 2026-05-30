"""summaries + summary_inbox_links: salida del summarizer multi-tier

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-29

Tablas base para el summarizer post-classifier (ADR-002 / ADR-003). Un `summary`
es la salida de UNA llamada al LLM y tiene un `tier`:

- `individual` — resume 1 mensaje (tier individual de ADR-002).
- `batch`      — resume N mensajes en una sola llamada (tier batch / ventanas de chat).

(El tier `blacklist` NO produce summary: solo se registra el arribo en
`classifications`, sin LLM.)

`summary_inbox_links` es la tabla puente N:M que liga un summary a los mensajes de
`inbox` que cubre. El caso `batch` (1 summary → N mensajes) es el que justifica el
N:M; el caso `individual` es un único link. Un experimento (2026-05-29) confirmó que
el LLM atribuye de forma fiable la salida a sus mensajes de origen, por lo que el
puente modela bien la relación 1-llamada↔N-mensajes.

El summarizer que las escribe todavía no existe (forward-declared, igual que
`classifications` en la 0005 y `llm_calls` en la 0002). `llm_calls` queda intacta:
no se le toca el `inbox_id` 1:1 (sirve al tier individual); el N:M vive acá.

Fuera de alcance (a propósito, por decisión del dueño):
- `extracted_facts` (gastos/calendario/viaje/orden) — tendrá su propia arquitectura,
  a discutir aparte; no se diseña todavía para no inventar la forma.
- `rule_proposals` (feedback loop de filtrado, ADR-001) — pertenece al filtrado, no
  a los resúmenes; se difiere.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE summaries (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tier        TEXT NOT NULL CHECK (tier IN ('batch','individual')),
            content     TEXT NOT NULL,
            metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX summaries_user_created ON summaries (user_id, created_at DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE summary_inbox_links (
            summary_id  BIGINT NOT NULL REFERENCES summaries(id) ON DELETE CASCADE,
            inbox_id    BIGINT NOT NULL REFERENCES inbox(id) ON DELETE CASCADE,
            PRIMARY KEY (summary_id, inbox_id)
        );
        CREATE INDEX summary_inbox_links_inbox ON summary_inbox_links (inbox_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS summary_inbox_links CASCADE;")
    op.execute("DROP TABLE IF EXISTS summaries CASCADE;")
