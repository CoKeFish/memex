"""`find_fuzzy_candidates`: similitud trigram (`pg_trgm`) contra el directorio, acotada por kind.
Para orgs, strip de sufijos legales. Corre contra la DB real (índices GIN + funciones memex_*)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.modules.identidades.fuzzy import LOW_THRESHOLD, find_fuzzy_candidates


def _seed(conn: Any) -> None:
    conn.execute(
        text(
            "INSERT INTO mod_identidades (user_id, kind, display_name) VALUES "
            "(1,'persona','Ada Lovelace'),(1,'organizacion','Acme S.A.S.'),"
            "(1,'organizacion','Globex')"
        )
    )


def test_person_typo_matches(conn: Any) -> None:
    _seed(conn)
    cands = find_fuzzy_candidates(conn, 1, kind="persona", probe="Adda Lovelace")
    assert cands and cands[0].display_name == "Ada Lovelace"
    assert cands[0].score >= LOW_THRESHOLD


def test_org_core_match_ignores_legal_suffix(conn: Any) -> None:
    _seed(conn)
    cands = find_fuzzy_candidates(conn, 1, kind="organizacion", probe="Acme SAS")
    assert cands and cands[0].display_name == "Acme S.A.S."
    assert cands[0].score >= 0.9  # núcleo idéntico ('acme')


def test_no_match_below_threshold(conn: Any) -> None:
    _seed(conn)
    assert find_fuzzy_candidates(conn, 1, kind="persona", probe="Zxqwvkpt") == []


def test_kind_scoped(conn: Any) -> None:
    _seed(conn)
    # 'Globex' es una org; buscarlo como persona no debe traerlo
    assert find_fuzzy_candidates(conn, 1, kind="persona", probe="Globex") == []


def test_empty_probe(conn: Any) -> None:
    _seed(conn)
    assert find_fuzzy_candidates(conn, 1, kind="persona", probe="   ") == []
