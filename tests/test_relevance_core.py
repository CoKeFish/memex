"""Núcleo determinista del gate de relevancia: settings, intereses, reglas + dry run, y el
filtro que aplican los worksets de summarize/extract. Sin LLM (todo determinista, DB sembrada)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memex.core.relevance_marks import set_mark
from memex.db import connection
from memex.modules.finance.module import FinanceModule
from memex.modules.workset import load_module_workset
from memex.relevance import (
    EMAIL_TYPES,
    VerdictItem,
    apply_active_rules,
    clear_verdicts,
    create_interest,
    create_rule,
    delete_interest,
    dry_run_rule,
    get_settings,
    insert_verdicts,
    list_interests,
    list_review_queue,
    list_rules,
    load_gate_workset,
    match_rule,
    resolve_insufficient,
    set_rule_status,
    update_interest,
    upsert_settings,
)
from memex.relevance.rules import EmailFields, extract_email_fields
from memex.summarizer.worker import _load_workset as load_summarize_workset


def _seed_source(name: str = "mail", source_type: str = "imap") -> int:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id"),
            {"n": name, "t": source_type},
        ).scalar()
    assert sid is not None
    return int(sid)


def _seed_msg(
    source_id: int,
    ext: str,
    *,
    tier: str = "batch",
    sender: str = "promo@steam.com",
    subject: str = "Oferta",
    body: str = "cuerpo",
    list_id: str | None = None,
    minute: int = 0,
) -> int:
    payload: dict[str, Any] = {
        "from": {"email": sender},
        "subject": subject,
        "body_text": body,
    }
    if list_id is not None:
        payload["list_id"] = list_id
    with connection() as c:
        iid = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (1, :sid, :eid, :occ, CAST(:p AS JSONB)) RETURNING id
                """
            ),
            {
                "sid": source_id,
                "eid": ext,
                "occ": datetime(2026, 6, 1, 12, minute, tzinfo=UTC),
                "p": json.dumps(payload),
            },
        ).scalar()
        c.execute(
            text("INSERT INTO classifications (user_id, inbox_id, tier) VALUES (1, :iid, :tier)"),
            {"iid": iid, "tier": tier},
        )
    assert iid is not None
    return int(iid)


def _verdict(inbox_id: int, verdict: str, *, method: str = "llm") -> None:
    with connection() as c:
        insert_verdicts(c, 1, [VerdictItem(inbox_id=inbox_id, verdict=verdict, method=method)])


def _enable_gate() -> None:
    with connection() as c:
        upsert_settings(c, 1, enabled=True)


# ---------------------------------------------------------------- settings + intereses


def test_settings_default_off_and_partial_upsert() -> None:
    with connection() as c:
        s = get_settings(c, 1)
        assert (s.enabled, s.mode, s.model) == (False, "per_window", "claude-opus-4-8")
        assert s.mining_min_messages == 5
        upsert_settings(c, 1, enabled=True)
        upsert_settings(c, 1, mode="per_message")  # parcial: no toca enabled
        upsert_settings(c, 1, mining_min_messages=3)  # parcial: no toca mode
        s = get_settings(c, 1)
        assert (s.enabled, s.mode, s.mining_min_messages) == (True, "per_message", 3)
        with pytest.raises(ValueError):
            upsert_settings(c, 1, mode="invalido")
        with pytest.raises(ValueError):
            upsert_settings(c, 1, mining_min_messages=0)


def test_interests_crud_and_duplicate() -> None:
    with connection() as c:
        row = create_interest(c, 1, "  descuentos de Steam  ")
        assert row["text"] == "descuentos de Steam"
        assert row["enabled"] is True
        updated = update_interest(c, int(row["id"]), 1, enabled=False)
        assert updated is not None and updated["enabled"] is False
        assert [r["text"] for r in list_interests(c, 1)] == ["descuentos de Steam"]
        assert list_interests(c, 1, enabled_only=True) == []
        with pytest.raises(ValueError):
            create_interest(c, 1, "   ")
    with connection() as c, pytest.raises(IntegrityError):
        create_interest(c, 1, "descuentos de Steam")
    with connection() as c:
        assert delete_interest(c, int(row["id"]), 1) is True
        assert delete_interest(c, int(row["id"]), 1) is False


