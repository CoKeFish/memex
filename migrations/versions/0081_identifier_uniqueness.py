"""Unicidad global de identificadores fuertes: un email/teléfono/dominio pertenece a UNA identidad

Revision ID: 0081_identifier_uniqueness
Revises: 0080_extraction_settings
Create Date: 2026-06-20

El modelo de `identidades` dice: un identificador (email/teléfono/dominio) es un ATRIBUTO de una
identidad, y pertenece a UNA sola. Hoy `mod_identidades_identifiers` solo tiene UNIQUE por-ficha
(`identity_id, platform, kind, value_norm`), así que el MISMO email podía colgar de dos fichas
(bug observado: bafs-newsletter@mail.beehiiv.com en BAF + beehiiv).

(a) Limpia los duplicados cross-identidad existentes — conserva la fila de la identidad de menor id
    (data de dev desechable; el clean break es aceptable).
(b) Crea un índice ÚNICO PARCIAL sobre (user_id, kind, value_norm) para email/phone/domain. `handle`
    queda FUERA (es por-plataforma: @foo en X ≠ @foo en Instagram → pueden ser identidades
    distintas). El guard de aplicación (`module._insert_identifier`) evita llegar a violar el índice.

Numeración (migration-numbering-worktrees): head lineal = 0080_extraction_settings; verificado libre
(main y el worktree relevancia-gate-regex están en 0077; 0078-0080 son de esta rama). El revision id
se mantiene <=32 chars (columna `alembic_version.version_num`).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0081_identifier_uniqueness"
down_revision: str | None = "0080_extraction_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        -- (a) limpiar duplicados cross-identidad: conservar la identidad de MENOR id.
        DELETE FROM mod_identidades_identifiers a
        USING mod_identidades_identifiers b
        WHERE a.user_id = b.user_id
          AND a.kind = b.kind
          AND a.value_norm = b.value_norm
          AND a.kind IN ('email', 'phone', 'domain')
          AND a.identity_id > b.identity_id;

        -- (b) unicidad global de identificadores fuertes (handle queda por-plataforma, fuera).
        CREATE UNIQUE INDEX mod_identidades_idf_strong_uq
            ON mod_identidades_identifiers (user_id, kind, value_norm)
            WHERE kind IN ('email', 'phone', 'domain');
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS mod_identidades_idf_strong_uq;")
