"""Resolvedor contextual de identidades por-correo: settings del módulo (una fila por usuario)

Revision ID: 0078_identidades_resolver
Revises: 0077_relevance_regex_rules
Create Date: 2026-06-19

Tabla PROPIA de settings del resolvedor contextual de identidades (patrón
`relevance_gate_settings`/`scheduler_settings`): la DB manda en runtime, sin fila → defaults
APAGADOS. `resolver_enabled` gatea la fase contextual por-correo dentro de `module.dedup`;
`batch_maintenance_enabled` gatea el mantenimiento por lotes (organize + merge phase-2) del ciclo
del scheduler — ambos default FALSE (se prenden tras validar). Los umbrales acotan el merge/
jerarquía que propone el LLM; `max_calls_per_window` topa el costo LLM por ventana. El proveedor/
modelo los decide el registry por consumer (`identidades_resolve`), no esta tabla.

Numeración (migration-numbering-worktrees): head lineal = 0077_relevance_regex_rules; verificado
libre (el worktree relevancia-gate-regex comparte migraciones hasta 0077).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0078_identidades_resolver"
down_revision: str | None = "0077_relevance_regex_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE identidades_resolver_settings (
            user_id                   BIGINT PRIMARY KEY
                                        REFERENCES users(id) ON DELETE CASCADE,
            resolver_enabled          BOOLEAN NOT NULL DEFAULT FALSE,
            batch_maintenance_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            min_confidence_merge      NUMERIC NOT NULL DEFAULT 0.75
                                        CHECK (min_confidence_merge BETWEEN 0 AND 1),
            min_confidence_parent     NUMERIC NOT NULL DEFAULT 0.80
                                        CHECK (min_confidence_parent BETWEEN 0 AND 1),
            max_calls_per_window      INT NOT NULL DEFAULT 16
                                        CHECK (max_calls_per_window >= 1),
            updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS identidades_resolver_settings CASCADE;")
