"""summary_inbox_links: UNIQUE(inbox_id) — un mensaje pertenece a lo sumo a un summary

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-30

El summarizer trackea progreso por "no estar en summary_inbox_links". Sin una constraint,
dos corridas concurrentes leen las mismas filas pendientes, ambas llaman al LLM y ambas
insertan summaries distintas → el mismo `inbox_id` queda ligado a 2 summaries (doble costo,
dato duplicado) porque la PK (summary_id, inbox_id) lo permite.

Esta migración agrega `UNIQUE(inbox_id)`: cada mensaje pertenece a LO SUMO a un summary.
Hace la duplicación imposible a nivel DB — el segundo INSERT de link viola la UNIQUE y su
transacción (que incluye el summary, en `_persist_summary`) hace rollback completo, sin
orphan. Reemplaza el índice no-único de la 0006 (la UNIQUE crea su propio índice, que sirve
igual para el reverse-lookup inbox→summaries).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS summary_inbox_links_inbox;")
    op.execute(
        "ALTER TABLE summary_inbox_links "
        "ADD CONSTRAINT summary_inbox_links_inbox_unique UNIQUE (inbox_id);"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE summary_inbox_links DROP CONSTRAINT IF EXISTS summary_inbox_links_inbox_unique;"
    )
    op.execute("CREATE INDEX summary_inbox_links_inbox ON summary_inbox_links (inbox_id);")
