"""Asuntos: marca de revisión de evidencia por el curador

Revision ID: 0089_asuntos_evidence_reviewed
Revises: 0088_asuntos
Create Date: 2026-09-06

El curador revisa la evidencia DUDOSA (confianza baja del asignador, correos de lista pegados a
trámites/eventos) y decide keep / reject_evidence. `reviewed_at` evita re-preguntar lo ya
revisado: la revisión es incremental. La revisión de NOMBRES no necesita columna: se guarda en
`mod_asuntos.metadata` (`name_reviewed_name`) y solo vuelve a entrar si el nombre cambió.

Numeración (migration-numbering-worktrees): 0089 verificado libre; head lineal = 0088_asuntos.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0089_asuntos_evidence_reviewed"
down_revision: str | None = "0088_asuntos"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE mod_asuntos_evidence ADD COLUMN reviewed_at TIMESTAMPTZ;")


def downgrade() -> None:
    op.execute("ALTER TABLE mod_asuntos_evidence DROP COLUMN IF EXISTS reviewed_at;")
