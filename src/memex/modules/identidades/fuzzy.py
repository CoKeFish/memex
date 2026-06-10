"""Generación de candidatos DIFUSOS para dedup de identidades (determinista, sin LLM).

Cuando las señales fuertes (`resolve.KnownIndex`) no atan una mención, se buscan candidatos por
SIMILITUD DE TRIGRAMAS (`pg_trgm`) contra el directorio:

  - personas:        `similarity(name_norm, memex_norm(probe))`
  - orgs/productos:  `similarity(org_core, memex_org_core(probe))`  (núcleo sin sufijos legales)

El match es kind-scoped (`WHERE kind = :kind`): un producto solo matchea contra productos.

El `%` usa el índice GIN (`gin_trgm_ops`) para acotar candidatos; luego se filtra por umbral real y
se ordena por similitud desc, con `levenshtein` como desempate determinista. El consumidor
(`module.dedup`) decide: `>= HIGH_THRESHOLD` → auto-merge; zona gris `[LOW, HIGH)` → candidato para
el desempate LLM; `< LOW_THRESHOLD` → identidad nueva.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: Umbrales de similitud (trigram, [0,1]). Tunables.
HIGH_THRESHOLD = 0.92
LOW_THRESHOLD = 0.55


@dataclass(frozen=True)
class FuzzyCandidate:
    """Un candidato de identidad similar a una mención, con su score de trigramas."""

    identity_id: int
    display_name: str
    kind: str
    score: float


def find_fuzzy_candidates(
    conn: Connection,
    user_id: int,
    *,
    kind: str,
    probe: str,
    limit: int = 5,
    threshold: float = LOW_THRESHOLD,
) -> list[FuzzyCandidate]:
    """Candidatos similares a `probe` en el directorio del user, acotados por `kind`. La
    normalización (memex_norm / memex_org_core) la hace la DB sobre `probe` y sobre la columna
    generada → sin divergencia Python↔SQL. `kind`/columnas son literales internos del módulo."""
    if not probe.strip():
        return []
    col = "name_norm" if kind == "persona" else "org_core"
    probe_expr = "memex_norm(:probe)" if kind == "persona" else "memex_org_core(:probe)"
    sql = text(
        f"""
        SELECT id, display_name, kind, similarity({col}, {probe_expr}) AS score
        FROM mod_identidades
        WHERE user_id = :uid AND kind = :kind
          AND {col} % {probe_expr}
          AND similarity({col}, {probe_expr}) >= :threshold
        ORDER BY score DESC,
                 levenshtein(left({col}, 255), left({probe_expr}, 255)) ASC,
                 id ASC
        LIMIT :limit
        """
    )
    rows = (
        conn.execute(
            sql,
            {
                "uid": user_id,
                "kind": kind,
                "probe": probe,
                "threshold": threshold,
                "limit": limit,
            },
        )
        .mappings()
        .all()
    )
    return [
        FuzzyCandidate(
            identity_id=int(r["id"]),
            display_name=str(r["display_name"]),
            kind=str(r["kind"]),
            score=float(r["score"]),
        )
        for r in rows
    ]


def find_containment_candidates(
    conn: Connection,
    user_id: int,
    *,
    kind: str,
    probe: str,
    exclude_id: int | None = None,
    limit: int = 5,
) -> list[FuzzyCandidate]:
    """Candidatos por CONTENCIÓN DE TOKENS: un nombre cuyo conjunto de palabras está contenido
    ESTRICTAMENTE en el otro (el largo tiene más palabras) y el lado CORTO tiene >= 2 palabras.

    Captura subcadenas/abreviaciones del MISMO nombre que el trigram NO alcanza ("Rodion Tabares" ⊂
    "Rodion Romanovich Tabares Correa"; "Jose David" ⊂ "José David Reyes sanchez"): cuando un nombre
    es subset del otro, la similitud trigram cae bajo el umbral y nunca se propone como candidato.
    Es una SEGUNDA fuente de candidatos para el desempate LLM, JUNTO a la de trigramas — solo
    CANDIDATOS (el caller nunca auto-fusiona por esta vía). El `score` es la similitud trigram real
    (informativa; suele estar bajo `LOW_THRESHOLD`, por eso el trigram la pierde). El `>= 2` en
    el lado corto evita que un token suelto ("jose") matchee a todos los "jose x". Mismo `kind`; la
    normalización (memex_norm / memex_org_core) la hace la DB (sin divergencia Python↔SQL)."""
    if not probe.strip():
        return []
    col = "name_norm" if kind == "persona" else "org_core"
    probe_expr = "memex_norm(:probe)" if kind == "persona" else "memex_org_core(:probe)"
    # Tokens por espacios sobre el valor YA normalizado (columna generada / memex_* sobre el probe).
    col_toks = f"string_to_array({col}, ' ')"
    probe_toks = f"string_to_array({probe_expr}, ' ')"
    # Excluir-self condicional: un :exclude_id NULL sin tipo confunde a psycopg ("could not
    # determine data type"); con la cláusula ausente, el parámetro ni se manda.
    exclude_clause = "AND id <> :exclude_id" if exclude_id is not None else ""
    sql = text(
        f"""
        SELECT id, display_name, kind, similarity({col}, {probe_expr}) AS score
        FROM mod_identidades
        WHERE user_id = :uid AND kind = :kind
          {exclude_clause}
          AND (
            -- la identidad CONTIENE al probe (probe corto ⊂ identidad larga); probe >= 2 palabras
            ({col_toks} @> {probe_toks}
             AND cardinality({col_toks}) > cardinality({probe_toks})
             AND cardinality({probe_toks}) >= 2)
            OR
            -- el probe CONTIENE a la identidad (identidad corta ⊂ probe largo)
            ({probe_toks} @> {col_toks}
             AND cardinality({probe_toks}) > cardinality({col_toks})
             AND cardinality({col_toks}) >= 2)
          )
        ORDER BY score DESC, id ASC
        LIMIT :limit
        """
    )
    params: dict[str, object] = {"uid": user_id, "kind": kind, "probe": probe, "limit": limit}
    if exclude_id is not None:
        params["exclude_id"] = exclude_id
    rows = conn.execute(sql, params).mappings().all()
    return [
        FuzzyCandidate(
            identity_id=int(r["id"]),
            display_name=str(r["display_name"]),
            kind=str(r["kind"]),
            score=float(r["score"]),
        )
        for r in rows
    ]
