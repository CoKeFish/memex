"""Paridad Python↔SQL de la normalización (`normalize_match`↔`memex_norm`, `org_core`↔
`memex_org_core`) + `norm_identifier` por kind. La paridad es crítica: el match exacto en memoria
(Python) debe coincidir con los índices/trigram (SQL). Excluye letras especiales NO descomponibles
por NFKD que `unaccent` mapea distinto (ß/ø/æ…), divergencia conocida y documentada."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.modules.identidades.normalize import norm_identifier, normalize_match, org_core

_NAMES = ["Café Ñoño", "  Ada   Lovelace  ", "JOSÉ", "naïve façade", "Müller", "Bogotá D.C."]
_ORGS = ["Acme S.A.S.", "Unity Technologies", "Grupo Bolívar S.A.", "OpenAI, Inc.", "Ñandú Ltda"]


def test_normalize_match_parity(conn: Any) -> None:
    for s in _NAMES + _ORGS:
        db = conn.execute(text("SELECT memex_norm(:s)"), {"s": s}).scalar_one()
        assert normalize_match(s) == db, f"{s!r}: py={normalize_match(s)!r} db={db!r}"


def test_org_core_parity(conn: Any) -> None:
    for s in _ORGS + _NAMES:
        db = conn.execute(text("SELECT memex_org_core(:s)"), {"s": s}).scalar_one()
        assert org_core(s) == db, f"org_core divergió en {s!r}: py={org_core(s)!r} db={db!r}"


def test_org_core_strips_legal_suffixes() -> None:
    assert org_core("Acme S.A.S.") == "acme"
    assert org_core("Unity Technologies") == "unity"
    assert org_core("Grupo Bolívar S.A.") == "bolivar"
    assert org_core("OpenAI, Inc.") == "openai"


def test_norm_identifier() -> None:
    assert norm_identifier("email", "  Ada@X.COM ") == "ada@x.com"
    assert norm_identifier("handle", "@AdaL") == "adal"
    assert norm_identifier("domain", "info@Unity.com") == "unity.com"
    assert norm_identifier("phone", "+57 (300) 123-45") == "+5730012345"
    assert norm_identifier("url", "HTTPS://Example.com/Path/") == "https://example.com/path"
