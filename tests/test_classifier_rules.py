"""Reglas puras del classifier (sin DB)."""

from __future__ import annotations

import pytest

from memex.classifier.rules import TIER_BATCH, TIER_BLACKLIST, classify


def test_list_id_is_blacklist() -> None:
    r = classify({"subject": "promo", "list_id": "<news.example.com>"})
    assert r.tier == TIER_BLACKLIST
    assert r.metadata["rule"] == "list_id"


def test_list_unsubscribe_is_blacklist() -> None:
    assert classify({"list_unsubscribe": "<mailto:unsub@x.com>"}).tier == TIER_BLACKLIST


@pytest.mark.parametrize("prec", ["bulk", "list", "junk", "Bulk"])
def test_bulk_precedence_is_blacklist(prec: str) -> None:
    assert classify({"precedence": prec}).tier == TIER_BLACKLIST


def test_auto_submitted_generated_is_blacklist() -> None:
    assert classify({"auto_submitted": "auto-generated"}).tier == TIER_BLACKLIST


def test_auto_submitted_no_is_batch() -> None:
    assert classify({"auto_submitted": "no"}).tier == TIER_BATCH


def test_plain_personal_email_is_batch() -> None:
    r = classify({"from": {"email": "a@b.com"}, "subject": "hola", "body_text": "qué tal"})
    assert r.tier == TIER_BATCH
    assert r.metadata["rule"] == "default"


def test_chat_payload_is_batch() -> None:
    # un payload de telegram no trae marcadores de bulk → default
    assert classify({"chat_id": -100, "text": "hola grupo"}).tier == TIER_BATCH


def test_empty_markers_are_not_blacklist() -> None:
    assert classify({"list_id": "  ", "precedence": ""}).tier == TIER_BATCH
