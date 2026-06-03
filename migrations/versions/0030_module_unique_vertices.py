"""vértice único por módulo: business-key UNIQUE para finance y hackathones (contrato v2)

Revision ID: 0030
Revises: 0029_identidades
Create Date: 2026-06-03

Contrato v2 (cada módulo produce vértices ÚNICOS): finance y hackathones eran extractores planos
(INSERT sin dedup → el mismo pago/hackatón reportado dos veces creaba filas duplicadas). Esta
migración les da la **unicidad de negocio** con un **índice funcional UNIQUE** (sin columna
desnormalizada), igual que identidades deduplica con `UNIQUE(user_id, lower(name))`:

- el comercio/nombre se compara normalizado por la expresión `_NORM` (lower + colapso de whitespace),
  computada por la DB dentro del índice → cualquier writer (módulo, API, backfill, tests) queda
  cubierto sin depender de que Python setee una columna `_norm`;
- la fecha NULL se trata como centinela (`COALESCE(col, '0001-01-01')`) para que dos filas sin fecha
  y resto igual SÍ colapsen (un índice UNIQUE normal trataría cada NULL como distinto);
- antes de crear el índice se COLAPSAN los duplicados existentes (se fusiona `source_inbox_ids` en el
  de menor id y se borran los demás), si no el índice fallaría sobre datos ya duplicados.

`_NORM` DEBE coincidir EXACTAMENTE con `memex.modules.dedup._NORM` (el `persist` busca por la misma
expresión). Numeración (migration-numbering-worktrees): cabeza de main = 0029_identidades; 0030 libre
en TODOS los worktrees (verificado).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029_identidades"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Normalización de texto de la business-key (espejo de `memex.modules.dedup._NORM`).
_NORM = "lower(btrim(regexp_replace({x}, '\\s+', ' ', 'g')))"
_SENTINEL = "DATE '0001-01-01'"


def _collapse_duplicates(table: str, key_cols: str) -> None:
    """Fusiona `source_inbox_ids` en la fila de menor id por cada grupo de la business-key y borra
    las demás (deja la DB lista para el UNIQUE). `key_cols` = la lista de columnas/expresiones que
    definen el grupo (las mismas del índice)."""
    op.execute(
        f"""
        UPDATE {table} k SET source_inbox_ids = g.merged
        FROM (
            SELECT min(e.id) AS keep_id, array_agg(DISTINCT sid) AS merged
            FROM {table} e, unnest(e.source_inbox_ids) AS sid
            GROUP BY {key_cols}
        ) g
        WHERE k.id = g.keep_id;
        """
    )
    op.execute(
        f"""
        DELETE FROM {table} d USING (
            SELECT id, min(id) OVER (PARTITION BY {key_cols}) AS keep_id
            FROM {table}
        ) x
        WHERE d.id = x.id AND d.id <> x.keep_id;
        """
    )


def upgrade() -> None:
    # finance: (user_id, currency, amount, fecha-centinela, comercio-normalizado)
    fin_key = f"user_id, currency, amount, COALESCE(occurred_on, {_SENTINEL}), {_NORM.format(x='merchant')}"
    _collapse_duplicates("mod_finance_expenses", fin_key)
    op.execute(
        f"CREATE UNIQUE INDEX mod_finance_expenses_identity ON mod_finance_expenses ({fin_key});"
    )

    # hackathones: (user_id, nombre-normalizado, fecha-centinela)
    hack_key = f"user_id, {_NORM.format(x='name')}, COALESCE(starts_on, {_SENTINEL})"
    _collapse_duplicates("mod_hackathones_events", hack_key)
    op.execute(
        f"CREATE UNIQUE INDEX mod_hackathones_events_identity ON mod_hackathones_events ({hack_key});"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS mod_hackathones_events_identity;")
    op.execute("DROP INDEX IF EXISTS mod_finance_expenses_identity;")
