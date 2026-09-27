"""identidades: org_core sin el sufijo legal 'spa' (evita auto-merge erróneo).

Revision ID: 0073_identidades_orgcore_sin_spa
Revises: 0072_relevance_composite_rules
Create Date: 2026-06-17

'spa' colapsaba el `org_core` de orgs DISTINTAS: `memex_org_core('Oxford Spa') = 'oxford' =
memex_org_core('Oxford Group')`, disparando el auto-merge por trigram (>= 0.92, `fuzzy.py`). Se quita
de los sufijos legales: 'Oxford Spa' conserva 'spa' en el núcleo ('oxford spa') y ya NO se funde con
'Oxford Group'. Espejo de `memex.modules.identidades.normalize._ORG_SUFFIXES` (lo verifica el test de
paridad Python↔SQL).

`org_core` es una columna GENERATED STORED → cambiar `memex_org_core` no recomputa los valores ya
almacenados. Para recomputarlos hay que droppear + re-agregar la columna (Postgres recomputa al
re-agregar); y, antes de reemplazar la función, soltar la dependencia (la columna y su índice).

Numeración: 0073 verificado libre en todos los worktrees/ramas (head = 0072). down_revision = head.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0073_identidades_orgcore_sin_spa"
down_revision: str | None = "0072_relevance_composite_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ESPEJO de `normalize._ORG_SUFFIXES` SIN 'spa' (test de paridad). Ordenados largo→corto.
_ORG_SUFFIXES = (
    "incorporated",
    "corporation",
    "technologies",
    "holdings",
    "company",
    "limited",
    "holding",
    "ltda",
    "grupo",
    "group",
    "gmbh",
    "corp",
    "oyj",
    "sapi",
    "eirl",
    "inc",
    "llc",
    "llp",
    "plc",
    "ltd",
    "sas",
    "sac",
    "sca",
    "scs",
    "slu",
    "srl",
    "pty",
    "pte",
    "ohg",
    "co",
    "sa",
    "sl",
    "ag",
    "bv",
    "oy",
    "kk",
    "kg",
)
#: Lista 0033/0057 (CON 'spa') para el downgrade.
_ORG_SUFFIXES_WITH_SPA = (*_ORG_SUFFIXES[:24], "spa", *_ORG_SUFFIXES[24:])


def _rebuild_org_core(suffix_re: str) -> None:
    """Recompone `memex_org_core` con `suffix_re` y RECOMPUTA la columna generada `org_core` (drop +
    re-add) + su índice trigram parcial (org/producto)."""
    op.execute("DROP INDEX IF EXISTS mod_identidades_orgcore_trgm;")
    op.execute("ALTER TABLE mod_identidades DROP COLUMN org_core;")
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION memex_org_core(text)
        RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS
        $$
          SELECT btrim(regexp_replace(
            regexp_replace(
              regexp_replace(replace(memex_norm($1), '.', ''), '[^a-z0-9]+', ' ', 'g'),
              '\\m({suffix_re})\\M', '', 'g'),
            '\\s+', ' ', 'g'))
        $$;
        """
    )
    op.execute(
        "ALTER TABLE mod_identidades ADD COLUMN org_core TEXT "
        "GENERATED ALWAYS AS (memex_org_core(display_name)) STORED;"
    )
    op.execute(
        "CREATE INDEX mod_identidades_orgcore_trgm ON mod_identidades "
        "USING GIN (org_core gin_trgm_ops) WHERE kind IN ('organizacion','producto');"
    )


def upgrade() -> None:
    _rebuild_org_core("|".join(_ORG_SUFFIXES))


def downgrade() -> None:
    _rebuild_org_core("|".join(_ORG_SUFFIXES_WITH_SPA))
