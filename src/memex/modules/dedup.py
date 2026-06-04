"""Dedup por business-key para los módulos (contrato v2: vértices únicos).

`upsert_unique` materializa una fila como VÉRTICE ÚNICO: busca por la business-key (NULL-safe) y, si
ya existe, fusiona los arrays declarados (típicamente `source_inbox_ids` — un vértice acumula sus
documentos de origen, ej. recibo + alerta del banco); si no, inserta. Es el patrón
resolve-then-create de identidades (`KnownIndex` + SELECT-first), generalizado para módulos con una
clave simple.

La unicidad la refuerza un UNIQUE de negocio (índice) en la migración del módulo. Las columnas de
texto de la clave (`norm_text`) se comparan NORMALIZADAS por la MISMA expresión SQL que usa ese
índice (`_NORM`: lower + colapso de whitespace) — la normalización la hace SIEMPRE la DB (sobre la
columna guardada y sobre el bind), nunca Python: así dos grafías del mismo comercio/nombre colapsan
y no hay divergencia entre el `casefold` de Python y el `lower` de Postgres. Espeja a identidades,
que deduplica con un índice funcional `UNIQUE(user_id, lower(name))` sin columna desnormalizada.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: Normalización canónica de una columna de texto para la business-key. DEBE coincidir EXACTAMENTE
#: con la expresión del índice UNIQUE en la migración del módulo (0030). `{x}` = columna o bind.
_NORM = "lower(btrim(regexp_replace({x}, '\\s+', ' ', 'g')))"


def _key_predicate(col: str, *, norm: bool) -> str:
    """Predicado NULL-safe de igualdad de la clave para `col`. Si `norm`, compara ambos lados
    (columna guardada y bind) bajo `_NORM` — la DB normaliza, no Python."""
    if norm:
        return f"{_NORM.format(x=col)} IS NOT DISTINCT FROM {_NORM.format(x=f':{col}')}"
    return f"{col} IS NOT DISTINCT FROM :{col}"


def upsert_unique(
    conn: Connection,
    table: str,
    *,
    identity: Mapping[str, Any],
    row: Mapping[str, Any],
    merge_arrays: Sequence[str] = (),
    norm_text: Sequence[str] = (),
) -> tuple[int, bool]:
    """Inserta `row` en `table`; si ya existe una fila con la misma `identity` (NULL-safe) fusiona
    en ella las columnas `merge_arrays` (asumidas `BIGINT[]`, ej. `source_inbox_ids`). Devuelve
    `(id, created)`.

    `table` y los nombres de columna son literales internos del módulo (NO input de usuario); los
    valores van siempre por bind. La igualdad de la clave usa `IS NOT DISTINCT FROM` para que una
    columna con `NULL` (ej. `occurred_on`) matchee a otra `NULL`. Las columnas en `norm_text` se
    comparan normalizadas por `_NORM` (mismo criterio que el índice UNIQUE de la migración): el
    valor crudo va en `identity`/`row` y la DB lo normaliza en ambos lados."""
    norm = set(norm_text)
    where = " AND ".join(_key_predicate(c, norm=c in norm) for c in identity)
    existing = conn.execute(
        text(f"SELECT id FROM {table} WHERE {where}"),
        dict(identity),
    ).scalar()
    if existing is not None:
        eid = int(existing)
        if merge_arrays:
            sets = ", ".join(
                f"{c} = (SELECT array_agg(DISTINCT v) "
                f"FROM unnest({c} || CAST(:{c} AS BIGINT[])) AS v)"
                for c in merge_arrays
            )
            conn.execute(
                text(f"UPDATE {table} SET {sets} WHERE id = :id"),
                {"id": eid, **{c: list(row[c]) for c in merge_arrays}},
            )
        return eid, False
    cols = ", ".join(row)
    binds = ", ".join(f":{c}" for c in row)
    new_id = conn.execute(
        text(f"INSERT INTO {table} ({cols}) VALUES ({binds}) RETURNING id"),
        dict(row),
    ).scalar_one()
    return int(new_id), True
