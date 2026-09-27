"""Settings de extracción a nivel orquestador (una fila por usuario): perilla `routing_enabled`

Revision ID: 0080_extraction_settings
Revises: 0079_openai_provider
Create Date: 2026-06-19

Tabla PROPIA de settings del ORQUESTADOR de extracción (patrón `relevance_gate_settings`/
`identidades_resolver_settings`): la DB manda en runtime, sin fila → defaults (ruteo ENCENDIDO,
comportamiento previo). `routing_enabled`=FALSE desactiva el ruteo LLM por ventana: se extraen
TODOS los módulos candidatos (los que consumen ese tipo de mensaje) en una sola llamada agrupada,
sin gastar la llamada de ruteo. El pre-filtro determinista por tipo (`candidates_for_kind`) NO se
toca — eso no es ruteo LLM. Es per-usuario (cross-módulo), no per-módulo (eso es `module_settings`).

Numeración (migration-numbering-worktrees): head lineal = 0079_openai_provider; verificado libre
(main y el worktree relevancia-gate-regex están en 0077; 0078/0079 son de esta rama).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0080_extraction_settings"
down_revision: str | None = "0079_openai_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE extraction_settings (
            user_id          BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            routing_enabled  BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS extraction_settings CASCADE;")