# ---------------------------------------------------------------- matcheo determinista


def test_extract_email_fields_normalizes() -> None:
    f = extract_email_fields(
        {"from": {"email": "Promo@Steam.COM"}, "subject": "GRAN Oferta", "list_id": "L.steam"}
    )
    assert f == EmailFields(
        sender_email="promo@steam.com",
        sender_domain="steam.com",
        subject="GRAN Oferta",
        list_id="l.steam",
    )
    empty = extract_email_fields({})
    assert empty == EmailFields("", "", "", "")


@pytest.mark.parametrize(
    ("kind", "pattern", "expected"),
    [
        ("sender_email", "promo@steam.com", True),
        ("sender_email", "otro@steam.com", False),
        ("sender_domain", "STEAM.com", True),
        ("sender_domain", "valve.com", False),
        ("subject_contains", "oferta", True),
        ("subject_contains", "factura", False),
        ("list_id", "l.steam", True),
        ("list_id", "l.otro", False),
    ],
)
def test_match_rule_per_kind(kind: str, pattern: str, expected: bool) -> None:
    fields = EmailFields("promo@steam.com", "steam.com", "Gran OFERTA de verano", "l.steam")
    assert match_rule(kind, pattern, fields) is expected


def test_apply_active_rules_first_match_wins_and_ignores_disabled() -> None:
    sid = _seed_source()
    iid = _seed_msg(sid, "m1", sender="promo@steam.com", subject="Oferta")
    rows = load_gate_workset(1)
    assert [r.inbox_id for r in rows] == [iid]
    with connection() as c:
        from memex.relevance.rules import dry_run_rule as dr

        r1 = create_rule(
            c,
            1,
            kind="sender_domain",
            pattern="steam.com",
            proposed_by="manual",
            report=dr(c, 1, "sender_domain", "steam.com"),
        )
        r2 = create_rule(
            c,
            1,
            kind="subject_contains",
            pattern="oferta",
            proposed_by="manual",
            report=dr(c, 1, "subject_contains", "oferta"),
        )
        assert r1 is not None and r2 is not None
        matched = apply_active_rules(c, 1, rows)
        assert matched == {iid: int(r1["id"])}  # la más vieja primero
        assert set_rule_status(c, int(r1["id"]), 1, "disabled") is not None
        matched = apply_active_rules(c, 1, rows)
        assert matched == {iid: int(r2["id"])}


# ---------------------------------------------------------------- dry run + ciclo de reglas


def test_dry_run_rejects_rule_that_catches_relevant_mail() -> None:
    sid = _seed_source()
    relevant = _seed_msg(sid, "m1", sender="promo@steam.com", subject="Oferta wishlist")
    noise = _seed_msg(sid, "m2", sender="promo@steam.com", subject="Oferta basura", minute=1)
    _verdict(relevant, "relevant")
    _verdict(noise, "not_relevant")
    pending = _seed_msg(sid, "m3", sender="promo@steam.com", subject="Otra", minute=2)
    with connection() as c:
        report = dry_run_rule(c, 1, "sender_domain", "steam.com")
    assert report.matched == 3
    assert report.matched_relevant == 1
    assert report.matched_not_relevant == 1
    assert report.matched_unverdicted == 1
    assert report.relevant_sample_ids == (relevant,)
    assert report.passes is False
    assert pending not in report.relevant_sample_ids


def test_dry_run_manual_mark_wins_over_verdict() -> None:
    sid = _seed_source()
    iid = _seed_msg(sid, "m1", sender="promo@steam.com")
    _verdict(iid, "not_relevant")
    with connection() as c:
        set_mark(c, user_id=1, inbox_id=iid, is_relevant=True)
        report = dry_run_rule(c, 1, "sender_email", "promo@steam.com")
    assert report.matched_relevant == 1  # la mark manual TRUE pisa el veredicto del gate
    assert report.passes is False


