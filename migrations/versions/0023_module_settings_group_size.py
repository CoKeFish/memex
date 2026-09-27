"""module_settings: group_size persistente (perilla de batching por módulo)

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-02

`module_settings` ya persistía `enabled` + `batching_policy` (0008). `group_size` era SOLO arg de CLI
(`memex-extract run --group-size`, default 3) — no se podía fijar por módulo. Ahora la UI de
/procesamiento lo expone como perilla (PATCH /modules/{slug}), así que pasa a columna persistente.
Default 3 = el mismo default del orquestador, así las filas existentes no cambian de comportamiento.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE module_settings ADD COLUMN group_size INT NOT NULL DEFAULT 3;")


def downgrade() -> None:
    op.execute("ALTER TABLE module_settings DROP COLUMN IF EXISTS group_size;")
