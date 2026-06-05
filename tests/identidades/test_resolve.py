"""`KnownIndex.resolve` (v2): señales fuertes deterministas sobre el directorio unificado, en orden
de prioridad (remitente → email → dominio → handle-por-plataforma → nombre → alias). Puro/sin DB."""

from __future__ import annotations

from typing import Any

from memex.modules.identidades.resolve import KnownIdentifier, KnownIdentity, KnownIndex
from memex.modules.identidades.schema import IdentityItem


def _idx() -> KnownIndex:
    return KnownIndex(
        [
            KnownIdentity(
                id=1,
                kind="persona",
                display_name="Ada Lovelace",
                identifiers=(
                    KnownIdentifier("email", "email", "ada@x.com"),
                    KnownIdentifier("x", "handle", "ada"),
                ),
            ),
            KnownIdentity(
                id=2,
                kind="persona",
                display_name="Ana López",
                identifiers=(KnownIdentifier("instagram", "handle", "ada"),),
            ),
            KnownIdentity(
                id=10,
                kind="organizacion",
                display_name="Unity",
                aliases=("Unity3D",),
                identifiers=(KnownIdentifier("domain", "domain", "unity.com"),),
            ),
        ]
    )


def _probe(**kw: Any) -> IdentityItem:
    return IdentityItem(source_inbox_ids=(1,), **kw)


def test_email_resolves_person() -> None:
    res = _idx().resolve(_probe(name="quien", email="ADA@x.com"))
    assert (res.kind, res.identity_id, res.method) == ("persona", 1, "email")


def test_email_domain_resolves_org() -> None:
    res = _idx().resolve(_probe(name="soporte", email="info@unity.com"))
    assert (res.kind, res.identity_id, res.method) == ("organizacion", 10, "domain")


def test_sender_email_is_strong_signal() -> None:
    # la mención no trae email, pero el remitente del mensaje sí → resuelve por remitente
    res = _idx().resolve(_probe(name="alguien"), sender_email="ada@x.com")
    assert (res.kind, res.identity_id, res.method) == ("persona", 1, "sender_email")


def test_handle_scoped_by_platform() -> None:
    idx = _idx()
    # el mismo handle 'ada' existe en X (id 1) y en Instagram (id 2) → la plataforma desambigua
    res_x = idx.resolve(_probe(name="x", handle="@ada"), source_platform="x")
    assert (res_x.identity_id, res_x.method) == (1, "handle")
    res_ig = idx.resolve(_probe(name="x", handle="ada"), source_platform="instagram")
    assert res_ig.identity_id == 2


def test_handle_ambiguous_without_platform_is_unresolved() -> None:
    # sin plataforma, un handle compartido por dos identidades NO se resuelve (evita cruzar)
    res = _idx().resolve(_probe(name="desconocido total", handle="ada"))
    assert res.method == "unresolved"


def test_exact_name_and_alias() -> None:
    idx = _idx()
    assert idx.resolve(_probe(name="unity")).method == "exact_name"
    assert idx.resolve(_probe(name="UNITY3D")).method == "alias"  # alias normalizado
    res = idx.resolve(_probe(name="ada  lovelace"))  # doble espacio + casing
    assert (res.kind, res.identity_id, res.method) == ("persona", 1, "exact_name")


def test_email_beats_name() -> None:
    # señal fuerte (email) gana sobre el nombre, aunque el nombre apunte a otra identidad
    res = _idx().resolve(_probe(name="unity", email="ada@x.com"))
    assert (res.kind, res.identity_id, res.method) == ("persona", 1, "email")


def test_unresolved() -> None:
    res = _idx().resolve(_probe(name="Nadie Conocido", email="x@nope.com"))
    assert (res.kind, res.identity_id, res.method) == (None, None, "unresolved")