def test_create_rule_activates_or_rejects_and_skips_duplicates() -> None:
    sid = _seed_source()
    relevant = _seed_msg(sid, "m1", sender="humano@uni.edu", subject="Notas")
    _verdict(relevant, "relevant")
    with connection() as c:
        bad = create_rule(
            c,
            1,
            kind="sender_domain",
            pattern="uni.edu",
            proposed_by="llm",
            report=dry_run_rule(c, 1, "sender_domain", "uni.edu"),
            rationale="ruido",
        )
        good = create_rule(
            c,
            1,
            kind="sender_domain",
            pattern="spam.io",
            proposed_by="llm",
            report=dry_run_rule(c, 1, "sender_domain", "spam.io"),
        )
        dup = create_rule(
            c,
            1,
            kind="sender_domain",
            pattern="spam.io",
            proposed_by="manual",
            report=dry_run_rule(c, 1, "sender_domain", "spam.io"),
        )
    assert bad is not None and bad["status"] == "rejected"
    assert bad["dry_run_report"]["passes"] is False
    assert bad["activated_at"] is None
    assert good is not None and good["status"] == "active"
    assert good["activated_at"] is not None
    assert dup is None
    with connection() as c:
        assert {r["status"] for r in list_rules(c, 1)} == {"rejected", "active"}
        assert [r["pattern"] for r in list_rules(c, 1, status="active")] == ["spam.io"]


def test_rejected_rule_cannot_be_activated() -> None:
    sid = _seed_source()
    relevant = _seed_msg(sid, "m1", sender="humano@uni.edu")
    _verdict(relevant, "relevant")
    with connection() as c:
        bad = create_rule(
            c,
            1,
            kind="sender_email",
            pattern="humano@uni.edu",
            proposed_by="llm",
            report=dry_run_rule(c, 1, "sender_email", "humano@uni.edu"),
        )
        assert bad is not None
        assert set_rule_status(c, int(bad["id"]), 1, "active") is None
        with pytest.raises(ValueError):
            set_rule_status(c, int(bad["id"]), 1, "rejected")


def test_dry_run_subject_contains_escapes_like_metachars() -> None:
    sid = _seed_source()
    _seed_msg(sid, "m1", subject="descuento 100% real")
    _seed_msg(sid, "m2", subject="sin porcentaje", minute=1)
    with connection() as c:
        report = dry_run_rule(c, 1, "subject_contains", "100% real")
    assert report.matched == 1  # el % es literal, no comodín


# ---------------------------------------------------------------- veredictos + cola manual


def test_insert_verdicts_idempotent_and_clear_keeps_manual() -> None:
    sid = _seed_source()
    a = _seed_msg(sid, "m1")
    b = _seed_msg(sid, "m2", minute=1)
    with connection() as c:
        n = insert_verdicts(
            c,
            1,
            [
                VerdictItem(a, "not_relevant", "llm", model="claude-opus-4-8", mode="per_window"),
                VerdictItem(b, "relevant", "manual"),
            ],
        )
        assert n == 2
        # Re-insert: no pisa (ON CONFLICT DO NOTHING)
        assert insert_verdicts(c, 1, [VerdictItem(a, "relevant", "llm")]) == 0
        assert clear_verdicts(c, 1, [a, b]) == 1  # keep_manual conserva el de b
        rows = c.execute(text("SELECT inbox_id FROM relevance_verdicts")).scalars().all()
        assert list(rows) == [b]


