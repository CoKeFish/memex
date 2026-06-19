"""Schema check de `relevance_gate_rules` con el modelo REGEX (migración 0077).

Verifica el DDL con SQL CRUDO (saltea la capa Python para ejercitar los CHECK de la DB): la
polaridad `effect`, el predicado de patrón (`pattern` regex + `match_field`, pareados), el
≥1-predicado, el remitente pareado, el `match_field IN (...)`, y el dedupe por (user, effect,
sender lower, pattern EXACTO, match_field). El patrón NO se baja a lower en el dedupe — el case del
regex es significativo (`\\D` ≠ `\\d`), a diferencia del remitente, que sí es case-insensitive.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memex.db import connection

_COLS = "(user_id, effect, sender_kind, sender_value, pattern, match_field, status, proposed_by)"


def _insert(
    effect: str,
    sender_kind: str | None,
    sender_value: str | None,
    pattern: str | None,
    match_field: str | None,
) -> None:
    with connection() as c:
        c.execute(
            text(
                f"INSERT INTO relevance_gate_rules {_COLS} "
                "VALUES (1, :e, :sk, :sv, :p, :mf, 'active', 'manual')"
            ),
            {"e": effect, "sk": sender_kind, "sv": sender_value, "p": pattern, "mf": match_field},
        )


def test_effect_check_rejects_unknown() -> None:
    with pytest.raises(IntegrityError):
        _insert("maybe", "sender_email", "a@b.com", None, None)


def test_requires_at_least_one_predicate() -> None:
    with pytest.raises(IntegrityError):
        _insert("block", None, None, None, None)


def test_sender_kind_and_value_travel_together() -> None:
    with pytest.raises(IntegrityError):  # kind sin value (aunque haya patrón)
        _insert("block", "sender_domain", None, "oferta", "subject")


def test_pattern_and_match_field_travel_together() -> None:
    with pytest.raises(IntegrityError):  # patrón sin match_field
        _insert("block", None, None, "oferta", None)
    with pytest.raises(IntegrityError):  # match_field sin patrón
        _insert("block", None, None, None, "subject")


def test_match_field_check_rejects_unknown() -> None:
    with pytest.raises(IntegrityError):
        _insert("block", None, None, "oferta", "header")


def test_dedupe_per_effect_sender_ci_pattern_cs() -> None:
    # La MISMA firma convive en las DOS polaridades (dedupe incluye effect).
    _insert("block", "sender_domain", "x.com", "oferta", "subject")
    _insert("allow", "sender_domain", "x.com", "oferta", "subject")
    with connection() as c:
        n = c.execute(text("SELECT count(*) FROM relevance_gate_rules")).scalar()
    assert n == 2
    # Remitente case-insensitive (lower en el índice) + patrón EXACTO + match_field → choca.
    with pytest.raises(IntegrityError):
        _insert("block", "sender_domain", "X.COM", "oferta", "subject")


def test_dedupe_pattern_is_case_sensitive() -> None:
    # El patrón NO se baja a lower en el dedupe: distinto case = regla distinta (`\\D` ≠ `\\d`).
    _insert("block", None, None, "oferta", "subject")
    _insert("block", None, None, "Oferta", "subject")  # case distinto → NO choca
    with connection() as c:
        n = c.execute(text("SELECT count(*) FROM relevance_gate_rules")).scalar()
    assert n == 2


def test_mining_min_messages_default_is_three() -> None:
    # user_id=1 ya existe en el fixture; sin fila de settings → el INSERT toma el default del DDL.
    with connection() as c:
        c.execute(text("INSERT INTO relevance_gate_settings (user_id) VALUES (1)"))
        default = c.execute(
            text("SELECT mining_min_messages FROM relevance_gate_settings WHERE user_id = 1")
        ).scalar()
    assert default == 3
