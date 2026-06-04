"""Generación de candidatos DIFUSOS para dedup de identidades (determinista, sin LLM).

Cuando las señales fuertes (`resolve.KnownIndex`) no atan una mención, se buscan candidatos por
SIMILITUD DE TRIGRAMAS (`pg_trgm`) contra el directorio:

  - personas: `similarity(name_norm, memex_norm(probe))`
  - orgs:     `similarity(org_core, memex_org_core(probe))`  (núcleo sin sufijos legales)

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
