"""Resolución determinista: email/dominio/handle/nombre/alias → conocido; prioridad; unresolved."""

from __future__ import annotations

from memex.modules.identidades.resolve import KnownIndex, KnownOrg, KnownPerson
from memex.modules.identidades.schema import IdentityItem

PERSONS = [
    KnownPerson(id=1, display_name="Ada Lovelace", emails=("ada@x.com",), handles=("@ada",)),
    KnownPerson(id=2, display_name="Alan Turing"),
]
ORGS = [
    KnownOrg(id=10, name="Unity", aliases=("Unity Technologies",), domains=("unity.com",)),
    KnownOrg(id=11, name="Claude", aliases=("claude.ai",)),
]


def _item(**kw: object) -> IdentityItem:
    data: dict[str, object] = {"source_inbox_ids": (1,), "name": ""}
    data.update(kw)
    return IdentityItem.model_validate(data)


def _idx() -> KnownIndex:
    return KnownIndex(PERSONS, ORGS)


def test_email_resolves_person() -> None:
    r = _idx().resolve(_item(name="alguien", email="ADA@x.com"))  # case-insensitive
    assert (r.kind, r.person_id, r.method) == ("person", 1, "email")


def test_email_domain_resolves_org() -> None:
    r = _idx().resolve(_item(name="Soporte", email="info@unity.com"))
    assert (r.kind, r.org_id, r.method) == ("org", 10, "domain")


def test_handle_resolves_person() -> None:
    r = _idx().resolve(_item(name="x", handle="ada"))  # sin @ también matchea
    assert (r.kind, r.person_id, r.method) == ("person", 1, "handle")


def test_exact_name_resolves_person() -> None:
    r = _idx().resolve(_item(name="ada  lovelace"))  # normalize colapsa espacios
    assert (r.kind, r.person_id, r.method) == ("person", 1, "exact_name")


def test_name_resolves_org() -> None:
    r = _idx().resolve(_item(name="unity"))
    assert (r.kind, r.org_id, r.method) == ("org", 10, "exact_name")


def test_alias_resolves_org() -> None:
    r = _idx().resolve(_item(name="claude.ai"))
    assert (r.kind, r.org_id, r.method) == ("org", 11, "alias")


def test_unresolved() -> None:
    r = _idx().resolve(_item(name="Desconocido SA", email="x@nope.com"))
    assert (r.kind, r.person_id, r.org_id, r.method) == (None, None, None, "unresolved")


def test_email_beats_name() -> None:
    # El email (señal fuerte) gana aunque el nombre apunte a una org distinta.
    r = _idx().resolve(_item(name="unity", email="ada@x.com"))
    assert (r.kind, r.person_id, r.method) == ("person", 1, "email")


def test_empty_index_is_unresolved() -> None:
    assert (
        KnownIndex().resolve(_item(name="Ada Lovelace", email="ada@x.com")).method == "unresolved"
    )
