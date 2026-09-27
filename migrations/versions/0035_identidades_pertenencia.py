"""identidades: pertenencia (jerarquía «sub») — parent_identity_id self-FK

Revision ID: 0035_identidades_pertenencia
Revises: 0033_identidades_v2
Create Date: 2026-06-04

Sistema GENÉRICO de pertenencia «sub» para `mod_identidades`: un único vínculo «pertenece a» que
cuelga una identidad de otra (programa→universidad, producto→empresa, filial→matriz, área→org,
sub-marca→marca). Cada identidad tiene **a lo sumo UN padre** (`parent_identity_id` self-FK), con
cadenas multinivel permitidas. Sin tipo de relación (un solo «pertenece a»). El enlace lo decide el
organizador LLM holístico (`identidades/hierarchy.py`) y se puede editar a mano por la UI.

Migración ADITIVA y NO destructiva: solo agrega una columna nullable + CHECK anti self-loop + índice
parcial. No toca datos ni otras tablas → no necesita la guarda destructiva de 0033. `ON DELETE SET
NULL`: borrar el padre huerfaniza los hijos (no los borra). El CHECK solo atrapa el self-loop
DIRECTO (A→A); los ciclos multinivel (A→B→A) se previenen en Python (organizador + PATCH + merge).
La procedencia del link vive en `metadata.parent_source` (jsonb ya existente), sin columna extra.

Numeración (migration-numbering-worktrees): 0034 lo tiene el worktree `finanzas` (sin commitear,
`0034_finance_transactions`); 0035 verificado libre en todos los worktrees/ramas. `down_revision`
apunta a `0033_identidades_v2` (cabeza de esta rama); el merge de ramas reconciliará el orden.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0035_identidades_pertenencia"
down_revision: str | None = "0033_identidades_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE mod_identidades
            ADD COLUMN parent_identity_id BIGINT
                REFERENCES mod_identidades(id) ON DELETE SET NULL
        """
    )
    op.execute(
        """
        ALTER TABLE mod_identidades
            ADD CONSTRAINT mod_identidades_no_self_parent
            CHECK (parent_identity_id IS NULL OR parent_identity_id <> id)
        """
    )
    # Índice parcial: solo las filas con padre (la mayoría no tendrá) → barato para listar hijos.
    op.execute(
        """
        CREATE INDEX mod_identidades_parent
            ON mod_identidades (parent_identity_id)
            WHERE parent_identity_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS mod_identidades_parent;")
    op.execute(
        "ALTER TABLE mod_identidades DROP CONSTRAINT IF EXISTS mod_identidades_no_self_parent;"
    )
    op.execute("ALTER TABLE mod_identidades DROP COLUMN IF EXISTS parent_identity_id;")
