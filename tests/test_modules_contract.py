"""Tests puros del contrato de módulos: parse_items + validate_item (sin DB ni LLM)."""

from __future__ import annotations

from memex.modules.contract import parse_items, validate_item
from memex.modules.finance.schema import ExpenseItem

_VALID = {
    "source_inbox_ids": [1],
    "amount": "4500.00",
    "currency": "ARS",
    "merchant": "Edenor",
    "occurred_on": "2026-05-20",
    "description": "luz",
    "evidence": "pagué los $4.500 de la luz",
}


# ----- parse_items --------------------------------------------------------------- #


def test_parse_items_happy() -> None:
    out = parse_items('{"items": [{"a": 1}, {"b": 2}]}')
    assert out == [{"a": 1}, {"b": 2}]


def test_parse_items_bad_json() -> None:
    assert parse_items("not json {") == []


def test_parse_items_missing_key() -> None:
    assert parse_items('{"expenses": []}') == []


def test_parse_items_not_a_list() -> None:
    assert parse_items('{"items": "nope"}') == []


def test_parse_items_filters_non_dicts() -> None:
    assert parse_items('{"items": [{"a": 1}, "x", 3, null]}') == [{"a": 1}]


# ----- validate_item: schema + atribución ---------------------------------------- #


def test_validate_item_valid_in_lote() -> None:
    item = validate_item(ExpenseItem, dict(_VALID), lote=frozenset({1}))
    assert isinstance(item, ExpenseItem)
    assert str(item.amount) == "4500.00"
    assert item.source_inbox_ids == (1,)


def test_validate_item_id_outside_lote_discarded() -> None:
    assert validate_item(ExpenseItem, dict(_VALID), lote=frozenset({2, 3})) is None


def test_validate_item_empty_attribution_discarded() -> None:
    raw = {**_VALID, "source_inbox_ids": []}
    assert validate_item(ExpenseItem, raw, lote=frozenset({1})) is None


def test_validate_item_missing_required_field_discarded() -> None:
    raw = {k: v for k, v in _VALID.items() if k != "amount"}
    assert validate_item(ExpenseItem, raw, lote=frozenset({1})) is None


def test_validate_item_extra_field_discarded() -> None:
    raw = {**_VALID, "categoria": "gasto"}  # extra="forbid" → item inválido
    assert validate_item(ExpenseItem, raw, lote=frozenset({1})) is None


def test_validate_item_evidence_miss_keeps_item() -> None:
    """Una evidencia que no matchea el texto se loguea pero NO descarta el item."""
    rendered = {1: "Ana: pagué los $4.500 de la luz"}
    raw = {**_VALID, "evidence": "esto no aparece en el texto"}
    item = validate_item(ExpenseItem, raw, lote=frozenset({1}), rendered_by_id=rendered)
    assert isinstance(item, ExpenseItem)


def test_validate_item_evidence_hit_normalized() -> None:
    rendered = {1: "Ana:  pagué   los $4.500 de la LUZ"}
    item = validate_item(ExpenseItem, dict(_VALID), lote=frozenset({1}), rendered_by_id=rendered)
    assert isinstance(item, ExpenseItem)
