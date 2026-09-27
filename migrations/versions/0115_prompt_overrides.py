"""Prompts editables desde el dashboard: override vigente por clave + historial de versiones.

Revision ID: 0115_prompt_overrides
Revises: 0114_tareas_origin_calendar

El dueño quiere ver y editar los prompts en una ventana, con confirmación (2026-09-26). El
código sigue siendo el default; `prompt_overrides` guarda el texto vigente por clave del
registro (`memex.prompts.registry`) y `prompt_override_versions` cada guardado (para volver a
una versión). Ver `memex.prompts`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0115_prompt_overrides"
down_revision: str | None = "0114_tareas_origin_calendar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE prompt_overrides (
            key        TEXT PRIMARY KEY,
            text       TEXT NOT NULL,
            note       TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE prompt_override_versions (
            id         BIGSERIAL PRIMARY KEY,
            key        TEXT NOT NULL,
            text       TEXT NOT NULL,
            note       TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX prompt_override_versions_key_idx ON prompt_override_versions (key, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS prompt_override_versions")
    op.execute("DROP TABLE IF EXISTS prompt_overrides")
