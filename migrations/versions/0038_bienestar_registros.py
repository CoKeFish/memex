"""bienestar: mod_bienestar_registros (registros deterministas de salud y bienestar)

Revision ID: 0038_bienestar_registros
Revises: 0037_trace_nodes
Create Date: 2026-06-05

Módulo `bienestar` = registrador DETERMINISTA (sin LLM, sin extracción): un agente externo (p. ej.
Hermes) entiende el lenguaje natural por Telegram y llama a la CLI `memex-bienestar` con campos YA
estructurados; memex solo guarda. Una sola tabla `mod_bienestar_registros`, SIN atribución a `inbox`
(no hay mensaje ingerido) ni dedup (cada evento auto-reportado es distinto: dos comidas iguales son
dos comidas).

`occurred_at` es el instante del evento (el que da el agente, o `now()` si no lo da);
`occurred_at_precision` distingue si la hora es real (`datetime`) o solo se conoce la fecha (`date`).
`detail` es un hueco JSONB para campos finos a futuro (p. ej. calorías) sin migrar; `metadata` guarda
procedencia (p. ej. el texto original del agente). `event_id` correlaciona los hechos del MISMO
mensaje de Hermes (cross-module): la capa de relaciones wirea las aristas por ahí (NULL = hecho suelto).

`downgrade` dropea la tabla (forward-only; el dueño no preserva datos de bienestar al revertir).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0038_bienestar_registros"
down_revision: str | None = "0037_trace_nodes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mod_bienestar_registros (
            id                    BIGSERIAL PRIMARY KEY,
            user_id               BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category              TEXT NOT NULL DEFAULT 'otros'
                                    CHECK (category IN
                                      ('comida','higiene','ejercicio','grooming','salud','otros')),
            activity              TEXT NOT NULL DEFAULT '',
            occurred_at           TIMESTAMPTZ NOT NULL,
            occurred_at_precision TEXT NOT NULL DEFAULT 'datetime'
                                    CHECK (occurred_at_precision IN ('datetime','date')),
            description           TEXT NOT NULL DEFAULT '',
            detail                JSONB NOT NULL DEFAULT '{}'::jsonb,
            metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
            event_id              TEXT,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX mod_bienestar_registros_user_occurred
            ON mod_bienestar_registros (user_id, occurred_at DESC);
        CREATE INDEX mod_bienestar_registros_user_category
            ON mod_bienestar_registros (user_id, category);
        CREATE INDEX mod_bienestar_registros_user_activity
            ON mod_bienestar_registros (user_id, activity);
        CREATE INDEX mod_bienestar_registros_user_event
            ON mod_bienestar_registros (user_id, event_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mod_bienestar_registros CASCADE;")
