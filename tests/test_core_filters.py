"""Tests del applier de filter_rules — evaluator + apply + decide.

No requiere DB — usa FilterRule construido inline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from memex.core.filters import FilterRule, apply, decide, evaluate
from memex.core.source import SourceRecord


def _rule(
    *,
    id: int = 1,
    scope: dict[str, Any] | None = None,
    action: Literal["keep", "ignore", "archive"] = "ignore",
    priority: int = 100,
    source_type: str | None = "imap",
    source_id: int | None = None,
    enabled: bool = True,
) -> FilterRule:
    return FilterRule(
        id=id,
        user_id=1,
        source_type=source_type,
        source_id=source_id,
        scope=scope or {},
        action=action,
        priority=priority,
        enabled=enabled,
    )


def _rec(eid: str, payload: dict[str, Any]) -> SourceRecord:
    return SourceRecord(
        external_id=eid,
        occurred_at=datetime(2026, 5, 28, 0, 0, tzinfo=UTC),
        payload=payload,
        dedupe_keys=[],
    )


# ---------- evaluate / operators ---------- #


def test_equals_matches_exact_value() -> None:
    r = _rule(scope={"from": {"equals": "spam@x.com"}})
    assert evaluate(r, {"from": "spam@x.com"}) is True
    assert evaluate(r, {"from": "other@x.com"}) is False
    assert evaluate(r, {}) is False


def test_in_matches_membership_in_list() -> None:
    r = _rule(scope={"chat_id": {"in": [-100, -200]}})
    assert evaluate(r, {"chat_id": -100}) is True
    assert evaluate(r, {"chat_id": -300}) is False


def test_regex_searches_string_value() -> None:
    r = _rule(scope={"sender_name": {"regex": "^bot:"}})
    assert evaluate(r, {"sender_name": "bot: alice"}) is True
    assert evaluate(r, {"sender_name": "alice bot:"}) is False
    assert evaluate(r, {"sender_name": 123}) is False  # non-string


def test_prefix_matches_string_prefix() -> None:
    r = _rule(scope={"subject": {"prefix": "[NEWSLETTER]"}})
    assert evaluate(r, {"subject": "[NEWSLETTER] daily"}) is True
    assert evaluate(r, {"subject": "re: [NEWSLETTER]"}) is False


def test_multiple_keys_in_scope_use_and_semantics() -> None:
    r = _rule(scope={"from": {"equals": "a@x"}, "subject": {"prefix": "[X]"}})
    assert evaluate(r, {"from": "a@x", "subject": "[X] hi"}) is True
    assert evaluate(r, {"from": "a@x", "subject": "hi"}) is False
    assert evaluate(r, {"from": "b@x", "subject": "[X] hi"}) is False


def test_empty_scope_matches_anything() -> None:
    """Useful for blanket 'drop everything from this source' rules."""
    r = _rule(scope={})
    assert evaluate(r, {"anything": "goes"}) is True
    assert evaluate(r, {}) is True


def test_unknown_operator_does_not_match() -> None:
    """Defensive: unknown op spec returns False instead of raising."""
    r = _rule(scope={"foo": {"weird_op": "x"}})
    assert evaluate(r, {"foo": "x"}) is False


def test_malformed_op_spec_does_not_match() -> None:
    r = _rule(scope={"foo": "not-a-dict"})
    assert evaluate(r, {"foo": "anything"}) is False


# ---------- decide (priority order) ---------- #


def test_decide_returns_first_match_in_iteration_order() -> None:
    # load_active_rules returns sorted by priority DESC already.
    rules = [
        _rule(id=10, priority=200, scope={"from": {"equals": "a@x"}}, action="keep"),
        _rule(id=11, priority=100, scope={"from": {"equals": "a@x"}}, action="ignore"),
    ]
    matched = decide(rules, {"from": "a@x"})
    assert matched is not None
    assert matched.id == 10  # higher priority wins


def test_decide_returns_none_when_no_rule_matches() -> None:
    rules = [_rule(scope={"from": {"equals": "a@x"}})]
    assert decide(rules, {"from": "b@x"}) is None


# ---------- apply (drop counts + structlog) ---------- #


def test_apply_drops_records_matching_ignore_rule() -> None:
    rule = _rule(id=42, scope={"from": {"equals": "spam@x"}}, action="ignore")
    records = [
        _rec("e1", {"from": "spam@x", "subject": "junk"}),
        _rec("e2", {"from": "ok@x", "subject": "fine"}),
        _rec("e3", {"from": "spam@x", "subject": "more junk"}),
    ]
    kept, drops = apply(records, [rule])
    assert [r.external_id for r in kept] == ["e2"]
    assert drops == {42: 2}


def test_apply_keeps_records_with_keep_rule_or_no_match() -> None:
    rule_keep = _rule(id=1, scope={"from": {"equals": "vip@x"}}, action="keep", priority=300)
    rule_drop = _rule(id=2, scope={"from": {"equals": "vip@x"}}, action="ignore", priority=100)
    records = [_rec("e1", {"from": "vip@x"}), _rec("e2", {"from": "other@x"})]
    kept, drops = apply(records, [rule_keep, rule_drop])
    assert [r.external_id for r in kept] == ["e1", "e2"]
    assert drops == {}


def test_apply_treats_archive_as_keep_for_now() -> None:
    """`archive` is reserved for a future feature; for Fase 1 it passes through."""
    rule = _rule(scope={"from": {"equals": "x@y"}}, action="archive")
    records = [_rec("e1", {"from": "x@y"})]
    kept, drops = apply(records, [rule])
    assert len(kept) == 1
    assert drops == {}


def test_apply_with_no_rules_keeps_everything() -> None:
    records = [_rec("e1", {"a": 1}), _rec("e2", {"b": 2})]
    kept, drops = apply(records, [])
    assert len(kept) == 2
    assert drops == {}
