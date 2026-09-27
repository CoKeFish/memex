"""trace_nodes: árbol de traza jerárquica por mensaje (logs estilo stack-trace)

Revision ID: 0037_trace_nodes
Revises: 0036_finance_transactions
Create Date: 2026-06-05

Sustituye la "traza" reconstruida de `llm_calls` (plana) + el debug por-módulo hardcodeado por un
ÁRBOL persistido y genérico: un módulo emite nodos llamando `ctx.trace.*` durante `persist`/`dedup`
(ver `memex.core.trace`) y el front los renderiza sin saber de cada módulo (vista en stack).

`trace_nodes` es una lista PLANA con `parent_id` (self-FK): cada fila es un paso del procesamiento de
un mensaje. `kind` clasifica el nodo para el render genérico (`root`/`module` = spans del orquestador;
`entity` = referencia a una fila de dominio vía `ref_table`+`ref_id`, sin re-renderizar el dato;
`step`/`log`/`decision` = pasos internos; `llm` = una llamada al modelo). El COSTO no se guarda acá:
los nodos `llm` referencian `llm_calls(id)` por `llm_call_id` y el roll-up jerárquico se calcula al
leer (`read_trace`). `detail` (JSONB) lleva señales del paso (p. ej. `{trgm, umbral}`).

Slice 1: solo esta tabla. Las `llm_calls` de ruteo/extracción ya llevan `inbox_id` (las cuelga del
root `read_trace` al leer), así que NO hace falta tocar `record_llm_call` ni agregar
`llm_calls.trace_node_id` todavía — eso entra en el slice del worker async (desempate LLM con
`inbox_id=NULL`, que se ata por nodo). `forget`/re-extracción NO borran nodos por-módulo: el
orquestador hace delete-then-write del subárbol del inbox al re-extraer (`create_root`); además
`ON DELETE CASCADE` por `inbox_id` limpia si se borra el mensaje. `downgrade` dropea la tabla.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0037_trace_nodes"
down_revision: str | None = "0036_finance_transactions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE trace_nodes (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            -- ancla por-mensaje (espeja llm_calls.inbox_id); CASCADE limpia si se borra el mensaje.
            inbox_id    BIGINT REFERENCES inbox(id) ON DELETE CASCADE,
            -- jerarquía: self-FK; CASCADE borra el subárbol al borrar un padre (delete-then-write).
            parent_id   BIGINT REFERENCES trace_nodes(id) ON DELETE CASCADE,
            seq         INTEGER NOT NULL DEFAULT 0,            -- orden entre hermanos
            kind        TEXT NOT NULL
                          CHECK (kind IN ('root','module','entity','step','log','decision','llm')),
            module_slug TEXT,                                  -- quién emitió (NULL = orquestador)
            label       TEXT NOT NULL DEFAULT '',
            status      TEXT CHECK (status IS NULL OR status IN ('ok','warn','error','info')),
            -- nodo `entity`: ancla polimórfica a una fila de dominio (sin FK: apunta a cualquier mod_*).
            ref_table   TEXT,
            ref_id      BIGINT,
            -- nodo `llm`: la llamada referenciada (su costo/output crudo viven en llm_calls).
            llm_call_id BIGINT REFERENCES llm_calls(id) ON DELETE SET NULL,
            detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX trace_nodes_inbox  ON trace_nodes (user_id, inbox_id, parent_id, seq);
        CREATE INDEX trace_nodes_parent ON trace_nodes (parent_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trace_nodes CASCADE;")