def test_resolve_insufficient_writes_mark_and_verdict() -> None:
    sid = _seed_source()
    iid = _seed_msg(sid, "m1", subject="¿impuestos?")
    _verdict(iid, "insufficient")
    with connection() as c:
        queue = list_review_queue(c, 1)
        assert [q["inbox_id"] for q in queue] == [iid]
        assert queue[0]["subject"] == "¿impuestos?"
        ok = resolve_insufficient(c, user_id=1, inbox_id=iid, is_relevant=True, reason="es banco")
        assert ok is True
        assert list_review_queue(c, 1) == []
        row = c.execute(
            text("SELECT verdict, method FROM relevance_verdicts WHERE inbox_id = :i"),
            {"i": iid},
        ).first()
        assert row is not None and (row[0], row[1]) == ("relevant", "manual")
        mark = c.execute(
            text("SELECT is_relevant FROM relevance_marks WHERE inbox_id = :i"), {"i": iid}
        ).scalar()
        assert mark is True
        # Sin veredicto insufficient → nada que resolver
        assert resolve_insufficient(c, user_id=1, inbox_id=iid, is_relevant=False) is False


# ---------------------------------------------------------------- filtros de worksets


def _summarize_ids() -> set[int]:
    return {r.inbox_id for r in load_summarize_workset(1, None, None, 100)}


def _extract_ids() -> set[int]:
    with connection() as c:
        rows = load_module_workset(c, 1, source_id=None, modules=[FinanceModule()], limit=100)
    return {r.inbox_id for r in rows}


def test_gate_off_worksets_unchanged() -> None:
    sid = _seed_source()
    iid = _seed_msg(sid, "m1")
    assert iid in _summarize_ids()
    assert iid in _extract_ids()


def test_gate_on_filters_both_worksets_by_verdict() -> None:
    _enable_gate()
    sid = _seed_source()
    pending = _seed_msg(sid, "m1")
    relevant = _seed_msg(sid, "m2", minute=1)
    irrelevant = _seed_msg(sid, "m3", minute=2)
    unsure = _seed_msg(sid, "m4", minute=3)
    _verdict(relevant, "relevant")
    _verdict(irrelevant, "not_relevant")
    _verdict(unsure, "insufficient")

    for ids in (_summarize_ids(), _extract_ids()):
        assert relevant in ids
        assert pending not in ids  # pendiente-de-gate = bloqueado
        assert irrelevant not in ids
        assert unsure not in ids  # insufficient espera la revisión manual


def test_gate_on_manual_mark_wins_both_ways() -> None:
    _enable_gate()
    sid = _seed_source()
    rescued = _seed_msg(sid, "m1")
    blocked = _seed_msg(sid, "m2", minute=1)
    _verdict(rescued, "not_relevant")
    _verdict(blocked, "relevant")
    with connection() as c:
        set_mark(c, user_id=1, inbox_id=rescued, is_relevant=True)
        set_mark(c, user_id=1, inbox_id=blocked, is_relevant=False)

    for ids in (_summarize_ids(), _extract_ids()):
        assert rescued in ids
        assert blocked not in ids


def test_gate_on_only_applies_to_email_kinds() -> None:
    _enable_gate()
    chat_sid = _seed_source("tg", "telegram")
    chat_msg = _seed_msg(chat_sid, "c1", sender="", subject="")
    assert "telegram" not in EMAIL_TYPES
    assert chat_msg in _summarize_ids()
    assert chat_msg in _extract_ids()


# ---------------------------------------------------------------- workset del gate


def test_load_gate_workset_only_pending_emails() -> None:
    sid = _seed_source()
    chat_sid = _seed_source("tg", "telegram")
    pending = _seed_msg(sid, "m1")
    judged = _seed_msg(sid, "m2", minute=1)
    marked = _seed_msg(sid, "m3", minute=2)
    blacklisted = _seed_msg(sid, "m4", tier="blacklist", minute=3)
    chat_msg = _seed_msg(chat_sid, "c1", minute=4)
    _verdict(judged, "relevant")
    with connection() as c:
        set_mark(c, user_id=1, inbox_id=marked, is_relevant=False)

    ids = {r.inbox_id for r in load_gate_workset(1)}
    assert pending in ids
    assert judged not in ids
    assert marked not in ids
    assert blacklisted not in ids
    assert chat_msg not in ids

    scoped = {r.inbox_id for r in load_gate_workset(1, inbox_ids=[judged])}
    assert scoped == set()  # acotado a un set explícito conserva los filtros
