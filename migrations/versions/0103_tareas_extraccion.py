"""Tareas extraídas de los mensajes: procedencia y origen en `mod_tareas`

Revision ID: 0103_tareas_extraccion
Revises: 0102_whatsapp
Create Date: 2026-09-13

Hasta acá `tareas` era un registrador manual (bot / consola): ningún mensaje generaba una tarea
solo. El dueño lo descubrió con el grupo de su curso de inglés («¿registró las tareas?»: cero).
Ahora `tareas` es también un módulo de EXTRACCIÓN como calendario: cada tarea que sale de un
mensaje guarda de qué mensajes salió (`source_inbox_ids`, para atribución, traza y re-extracción
en limpio) y la cita textual que la justifica (`evidence`). `origin` separa lo manual de lo
extraído: la re-extracción olvida solo lo extraído.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0103_tareas_extraccion"
down_revision: str | None = "0102_whatsapp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_tareas
            ADD COLUMN source_inbox_ids BIGINT[] NOT NULL DEFAULT '{}'::bigint[],
            ADD COLUMN evidence TEXT NOT NULL DEFAULT '',
            ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'
                CHECK (origin IN ('manual', 'extraction'));
        CREATE INDEX mod_tareas_source_inbox ON mod_tareas USING GIN (source_inbox_ids);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS mod_tareas_source_inbox;
        ALTER TABLE mod_tareas
            DROP COLUMN IF EXISTS origin,
            DROP COLUMN IF EXISTS evidence,
            DROP COLUMN IF EXISTS source_inbox_ids;
        """
    )
