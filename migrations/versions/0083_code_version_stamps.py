"""Sello de versión del código por etapa: `code_version` en inbox, classifications,
relevance_verdicts, module_extractions y worker_runs

Revision ID: 0083_code_version_stamps
Revises: 0082_accounts_health_detail
Create Date: 2026-09-04

Los datos de dev conviven con filas producidas por parsers/prompts distintos (cuerpos con relleno,
horas en UTC, reglas de identidades viejas). Cada etapa que escribe su resultado deja el
identificador del código que lo produjo (`memex.version.code_version()`: sha corto de git o el
build-arg de la imagen; «unknown» si no se supo). NULL = fila anterior a este sello.

Numeración (migration-numbering-worktrees): head lineal = 0082; el único otro worktree vivo
(relevancia-gate-regex) está en 0077 → 0083 libre.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0083_code_version_stamps"
down_revision: str | None = "0082_accounts_health_detail"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("inbox", "classifications", "relevance_verdicts", "module_extractions", "worker_runs")


def upgrade() -> None:
    for t in _TABLES:
        op.execute(f"ALTER TABLE {t} ADD COLUMN code_version TEXT")


def downgrade() -> None:
    for t in reversed(_TABLES):
        op.execute(f"ALTER TABLE {t} DROP COLUMN code_version")
