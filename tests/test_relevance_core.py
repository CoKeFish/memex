"""Núcleo determinista del gate de relevancia: settings, intereses, reglas + dry run, y el
filtro que aplican los worksets de summarize/extract. Sin LLM (todo determinista, DB sembrada)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memex.core.relevance_marks import set_mark
from memex.db import connection
from memex.modules.finance.module import FinanceModule
from memex.modules.workset import load_module_workset
from memex.relations.summary import _load_workset as load_summarize_workset
from memex.relevance import (
    EMAIL_TYPES,
    GateSettings,
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
    resolve_insufficient,
    set_rule_status,
    update_interest,
    upsert_settings,
)
from memex.relevance.rules import (
    EmailFields,
    extract_email_fields,
    match_pattern,
    match_sender,
    rule_matches,
    validate_pattern,
)


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


def _mk_rule(
    c: Any,
    *,
    effect: str = "block",
    sender_kind: str | None = None,
    sender_value: str | None = None,
    pattern: str | None = None,
    match_field: str | None = None,
    proposed_by: str = "manual",
    rationale: str = "",
) -> dict[str, Any] | None:
    """Helper: corre el dry run y crea la regla compuesta sobre los MISMOS predicados."""
    report = dry_run_rule(
        c,
        1,
        effect=effect,
        sender_kind=sender_kind,
        sender_value=sender_value,
        pattern=pattern,
        match_field=match_field,
    )
    return create_rule(
        c,
        1,
        effect=effect,
        sender_kind=sender_kind,
        sender_value=sender_value,
        pattern=pattern,
        match_field=match_field,
        proposed_by=proposed_by,
        report=report,
        rationale=rationale,
    )


# ---------------------------------------------------------------- settings + intereses


def test_settings_default_off_and_partial_upsert() -> None:
    with connection() as c:
        s = get_settings(c, 1)
        assert (s.enabled, s.mode, s.model) == (False, "per_window", "claude-opus-4-8")
        assert s.mining_min_messages == 3  # disparador por cantidad, default bajo y configurable
        assert (s.provider, s.codex_model) == ("anthropic", None)
        # Sistema unificado: minería intercalada ON por default; umbral del lazo de intereses.
        assert (s.mining_interleave, s.interest_suggest_min_marks) == (True, 5)
        upsert_settings(c, 1, enabled=True)
        upsert_settings(c, 1, mode="per_message")  # parcial: no toca enabled
        upsert_settings(c, 1, mining_min_messages=4)  # parcial: no toca mode
        s = get_settings(c, 1)
        assert (s.enabled, s.mode, s.mining_min_messages) == (True, "per_message", 4)
        upsert_settings(c, 1, provider="codex", codex_model="gpt-5.1-codex")
        s = get_settings(c, 1)
        assert (s.provider, s.codex_model) == ("codex", "gpt-5.1-codex")
        upsert_settings(c, 1, codex_model="")  # "" limpia el override
        assert get_settings(c, 1).codex_model is None
        upsert_settings(c, 1, mining_interleave=False)  # parcial: no toca el resto
        upsert_settings(c, 1, interest_suggest_min_marks=2)
        s = get_settings(c, 1)
        assert (s.mining_interleave, s.interest_suggest_min_marks) == (False, 2)
        assert s.provider == "codex"  # el upsert parcial conservó lo previo
        upsert_settings(c, 1, provider="deepseek")  # deepseek = proveedor de primera clase
        assert get_settings(c, 1).provider == "deepseek"
        with pytest.raises(ValueError):
            upsert_settings(c, 1, mode="invalido")
        with pytest.raises(ValueError):
            upsert_settings(c, 1, mining_min_messages=0)
        with pytest.raises(ValueError):
            upsert_settings(c, 1, provider="openai")
        with pytest.raises(ValueError):
            upsert_settings(c, 1, interest_suggest_min_marks=0)


def test_complete_model_gates_to_anthropic() -> None:
    # `model` (claude-opus-*) solo viaja al path Anthropic; codex/deepseek lo IGNORAN (None =
    # default del cliente). Es lo que hace al proveedor intercambiable sin reescribir `model`.
    assert GateSettings(provider="anthropic", model="claude-opus-4-8").complete_model == (
        "claude-opus-4-8"
    )
    assert GateSettings(provider="codex", model="claude-opus-4-8").complete_model is None
    assert GateSettings(provider="deepseek", model="claude-opus-4-8").complete_model is None


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
        {
            "from": {"email": "Promo@Steam.COM"},
            "subject": "GRAN Oferta",
            "list_id": "L.steam",
            "body_text": "Cuerpo  CON   espacios\nY salto",
        }
    )
    assert f == EmailFields(
        sender_email="promo@steam.com",
        sender_domain="steam.com",
        list_id="l.steam",
        subject="gran oferta",  # normalizado: 1 línea + minúscula
        body="cuerpo con espacios y salto",  # invisibles fuera, ws colapsado, minúscula
    )
    empty = extract_email_fields({})
    assert empty == EmailFields("", "", "", "", "")


@pytest.mark.parametrize(
    ("sender_kind", "sender_value", "expected"),
    [
        ("sender_email", "promo@steam.com", True),
        ("sender_email", "otro@steam.com", False),
        ("sender_domain", "STEAM.com", True),
        ("sender_domain", "valve.com", False),
        ("list_id", "l.steam", True),
        ("list_id", "l.otro", False),
    ],
)
def test_match_sender_per_kind(sender_kind: str, sender_value: str, expected: bool) -> None:
    fields = EmailFields("promo@steam.com", "steam.com", "l.steam", "gran oferta de verano", "")
    assert match_sender(sender_kind, sender_value, fields) is expected


def test_match_pattern_regex_over_field() -> None:
    fields = EmailFields(
        "promo@steam.com", "steam.com", "l.steam", "gran oferta de verano", "cuerpo del correo"
    )
    assert match_pattern(re.compile("oferta", re.ASCII), "subject", fields) is True
    assert match_pattern(re.compile("factura", re.ASCII), "subject", fields) is False
    assert match_pattern(re.compile("cuerpo", re.ASCII), "body", fields) is True
    assert match_pattern(re.compile("cuerpo", re.ASCII), "subject", fields) is False
    assert match_pattern(re.compile("oferta|cuerpo", re.ASCII), "subject_or_body", fields) is True


def test_rule_matches_composite_is_and_of_predicates() -> None:
    fields = EmailFields("prof@uni.edu", "uni.edu", "", "notas del parcial", "")
    # remitente + patrón: matchea solo si los DOS coinciden
    assert rule_matches("sender_domain", "uni.edu", "notas", "subject", fields) is True
    assert rule_matches("sender_domain", "uni.edu", "oferta", "subject", fields) is False
    assert rule_matches("sender_domain", "otra.edu", "notas", "subject", fields) is False
    # un solo predicado: matchea por ese solo
    assert rule_matches("sender_domain", "uni.edu", None, None, fields) is True
    assert rule_matches(None, None, "notas", "subject", fields) is True
    # sin predicados → False (defensivo: una regla vacía no matchea todo)
    assert rule_matches(None, None, None, None, fields) is False


def test_apply_active_rules_block_first_match_wins_and_ignores_disabled() -> None:
    sid = _seed_source()
    iid = _seed_msg(sid, "m1", sender="promo@steam.com", subject="Oferta")
    rows = load_gate_workset(1)
    assert [r.inbox_id for r in rows] == [iid]
    with connection() as c:
        r1 = _mk_rule(c, sender_kind="sender_domain", sender_value="steam.com")
        r2 = _mk_rule(c, pattern="oferta", match_field="subject")
        assert r1 is not None and r2 is not None
        app = apply_active_rules(c, 1, rows)
        assert app.conflicts == []
        assert [(d.inbox_id, d.effect, d.rule_id) for d in app.decisions] == [
            (iid, "block", int(r1["id"]))  # la más vieja primero
        ]
        assert set_rule_status(c, int(r1["id"]), 1, "disabled") is not None
        app = apply_active_rules(c, 1, rows)
        assert [d.rule_id for d in app.decisions] == [int(r2["id"])]


def test_apply_active_rules_allow_marks_relevant() -> None:
    sid = _seed_source()
    iid = _seed_msg(sid, "m1", sender="prof@uni.edu", subject="Notas del parcial")
    rows = load_gate_workset(1)
    with connection() as c:
        rule = _mk_rule(
            c,
            effect="allow",
            sender_kind="sender_domain",
            sender_value="uni.edu",
            pattern="notas",
            match_field="subject",
        )
        assert rule is not None and rule["status"] == "active"
        app = apply_active_rules(c, 1, rows)
        assert app.conflicts == []
        assert [(d.inbox_id, d.effect, d.rule_id) for d in app.decisions] == [
            (iid, "allow", int(rule["id"]))
        ]


def test_apply_active_rules_conflict_goes_to_judge() -> None:
    sid = _seed_source()
    iid = _seed_msg(sid, "m1", sender="prof@uni.edu", subject="Oferta y notas")
    rows = load_gate_workset(1)
    with connection() as c:
        block = _mk_rule(c, effect="block", pattern="oferta", match_field="subject")
        allow = _mk_rule(c, effect="allow", sender_kind="sender_domain", sender_value="uni.edu")
        assert block is not None and allow is not None
        app = apply_active_rules(c, 1, rows)
        # matchea AMBAS polaridades → no se cortocircuita (sin decisión), cae al juez
        assert app.decisions == []
        assert len(app.conflicts) == 1
        conflict = app.conflicts[0]
        assert (conflict.inbox_id, conflict.block_rule_id, conflict.allow_rule_id) == (
            iid,
            int(block["id"]),
            int(allow["id"]),
        )


# ---------------------------------------------------------------- dry run + ciclo de reglas


def test_dry_run_block_rejects_rule_that_catches_relevant_mail() -> None:
    sid = _seed_source()
    relevant = _seed_msg(sid, "m1", sender="promo@steam.com", subject="Oferta wishlist")
    noise = _seed_msg(sid, "m2", sender="promo@steam.com", subject="Oferta basura", minute=1)
    _verdict(relevant, "relevant")
    _verdict(noise, "not_relevant")
    pending = _seed_msg(sid, "m3", sender="promo@steam.com", subject="Otra", minute=2)
    with connection() as c:
        report = dry_run_rule(
            c, 1, effect="block", sender_kind="sender_domain", sender_value="steam.com"
        )
    assert report.matched == 3
    assert report.matched_relevant == 1
    assert report.matched_not_relevant == 1
    assert report.matched_unverdicted == 1
    assert report.relevant_sample_ids == (relevant,)
    assert report.contaminating_sample_ids == (relevant,)  # block: el lado malo es el relevante
    assert report.passes is False
    assert pending not in report.relevant_sample_ids


def test_dry_run_allow_passes_only_when_no_not_relevant() -> None:
    # La precisión la da el patrón: el remitente solo es mixto (1 relevant + 1 not_relevant), pero
    # la regla allow compuesta (remitente + asunto) carva solo el subconjunto relevante → pasa.
    sid = _seed_source()
    good = _seed_msg(sid, "m1", sender="prof@uni.edu", subject="Notas del parcial")
    bad = _seed_msg(sid, "m2", sender="prof@uni.edu", subject="Promo del bazar", minute=1)
    _verdict(good, "relevant")
    _verdict(bad, "not_relevant")
    with connection() as c:
        coarse = dry_run_rule(  # solo-remitente: atrapa el not_relevant → rechazada
            c, 1, effect="allow", sender_kind="sender_domain", sender_value="uni.edu"
        )
        precise = dry_run_rule(  # compuesta (remitente + patrón): solo el relevante → pasa
            c,
            1,
            effect="allow",
            sender_kind="sender_domain",
            sender_value="uni.edu",
            pattern="notas",
            match_field="subject",
        )
    assert coarse.matched_not_relevant == 1
    assert coarse.contaminating_sample_ids == (bad,)  # allow: el lado malo es el no-relevante
    assert coarse.passes is False
    assert precise.matched == 1 and precise.matched_not_relevant == 0
    assert precise.passes is True


def test_dry_run_manual_mark_wins_over_verdict() -> None:
    sid = _seed_source()
    iid = _seed_msg(sid, "m1", sender="promo@steam.com")
    _verdict(iid, "not_relevant")
    with connection() as c:
        set_mark(c, user_id=1, inbox_id=iid, is_relevant=True)
        report = dry_run_rule(
            c, 1, effect="block", sender_kind="sender_email", sender_value="promo@steam.com"
        )
    assert report.matched_relevant == 1  # la mark manual TRUE pisa el veredicto del gate
    assert report.passes is False


def test_create_rule_activates_or_rejects_and_skips_duplicates() -> None:
    sid = _seed_source()
    relevant = _seed_msg(sid, "m1", sender="humano@uni.edu", subject="Notas")
    _verdict(relevant, "relevant")
    with connection() as c:
        bad = _mk_rule(
            c,
            sender_kind="sender_domain",
            sender_value="uni.edu",
            proposed_by="llm",
            rationale="ruido",
        )
        good = _mk_rule(c, sender_kind="sender_domain", sender_value="spam.io", proposed_by="llm")
        dup = _mk_rule(c, sender_kind="sender_domain", sender_value="spam.io", proposed_by="manual")
    assert bad is not None and bad["status"] == "rejected"
    assert bad["dry_run_report"]["passes"] is False
    assert bad["activated_at"] is None
    assert good is not None and good["status"] == "active"
    assert good["activated_at"] is not None
    assert dup is None
    with connection() as c:
        assert {r["status"] for r in list_rules(c, 1)} == {"rejected", "active"}
        active = list_rules(c, 1, status="active")
        assert [(r["effect"], r["sender_value"]) for r in active] == [("block", "spam.io")]


def test_block_and_allow_same_predicates_coexist() -> None:
    # El dedup es por (user, effect, predicados): la MISMA firma vale en las dos polaridades.
    with connection() as c:
        b = _mk_rule(
            c,
            effect="block",
            sender_kind="sender_domain",
            sender_value="x.com",
            pattern="oferta",
            match_field="subject",
        )
        a = _mk_rule(
            c,
            effect="allow",
            sender_kind="sender_domain",
            sender_value="x.com",
            pattern="oferta",
            match_field="subject",
        )
        assert b is not None and a is not None
        assert {r["effect"] for r in list_rules(c, 1)} == {"block", "allow"}
        assert [r["effect"] for r in list_rules(c, 1, effect="allow")] == ["allow"]


def test_rejected_rule_cannot_be_activated() -> None:
    sid = _seed_source()
    relevant = _seed_msg(sid, "m1", sender="humano@uni.edu")
    _verdict(relevant, "relevant")
    with connection() as c:
        bad = _mk_rule(
            c, sender_kind="sender_email", sender_value="humano@uni.edu", proposed_by="llm"
        )
        assert bad is not None
        assert set_rule_status(c, int(bad["id"]), 1, "active") is None
        with pytest.raises(ValueError):
            set_rule_status(c, int(bad["id"]), 1, "rejected")


def test_create_rule_rejects_invalid_predicates() -> None:
    with connection() as c:
        with pytest.raises(ValueError):  # sin ningún predicado
            dry_run_rule(c, 1, effect="block")
        with pytest.raises(ValueError):  # remitente sin valor
            dry_run_rule(c, 1, effect="allow", sender_kind="sender_domain")


def test_dry_run_regex_anchored_kills_off_official_footgun() -> None:
    # El footgun original: substring 'off' matcheaba 'official'. Un regex anclado con límites
    # explícitos (\b está prohibido por divergente) matchea 'off' como palabra, NO 'official'.
    sid = _seed_source()
    _seed_msg(sid, "m1", subject="20% off hoy")
    _seed_msg(sid, "m2", subject="official release v2", minute=1)
    with connection() as c:
        report = dry_run_rule(
            c, 1, effect="block", pattern=r"(^|[^a-z])off([^a-z]|$)", match_field="subject"
        )
    assert report.matched == 1  # solo el promo; 'official' NO matchea


#: Batería de paridad: (pattern, match_field, subject, body, expected). El MISMO regex DEBE matchear
#: idéntico en Python `re` (runtime) y en Postgres `~` (dry run) — esa igualdad es la garantía del
#: rediseño (una divergencia reintroduce el footgun: el dry run pasa pero el runtime difiere).
_PARITY_CASES = [
    (r"^re: \[.+/.+\]", "subject", "Re: [louthy/language-ext] Bug #42", "x", True),
    (r"^re: \[.+/.+\]", "subject", "Re: language-ext sin corchetes", "x", False),
    (r"(^|[^a-z])off([^a-z]|$)", "subject", "20% off hoy", "x", True),
    (r"(^|[^a-z])off([^a-z]|$)", "subject", "official release v2", "x", False),
    # acentos: el patrón en minúscula con ó matchea el asunto en MAYÚSCULA en AMBOS motores
    # (str.lower() ≡ lower() en latín; sin plegado del motor, que divergiría).
    ("inscripción", "subject", "INSCRIPCIÓN ABIERTA 2026", "x", True),
    (r"pedido \d{4}", "subject", "Pedido 2026 confirmado", "x", True),  # \d ASCII en ambos
    # cuerpo: footer recurrente; la normalización colapsa saltos/espacios igual en Py y SQL
    (
        "you are receiving this because",
        "body",
        "Asunto cualquiera",
        "Hola\n\nYou  are   receiving\nthis because you subscribed",
        True,
    ),
    (
        "you are receiving this because",
        "subject",
        "Asunto cualquiera",
        "Hola\n\nYou  are   receiving\nthis because you subscribed",
        False,
    ),
    ("suscripción", "subject_or_body", "Asunto X", "gestioná tu suscripción acá", True),
    # soft hyphen (U+00AD) dentro de la palabra: se quita en ambos lados → matchea
    ("suscripción", "body", "Asunto X", "sus­cripción activa", True),
]


@pytest.mark.parametrize(("pattern", "match_field", "subject", "body", "expected"), _PARITY_CASES)
def test_dialect_parity_python_eq_postgres(
    pattern: str, match_field: str, subject: str, body: str, expected: bool
) -> None:
    sid = _seed_source()
    _seed_msg(sid, "m1", subject=subject, body=body)
    fields = extract_email_fields({"subject": subject, "body_text": body})
    py = rule_matches(None, None, pattern, match_field, fields)
    with connection() as c:
        report = dry_run_rule(c, 1, effect="block", pattern=pattern, match_field=match_field)
    pg = report.matched == 1
    assert py == pg, f"divergencia Py={py} != PG={pg} para {pattern!r}/{match_field}"
    assert py is expected


@pytest.mark.parametrize(
    "pattern",
    [
        "oferta",
        r"^re: \[.+/.+\]",
        r"(^|[^a-z])off([^a-z]|$)",
        "inscripción",
        r"pedido \d{4}",
        "(?:ab)+",  # grupo cuantificado SIN cuantificador interno → seguro
        "a{2,5}",  # repetición acotada
    ],
)
def test_validate_pattern_accepts_safe_dialect(pattern: str) -> None:
    assert validate_pattern(pattern) == pattern.strip()


@pytest.mark.parametrize(
    "pattern",
    [
        "",  # vacío
        "a" * 300,  # supera el cap de longitud
        r"\boff\b",  # límite de palabra (diverge entre motores)
        r"fact\w+",  # \w (diverge locale/unicode)
        "(?=secret)",  # lookahead
        r"(a)\1",  # backreference
        "Oferta",  # mayúscula literal (el haystack se compara en minúscula)
        ".*",  # matchea la cadena vacía → matchearía todos los correos
        "(a+)+",  # cuantificador anidado no acotado → ReDoS
    ],
)
def test_validate_pattern_rejects_unsafe(pattern: str) -> None:
    with pytest.raises(ValueError):
        validate_pattern(pattern)


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


def test_blacklist_excluded_off_but_rescued_on() -> None:
    """El tier deja de ser pre-filtro: apagado excluye blacklist (cost-safe, como hoy);
    encendido lo decide la relevancia → un bulk rescatado (veredicto relevant) entra y se
    procesa (ventaneado como batch por `plan_windows`)."""
    sid = _seed_source()
    bulk = _seed_msg(sid, "b1", tier="blacklist")
    # Gate APAGADO (default): el bulk NO entra a los worksets (corte barato por cabeceras).
    assert bulk not in _summarize_ids()
    assert bulk not in _extract_ids()
    # Gate ENCENDIDO + veredicto relevant (rescatado): el bulk SÍ entra.
    _enable_gate()
    _verdict(bulk, "relevant")
    assert bulk in _summarize_ids()
    assert bulk in _extract_ids()


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
    # El gate JUZGA el bulk también: ser blacklist es una señal, no un veredicto de relevancia.
    assert blacklisted in ids
    assert chat_msg not in ids

    scoped = {r.inbox_id for r in load_gate_workset(1, inbox_ids=[judged])}
    assert scoped == set()  # acotado a un set explícito conserva los filtros
