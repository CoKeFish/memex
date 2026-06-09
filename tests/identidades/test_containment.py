"""find_containment_candidates: 2ª fuente de candidatos por CONTENCIÓN DE TOKENS (subcadena /
abreviación del mismo nombre) que el trigram no alcanza (H-7). Contra la DB real (postgres-test).

Solo CANDIDATOS para el juez LLM — nunca auto-merge: un falso candidato se descarta sin daño.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.modules.identidades.fuzzy import find_containment_candidates
from memex.modules.identidades.module import _propose_merge_candidate


def _ins(conn: Any, kind: str, name: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1, :k, :n) RETURNING id"
            ),
            {"k": kind, "n": name},
        ).scalar_one()
    )


def _names(cands: list[Any]) -> set[str]:
    return {c.display_name for c in cands}


def test_short_probe_subset_of_existing(conn: Any) -> None:
    # probe corto (2 tokens) ⊂ identidad larga existente
    _ins(conn, "persona", "Rodion Romanovich Tabares Correa")
    cands = find_containment_candidates(conn, 1, kind="persona", probe="Rodion Tabares")
    assert "Rodion Romanovich Tabares Correa" in _names(cands)


def test_long_probe_contains_existing(conn: Any) -> None:
    # identidad corta (2 tokens) ⊂ probe largo
    _ins(conn, "persona", "Rodion Tabares")
    cands = find_containment_candidates(
        conn, 1, kind="persona", probe="Rodion Romanovich Tabares Correa"
    )
    assert "Rodion Tabares" in _names(cands)


def test_accents_normalized(conn: Any) -> None:
    # "Jose David" (probe) ⊂ "José David Reyes sanchez" — el unaccent lo hace memex_norm en la DB.
    _ins(conn, "persona", "José David Reyes sanchez")
    cands = find_containment_candidates(conn, 1, kind="persona", probe="Jose David")
    assert "José David Reyes sanchez" in _names(cands)


def test_single_token_probe_no_match(conn: Any) -> None:
    # "Jose" suelto (1 token) NO debe matchear a ningún "Jose X" (exige >= 2 en el lado corto).
    _ins(conn, "persona", "Jose Garcia")
    _ins(conn, "persona", "Jose Perez")
    assert find_containment_candidates(conn, 1, kind="persona", probe="Jose") == []


def test_disjoint_tokens_no_match(conn: Any) -> None:
    # "Jose Garcia" ni contiene ni está contenido en "Jose Perez" (garcia ≠ perez) → sin candidato.
    _ins(conn, "persona", "Jose Perez")
    cands = find_containment_candidates(conn, 1, kind="persona", probe="Jose Garcia")
    assert "Jose Perez" not in _names(cands)


def test_kind_scoped_persona_vs_org(conn: Any) -> None:
    # Una organización no debe salir como candidato al buscar una persona (mismo kind).
    _ins(conn, "organizacion", "Globex Holdings Corp")
    assert find_containment_candidates(conn, 1, kind="persona", probe="Globex Holdings") == []


def test_exclude_self(conn: Any) -> None:
    # `exclude_id` evita que el probe matchee a su propia identidad recién creada.
    long_id = _ins(conn, "persona", "Rodion Romanovich Tabares")
    self_id = _ins(conn, "persona", "Rodion Tabares")
    cands = find_containment_candidates(
        conn, 1, kind="persona", probe="Rodion Tabares", exclude_id=self_id
    )
    ids = {c.identity_id for c in cands}
    assert long_id in ids
    assert self_id not in ids


def test_no_duplicate_candidate_across_sources(conn: Any) -> None:
    # Caso 4: un par cubierto por trigram Y por contención no se duplica (ON CONFLICT por par
    # canónico a<b, sin importar el orden ni el `reason`).
    a = _ins(conn, "persona", "Rodion Romanovich Tabares Correa")
    b = _ins(conn, "persona", "Rodion Tabares")
    _propose_merge_candidate(conn, 1, a, b, "trgm_name", 0.7)
    _propose_merge_candidate(conn, 1, b, a, "token_containment", 0.4)  # orden inverso = mismo par
    n = int(
        conn.execute(
            text("SELECT count(*) FROM mod_identidades_merge_candidates WHERE user_id = 1")
        ).scalar_one()
    )
    assert n == 1
