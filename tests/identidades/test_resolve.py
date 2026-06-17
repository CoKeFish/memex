"""`KnownIndex.resolve` (v2): señales fuertes deterministas sobre el directorio unificado, en orden
de prioridad (email → dominio → handle-por-plataforma → nombre → alias). El remitente del mensaje NO
es señal. Puro/sin DB."""

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


def test_sender_is_not_a_resolution_signal() -> None:
    # una mención cuyo nombre/identificadores no están en el directorio queda SIN resolver. El
    # remitente del mensaje ya NO se usa: que el correo venga de Ada (ada@x.com) no implica que esta
    # mención sea Ada. Antes esto se colapsaba erróneamente al remitente (bug Nequi→Tigo).
    res = _idx().resolve(_probe(name="alguien"))
    assert res.method == "unresolved"


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


def test_persona_con_correo_corporativo_no_colapsa_en_la_org() -> None:
    # una mención PERSONA con correo de un dominio que pertenece a una org NO se resuelve a la org:
    # un dominio NO identifica a una persona (su afiliación se teje aparte). Cae al match por
    # nombre, que acá no existe → unresolved (la persona la crea/afilia el remitente, no este).
    res = _idx().resolve(_probe(name="Juan Pérez", kind="persona", email="juan.perez@unity.com"))
    assert res.method == "unresolved"


def test_exact_name_kind_scoped_y_cross_kind_unico() -> None:
    # homónimos de distinto kind NO se colapsan: el match exacto por nombre está acotado al kind de
    # la mención. Con kind DESCONOCIDO resuelve cross-kind solo si el nombre es ÚNICO.
    idx = KnownIndex(
        [
            KnownIdentity(id=1, kind="persona", display_name="Claude"),
            KnownIdentity(id=2, kind="producto", display_name="Claude"),
        ]
    )
    assert idx.resolve(_probe(name="Claude", kind="producto")).identity_id == 2
    assert idx.resolve(_probe(name="Claude", kind="persona")).identity_id == 1
    # kind desconocido + homónimo de dos kinds → no se arriesga
    assert idx.resolve(_probe(name="Claude")).method == "unresolved"
    # pero un nombre ÚNICO sí resuelve cross-kind (caso finance: counterparty "Nequi" sin kind)
    idx2 = KnownIndex([KnownIdentity(id=9, kind="organizacion", display_name="Nequi")])
    res = idx2.resolve(_probe(name="Nequi"))
    assert (res.kind, res.identity_id, res.method) == ("organizacion", 9, "exact_name")


def test_producto_mention_resuelve_a_org_homonima() -> None:
    # org y producto comparten grupo de match: una mención PRODUCTO "Steam" resuelve a la ORG
    # homónima existente (Steam/Claude son org Y producto = la misma entidad), no crea un duplicado.
    idx = KnownIndex([KnownIdentity(id=5, kind="organizacion", display_name="Steam")])
    res = idx.resolve(_probe(name="Steam", kind="producto"))
    assert (res.kind, res.identity_id, res.method) == ("organizacion", 5, "exact_name")
