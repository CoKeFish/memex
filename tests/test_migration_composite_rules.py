"""Schema check de la migración 0072 (reglas del gate compuestas + bipolares).

Verifica el DDL del modelo nuevo de `relevance_gate_rules` con SQL CRUDO (saltea la validación de
la capa Python para ejercitar los CHECK de la DB): la polaridad `effect`, los slots del predicado
compuesto (remitente + asunto, ≥1, remitente y valor juntos) y el dedupe por (user, effect,
predicados) case-insensitive. Además el nuevo default del disparador de minería.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memex.db import connection

_COLS = "(user_id, effect, sender_kind, sender_value, subject_pattern, status, proposed_by)"


def _insert(
    effect: str,
    sender_kind: str | None,
    sender_value: str | None,
    subject_pattern: str | None,
) -> None:
    with connection() as c:
        c.execute(
            text(
                f"INSERT INTO relevance_gate_rules {_COLS} "
                "VALUES (1, :e, :sk, :sv, :sp, 'active', 'manual')"
            ),
            {"e": effect, "sk": sender_kind, "sv": sender_value, "sp": subject_pattern},
        )


def test_effect_check_rejects_unknown() -> None:
    with pytest.raises(IntegrityError):
        _insert("maybe", "sender_email", "a@b.com", None)


def test_requires_at_least_one_predicate() -> None:
    with pytest.raises(IntegrityError):
        _insert("block", None, None, None)


def test_sender_kind_and_value_travel_together() -> None:
    with pytest.raises(IntegrityError):  # kind sin value (aunque haya patrón de asunto)
        _insert("block", "sender_domain", None, "oferta")


def test_dedupe_is_per_effect_and_case_insensitive() -> None:
    # La MISMA firma de predicados convive en las DOS polaridades (dedupe incluye effect).
    _insert("block", "sender_domain", "x.com", "oferta")
    _insert("allow", "sender_domain", "x.com", "oferta")
    with connection() as c:
        n = c.execute(text("SELECT count(*) FROM relevance_gate_rules")).scalar()
    assert n == 2
    # Misma firma + misma polaridad (case-insensitive) → choca con el índice único.
    with pytest.raises(IntegrityError):
        _insert("block", "sender_domain", "X.COM", "Oferta")


def test_mining_min_messages_default_is_three() -> None:
    # user_id=1 ya existe en el fixture; sin fila de settings → el INSERT toma el default del DDL.
    with connection() as c:
        c.execute(text("INSERT INTO relevance_gate_settings (user_id) VALUES (1)"))
        default = c.execute(
            text("SELECT mining_min_messages FROM relevance_gate_settings WHERE user_id = 1")
        ).scalar()
    assert default == 3
