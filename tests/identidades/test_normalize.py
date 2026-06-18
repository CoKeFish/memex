"""Paridad Python↔SQL de la normalización (`normalize_match`↔`memex_norm`, `org_core`↔
`memex_org_core`) + `norm_identifier` por kind. La paridad es crítica: el match exacto en memoria
(Python) debe coincidir con los índices/trigram (SQL). Excluye letras especiales NO descomponibles
por NFKD que `unaccent` mapea distinto (ß/ø/æ…), divergencia conocida y documentada."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.modules.identidades.normalize import (
    is_role_email,
    norm_identifier,
    normalize_match,
    org_core,
    registrable_domain,
)

_NAMES = ["Café Ñoño", "  Ada   Lovelace  ", "JOSÉ", "naïve façade", "Müller", "Bogotá D.C."]
_ORGS = [
    "Acme S.A.S.",
    "Unity Technologies",
    "Grupo Bolívar S.A.",
    "OpenAI, Inc.",
    "Ñandú Ltda",
    "Oxford Spa",  # 'spa' ya NO se stripea → cubre la paridad del cambio 0073
    "Aqua Co",
]


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


def test_org_core_no_strips_spa_y_no_colapsa() -> None:
    # 'spa' NO es sufijo legal (0073): conserva su núcleo y NO colapsa orgs distintas — era el bug
    # que disparaba el auto-merge erróneo ('Oxford Spa' == 'Oxford Group' → 'oxford').
    assert org_core("Oxford Spa") == "oxford spa"
    assert org_core("Oxford Spa") != org_core("Oxford Group")
    assert org_core("Aqua Spa") != org_core("Aqua Co")


def test_norm_identifier_email_gmail_y_subaddressing() -> None:
    # Gmail ignora puntos y +tag; googlemail == gmail → dos grafías de la misma casilla colapsan.
    assert norm_identifier("email", "j.doe+promo@gmail.com") == "jdoe@gmail.com"
    assert norm_identifier("email", "jdoe@gmail.com") == "jdoe@gmail.com"
    assert norm_identifier("email", "JDoe@googlemail.com") == "jdoe@gmail.com"
    # +tag (RFC 5233) se quita en TODO dominio; los puntos solo en Gmail.
    assert norm_identifier("email", "juan+work@empresa.com") == "juan@empresa.com"
    assert norm_identifier("email", "juan.perez@empresa.com") == "juan.perez@empresa.com"


def test_norm_identifier_phone_e164() -> None:
    # móvil CO de 10 dígitos (3XX…) → E.164 +57; ya en '+' se respeta; un fijo no se prefija.
    assert norm_identifier("phone", "300 123 4567") == "+573001234567"
    assert norm_identifier("phone", "+57 300 123 4567") == "+573001234567"
    assert norm_identifier("phone", "(601) 234 5678") == "6012345678"


def test_is_role_email() -> None:
    # relay/role: NO son clave de identidad (las comparte mucha gente)
    assert is_role_email("notifications@github.com")
    assert is_role_email("messages-noreply@linkedin.com")
    assert is_role_email("no-reply@x.com")
    assert is_role_email("mailer-daemon@host.com")
    # personales / de org: SÍ identifican
    assert not is_role_email("ada@gmail.com")
    assert not is_role_email("info@acme.com")
    assert not is_role_email("juan.perez@empresa.co")


def test_norm_identifier() -> None:
    assert norm_identifier("email", "  Ada@X.COM ") == "ada@x.com"
    assert norm_identifier("handle", "@AdaL") == "adal"
    assert norm_identifier("domain", "info@Unity.com") == "unity.com"
    assert norm_identifier("phone", "+57 (300) 123-45") == "+5730012345"
    assert norm_identifier("url", "HTTPS://Example.com/Path/") == "https://example.com/path"
    # dominio → registrable (eTLD+1): colapsa subdominios, respeta sufijos multi-etiqueta.
    assert norm_identifier("domain", "noreply@accounts.google.com") == "google.com"
    assert norm_identifier("domain", "x@tm.openai.com") == "openai.com"
    assert norm_identifier("domain", "x@tienda.com.co") == "tienda.com.co"
    assert norm_identifier("domain", "x@sub.tienda.com.co") == "tienda.com.co"


def test_registrable_domain() -> None:
    # colapsa subdominios al dominio que la org posee
    assert registrable_domain("acme.com") == "acme.com"
    assert registrable_domain("mail.acme.com") == "acme.com"
    assert registrable_domain("a.b.c.acme.com") == "acme.com"
    # sufijos multi-etiqueta (la PSL los conoce; un recorte de 2 etiquetas fallaría)
    assert registrable_domain("forums.bbc.co.uk") == "bbc.co.uk"
    assert registrable_domain("otra.com.co") == "otra.com.co"
    # idempotente
    assert registrable_domain(registrable_domain("mail.acme.com")) == "acme.com"
    # fallback: host sin sufijo público / vacío
    assert registrable_domain("localhost") == "localhost"
    assert registrable_domain("") == ""
