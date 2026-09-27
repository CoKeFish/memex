"""EntityProfile + validate_profile: validación, normalización y garantía de formato."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from memex.webcontext import (
    EntityProfile,
    WebContextFormatError,
    entity_profile_schema,
    validate_profile,
    validate_profile_data,
)


def test_valid_profile() -> None:
    p = EntityProfile(name="Rappi", kind="organizacion", one_liner="superapp", sector="tech")
    assert p.name == "Rappi"
    assert p.kind == "organizacion"


def test_kind_synonyms_normalized() -> None:
    assert EntityProfile(name="x", kind="empresa").kind == "organizacion"
    assert EntityProfile(name="x", kind="app").kind == "producto"


def test_kind_impossible_raises() -> None:
    with pytest.raises(ValidationError):
        EntityProfile(name="x", kind="persona")


def test_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        EntityProfile(name="x", kind="producto", inventado="z")  # type: ignore[call-arg]


def test_str_tuple_cleaning_and_dedup() -> None:
    p = EntityProfile(
        name="x",
        kind="producto",
        key_facts=["a", " a ", "", "b"],
        sources="http://u",
    )
    assert p.key_facts == ("a", "b")  # strip + dedup + drop vacíos
    assert p.sources == ("http://u",)  # str → tuple de uno


def test_empty_strings_to_none() -> None:
    p = EntityProfile(name="x", kind="producto", sector="  ", country="")
    assert p.sector is None
    assert p.country is None


def test_schema_additional_properties_false() -> None:
    schema = entity_profile_schema()
    assert schema.get("additionalProperties") is False
    assert "name" in schema["properties"]
    assert "sources" in schema["properties"]


def test_validate_profile_reinjects_kind() -> None:
    raw = json.dumps({"name": "Rappi", "kind": "producto", "one_liner": "x"})  # kind "equivocado"
    p = validate_profile(raw, expected_kind="organizacion")
    assert p.kind == "organizacion"  # el caller manda
    assert p.name == "Rappi"


def test_validate_profile_fenced_output() -> None:
    raw = 'Claro, aca va:\n```json\n{"name":"X","kind":"organizacion","one_liner":"y"}\n```\n'
    p = validate_profile(raw, expected_kind="organizacion")
    assert p.name == "X"


def test_validate_profile_garbage_raises() -> None:
    with pytest.raises(WebContextFormatError):
        validate_profile("esto no es json", expected_kind="organizacion")


def test_validate_profile_data_non_dict_raises() -> None:
    with pytest.raises(WebContextFormatError):
        validate_profile_data(["no", "dict"], expected_kind="producto")


def test_validate_profile_data_missing_required_raises() -> None:
    # falta 'name' (requerido) → no valida
    with pytest.raises(WebContextFormatError):
        validate_profile_data({"one_liner": "x"}, expected_kind="producto")
