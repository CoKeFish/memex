"""tareas: mod_tareas (pendientes accionables, con o sin fecha límite)

Revision ID: 0091_tareas
Revises: 0090_finance_status_kind
Create Date: 2026-09-08

Módulo `tareas` = registrador DETERMINISTA (sin LLM, sin extracción de mensajes): el agente entiende
el lenguaje natural («tarea del Britano: ejercicios de la página 42 para el jueves») y llama a la
CLI `memex-tareas` con campos estructurados. Una tarea es una ACCIÓN pendiente; se distingue del
calendario (cosas que OCURREN en un momento) y de asuntos (hilos de vida inferidos de los mensajes):
`due_on` es opcional («cambiar el punto de atención de la EPS» no tiene fecha) y `asunto_id`
opcional la cuelga del asunto al que pertenece (el curso del Britano).

Dedup (contrato de módulos: clave natural + resolución difusa) en el WRITE-PATH: misma
`memex_norm(title)` entre tareas pendientes del mismo asunto (o sueltas) = la misma tarea; los
títulos casi iguales se resuelven por similitud en Python (`module.find_duplicate`). El índice por
`memex_norm(title)` sostiene la clave natural (la función es IMMUTABLE desde 0033).

`event_id` correlaciona los hechos del MISMO mensaje del agente (cross-module, igual que bienestar y
finanzas). `downgrade` dropea la tabla (forward-only; la data de dev es desechable).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0091_tareas"
down_revision: str | None = "0090_finance_status_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mod_tareas (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title       TEXT NOT NULL CHECK (btrim(title) <> ''),
            description TEXT NOT NULL DEFAULT '',
            due_on      DATE,
            priority    SMALLINT NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
            status      TEXT NOT NULL DEFAULT 'pendiente'
                          CHECK (status IN ('pendiente','hecha','cancelada')),
            asunto_id   BIGINT REFERENCES mod_asuntos(id) ON DELETE SET NULL,
            done_at     TIMESTAMPTZ,
            metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
            event_id    TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX mod_tareas_user_status_due ON mod_tareas (user_id, status, due_on);
        CREATE INDEX mod_tareas_user_title_norm ON mod_tareas (user_id, memex_norm(title));
        CREATE INDEX mod_tareas_asunto ON mod_tareas (asunto_id) WHERE asunto_id IS NOT NULL;
        CREATE INDEX mod_tareas_event ON mod_tareas (event_id) WHERE event_id IS NOT NULL;
        """
    )
    # El evento multi-hecho del agente (`memex start … end`, 0041) encola hechos por `kind`: las
    # tareas también se encolan y persisten juntas al cerrar.
    op.execute(
        """
        ALTER TABLE mod_agent_event_facts DROP CONSTRAINT mod_agent_event_facts_kind_check;
        ALTER TABLE mod_agent_event_facts ADD CONSTRAINT mod_agent_event_facts_kind_check
            CHECK (kind IN ('identidad','finance','bienestar','tareas'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM mod_agent_event_facts WHERE kind = 'tareas';
        ALTER TABLE mod_agent_event_facts DROP CONSTRAINT mod_agent_event_facts_kind_check;
        ALTER TABLE mod_agent_event_facts ADD CONSTRAINT mod_agent_event_facts_kind_check
            CHECK (kind IN ('identidad','finance','bienestar'));
        DROP TABLE IF EXISTS mod_tareas CASCADE;
        """
    )
