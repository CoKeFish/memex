"""Unicidad global de identificadores fuertes (email/phone/domain = una identidad; índice 0081).

El modelo: un identificador fuerte es un atributo y pertenece a UNA identidad. El guard de
aplicación (`_insert_identifier` vía `identifier_owner`) evita colgar uno que ya es de otra ficha;
el índice único parcial es el backstop. `handle` queda fuera (es por-plataforma)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memex.modules.identidades.module import (
    STRONG_ID_KINDS,
    _insert_identifier,
    identifier_owner,
)


def _mk(conn: Any, kind: str, name: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1, :k, :n) RETURNING id"
            ),
            {"k": kind, "n": name},
        ).scalar_one()
    )


def _owners(conn: Any, kind: str, vn: str) -> list[int]:
    return [
        int(r[0])
        for r in conn.execute(
            text(
                "SELECT identity_id FROM mod_identidades_identifiers "
                "WHERE user_id = 1 AND kind = :k AND value_norm = :vn ORDER BY identity_id"
            ),
            {"k": kind, "vn": vn},
        ).all()
    ]


def _raw_insert(conn: Any, identity_id: int, platform: str, kind: str, vn: str) -> None:
    conn.execute(
        text(
            "INSERT INTO mod_identidades_identifiers "
            "(user_id, identity_id, platform, kind, value, value_norm, source) "
            "VALUES (1, :id, :p, :k, :vn, :vn, 'manual')"
        ),
        {"id": identity_id, "p": platform, "k": kind, "vn": vn},
    )


def test_strong_kinds_set() -> None:
    assert "email" in STRONG_ID_KINDS
    assert "phone" in STRONG_ID_KINDS
    assert "domain" in STRONG_ID_KINDS
    assert "handle" not in STRONG_ID_KINDS  # por-plataforma, fuera de la unicidad global


def test_identifier_owner_strong_only(conn: Any) -> None:
    a = _mk(conn, "organizacion", "Acme")
    _insert_identifier(conn, 1, a, "email", "email", "info@acme.com", "info@acme.com")
    assert identifier_owner(conn, 1, "email", "info@acme.com") == a
    assert identifier_owner(conn, 1, "email", "otro@acme.com") is None
    # handle no es fuerte → siempre None (no participa de la unicidad global)
    assert identifier_owner(conn, 1, "handle", "info@acme.com") is None


def test_insert_skips_cross_identity_strong(conn: Any) -> None:
    a = _mk(conn, "organizacion", "Acme")
    b = _mk(conn, "organizacion", "Beta")
    _insert_identifier(conn, 1, a, "email", "email", "info@acme.com", "info@acme.com")
    # intentar colgar el MISMO email en otra ficha → no se cuelga (queda con la dueña)
    _insert_identifier(conn, 1, b, "email", "email", "info@acme.com", "info@acme.com")
    assert _owners(conn, "email", "info@acme.com") == [a]


def test_insert_same_identity_idempotent(conn: Any) -> None:
    a = _mk(conn, "organizacion", "Acme")
    _insert_identifier(conn, 1, a, "email", "email", "info@acme.com", "info@acme.com")
    _insert_identifier(conn, 1, a, "email", "email", "info@acme.com", "info@acme.com")
    assert _owners(conn, "email", "info@acme.com") == [a]


def test_handle_not_globally_unique(conn: Any) -> None:
    # handle es por-plataforma: @foo en X e Instagram pueden ser identidades distintas.
    a = _mk(conn, "persona", "A")
    b = _mk(conn, "persona", "B")
    _insert_identifier(conn, 1, a, "twitter", "handle", "@foo", "foo")
    _insert_identifier(conn, 1, b, "instagram", "handle", "@foo", "foo")
    assert _owners(conn, "handle", "foo") == [a, b]


def test_db_index_is_backstop(conn: Any) -> None:
    # El índice único parcial rechaza un insert crudo cross-identidad (bypassa el guard de app).
    a = _mk(conn, "organizacion", "Acme")
    b = _mk(conn, "organizacion", "Beta")
    _raw_insert(conn, a, "email", "email", "x@y.com")
    with pytest.raises(IntegrityError), conn.begin_nested():
        _raw_insert(conn, b, "email", "email", "x@y.com")
