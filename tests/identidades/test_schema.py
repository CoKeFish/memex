"""IdentityItem: extra prohibido, kind→unknown, email lowercase, confidence clamp."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memex.modules.contract import ExtractionItem
from memex.modules.identidades.schema import IDENTITY_KINDS, IdentityItem


def test_minimal_valid() -> None:
    it = IdentityItem(source_inbox_ids=(1,), name="Ada")
    assert it.name == "Ada"
    assert it.kind == "unknown"
    assert it.confidence == 0.5
    assert it.email is None


def test_is_extraction_item() -> None:
    assert issubclass(IdentityItem, ExtractionItem)


def test_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        IdentityItem.model_validate({"source_inbox_ids": [1], "name": "Ada", "foo": "bar"})


def test_kind_out_of_list_becomes_unknown() -> None:
    it = IdentityItem.model_validate({"source_inbox_ids": [1], "name": "X", "kind": "empresa"})
    assert it.kind == "unknown"


def test_kinds_sin_agente() -> None:
    assert IDENTITY_KINDS == ("persona", "organizacion", "producto", "unknown")


def test_kind_agente_legado_se_vuelve_producto() -> None:
    # 'agente' salió de la taxonomía (0057); una salida LLM rancia pliega a producto, no a unknown.
    it = IdentityItem.model_validate({"source_inbox_ids": [1], "name": "Claude", "kind": "agente"})
    assert it.kind == "producto"


def test_kind_valid_lowercased() -> None:
    it = IdentityItem.model_validate({"source_inbox_ids": [1], "name": "X", "kind": "Organizacion"})
    assert it.kind == "organizacion"


def test_email_lowercased_and_empty_to_none() -> None:
    lower = IdentityItem.model_validate(
        {"source_inbox_ids": [1], "name": "X", "email": "Ada@X.COM"}
    )
    assert lower.email == "ada@x.com"
    blank = IdentityItem.model_validate({"source_inbox_ids": [1], "name": "X", "email": "  "})
    assert blank.email is None


def test_confidence_clamped() -> None:
    def conf(v: object) -> float:
        return IdentityItem.model_validate(
            {"source_inbox_ids": [1], "name": "X", "confidence": v}
        ).confidence

    assert conf(5) == 1.0
    assert conf(-2) == 0.0
    assert conf("0.7") == 0.7
    assert conf("abc") == 0.5
    assert conf(None) == 0.5
