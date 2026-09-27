"""identidades: más sufijos legales en `org_core` (pbc, esp, cia, sarl, fz, nv, lp)

Revision ID: 0087_identidades_orgcore_v3
Revises: 0086_identidades_link_claims
Create Date: 2026-09-05

«Anthropic, PBC» y «Anthropic» convivían como dos organizaciones (una colgando de la otra) porque
`memex_org_core('Anthropic, PBC') = 'anthropic pbc'` ≠ 'anthropic': 'pbc' (Public Benefit
Corporation) no estaba en los sufijos legales, así que ni el resolve-or-create ni el par por
contención del curador las veían como la misma. Lo mismo con 'esp' («TIGO - COLOMBIA MOVIL S.A.
ESP»), 'fz' («Telegram FZ-LLC» → tokens fz + llc), 'cia', 'sarl', 'nv', 'lp'. Se agregan al espejo
SQL de `normalize._ORG_SUFFIXES` (el test de paridad Python↔SQL lo verifica) y se recomputa la
columna generada (drop + re-add, como en 0073).

Numeración (migration-numbering-worktrees): 0087 verificado libre en main + worktrees; head lineal
= 0086_identidades_link_claims.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0087_identidades_orgcore_v3"
down_revision: str | None = "0086_identidades_link_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ESPEJO de `normalize._ORG_SUFFIXES` (test de paridad). Ordenados largo→corto.
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
    "sarl",
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
    "pbc",
    "esp",
    "cia",
    "co",
    "sa",
    "sl",
    "ag",
    "bv",
    "oy",
    "kk",
    "kg",
    "fz",
    "nv",
    "lp",
)
#: Lista 0073 (sin los nuevos) para el downgrade.
_ORG_SUFFIXES_PREV = tuple(
    s for s in _ORG_SUFFIXES if s not in ("sarl", "pbc", "esp", "cia", "fz", "nv", "lp")
)


def _rebuild_org_core(suffix_re: str) -> None:
    """Reemplaza `memex_org_core` y recomputa la columna generada `org_core` (drop + re-add) + su
    índice trigram parcial (org/producto). Igual que 0073."""
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
    _rebuild_org_core("|".join(_ORG_SUFFIXES_PREV))
