"""Auditoría de reglas del gate (memex.relevance.audit): muestreo, contadores, revisión, reparación.

Reusa los helpers y el LLM falso de test_relevance_gate (misma DB sembrada, sin red).
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.relevance.audit import (
    agrees,
    confirm_rule,
    list_rule_audits,
    needs_review,
    pick_samples,
    rejudge_rule,
    run_rule_audit,
)
from memex.relevance.gate import run_relevance_gate
from memex.relevance.rules import RuleDecision
from memex.relevance.settings import upsert_settings
from tests.test_relevance_gate import FakeGateLLM, _seed_msg, _seed_source, _verdict_rows


def _rule(sender_value: str, *, effect: str = "block", kind: str = "sender_email") -> int:
    with connection() as c:
        rid = c.execute(
            text(
                """
                INSERT INTO relevance_gate_rules
                    (user_id, status, proposed_by, effect, sender_kind, sender_value)
                VALUES (1, 'active', 'manual', :effect, :kind, :value) RETURNING id
                """
            ),
            {"effect": effect, "kind": kind, "value": sender_value},
        ).scalar_one()
    return int(rid)


def _rule_row(rid: int) -> dict[str, Any]:
    with connection() as c:
        row = (
            c.execute(
                text(
                    "SELECT status, hit_count, audit_count, contradictions, last_confirmed_at, "
                    "reviewed_at FROM relevance_gate_rules WHERE id = :id"
                ),
                {"id": rid},
            )
            .mappings()
            .one()
        )
    return dict(row)


def _enable(every: int) -> None:
    with connection() as c:
        upsert_settings(c, 1, enabled=True, mode="per_window", audit_sample_every=every)


# ----------------------------------------------------------------------------- puro


def test_agrees_semantics() -> None:
    assert agrees("block", "not_relevant") is True
    assert agrees("block", "relevant") is False
    assert agrees("allow", "relevant") is True
    assert agrees("allow", "not_relevant") is False
    assert agrees("block", "insufficient") is None


def test_pick_samples_first_hit_then_every_nth() -> None:
    ds = [RuleDecision(i, "block", 7) for i in range(1, 26)]  # 25 hits de la regla 7
    picked = pick_samples(ds, {7: 25}, every=10)
    assert [d.inbox_id for d in picked] == [1, 11, 21]  # el 1.º siempre, luego cada 10
    # con hits previos: hit_count pasa de 30 a 33 → secuencias 31,32,33 → solo la 31
    picked = pick_samples(ds[:3], {7: 33}, every=10)
    assert [d.inbox_id for d in picked] == [1]
    assert pick_samples(ds, {7: 25}, every=0) == []
    assert len(pick_samples(ds, {7: 25}, every=1)) == 25


def test_needs_review_thresholds() -> None:
    assert needs_review({"contradictions": 0, "audit_count": 5}) is False
    assert needs_review({"contradictions": 1, "audit_count": 1}) is True  # 1 de 1: señal temprana
    assert needs_review({"contradictions": 1, "audit_count": 8}) is False  # 1 de 8: ruido
    assert needs_review({"contradictions": 2, "audit_count": 8}) is True
    # una regla apagada no pide revisión aunque arrastre contradicciones
    assert needs_review({"contradictions": 5, "audit_count": 5, "status": "disabled"}) is False


# ----------------------------------------------------------------------------- en línea


def test_gate_audits_first_rule_hit_and_flags_contradiction() -> None:
    """Regla block sobre un remitente; el juez (falso) dice «relevante» → el veredicto sigue siendo
    el de la regla, pero queda la segunda opinión, la regla suma 1 contradicción y pide revisión."""
    _enable(every=10)
    sid = _seed_source("audit-src")
    rid = _rule("news@kronnect.com")
    a = _seed_msg(sid, "k1", sender="news@kronnect.com", subject="Player32 is open", minute=1)
    b = _seed_msg(sid, "k2", sender="news@kronnect.com", subject="Weekly drop", minute=2)
    llm = FakeGateLLM(default_verdict="relevant")

    stats = asyncio.run(run_relevance_gate(1, source_id=sid, client=llm))

    assert _verdict_rows()[a] == ("not_relevant", "rule")
    assert _verdict_rows()[b] == ("not_relevant", "rule")
    assert stats.by_rule == 2
    assert stats.audited == 1 and stats.audit_contradictions == 1  # solo el 1.º hit (N=10)
    row = _rule_row(rid)
    assert (row["hit_count"], row["audit_count"], row["contradictions"]) == (2, 1, 1)
    assert needs_review(row) is True
    with connection() as c:
        audits = list_rule_audits(c, 1, rid)
    assert len(audits) == 1
    assert (audits[0]["inbox_id"], audits[0]["agrees"], audits[0]["judge_verdict"]) == (
        a,
        False,
        "relevant",
    )
    assert audits[0]["subject"] == "Player32 is open"


def test_gate_audit_agreement_confirms_rule() -> None:
    _enable(every=1)
    sid = _seed_source("audit-ok")
    rid = _rule("promo@steam.com")
    _seed_msg(sid, "s1", sender="promo@steam.com")
    asyncio.run(
        run_relevance_gate(1, source_id=sid, client=FakeGateLLM(default_verdict="not_relevant"))
    )
    row = _rule_row(rid)
    assert (row["audit_count"], row["contradictions"]) == (1, 0)
    assert row["last_confirmed_at"] is not None
    assert needs_review(row) is False


def test_gate_audit_off_when_sample_every_is_zero() -> None:
    _enable(every=0)
    sid = _seed_source("audit-off")
    rid = _rule("promo@steam.com")
    _seed_msg(sid, "s1", sender="promo@steam.com")
    llm = FakeGateLLM(default_verdict="relevant")
    stats = asyncio.run(run_relevance_gate(1, source_id=sid, client=llm))
    assert stats.by_rule == 1 and stats.audited == 0
    assert llm.gate_calls == 0  # nada fue al juez
    assert _rule_row(rid)["audit_count"] == 0


# ----------------------------------------------------------------------------- retroactiva


def test_run_rule_audit_samples_decided_messages_per_rule() -> None:
    _enable(every=0)  # sin auditoría en línea: los veredictos por regla quedan sin opinión
    sid = _seed_source("retro")
    rid = _rule("promo@steam.com")
    for i in range(4):
        _seed_msg(sid, f"r{i}", sender="promo@steam.com", minute=i)
    asyncio.run(run_relevance_gate(1, source_id=sid, client=FakeGateLLM()))
    assert _rule_row(rid)["audit_count"] == 0

    stats = asyncio.run(
        run_rule_audit(1, per_rule=2, client=FakeGateLLM(default_verdict="relevant"))
    )
    assert (stats.rules, stats.audited, stats.contradictions) == (1, 2, 2)
    assert _rule_row(rid)["contradictions"] == 2
    # segunda corrida: no repite los ya auditados; toma los 2 restantes
    stats2 = asyncio.run(
        run_rule_audit(1, per_rule=2, client=FakeGateLLM(default_verdict="not_relevant"))
    )
    assert (stats2.audited, stats2.agreements) == (2, 2)
    stats3 = asyncio.run(run_rule_audit(1, per_rule=2, client=FakeGateLLM()))
    assert stats3.audited == 0


# ----------------------------------------------------------------------------- revisión


def test_confirm_resets_contradictions_and_rejudge_repairs() -> None:
    _enable(every=1)
    sid = _seed_source("repair")
    rid = _rule("news@kronnect.com")
    a = _seed_msg(sid, "k1", sender="news@kronnect.com", minute=1)
    b = _seed_msg(sid, "k2", sender="news@kronnect.com", minute=2)
    asyncio.run(
        run_relevance_gate(1, source_id=sid, client=FakeGateLLM(default_verdict="relevant"))
    )
    assert _rule_row(rid)["contradictions"] == 2

    with connection() as c:
        row = confirm_rule(c, 1, rid)
    assert row is not None and row["contradictions"] == 0 and row["reviewed_at"] is not None
    assert needs_review(row) is False

    res = asyncio.run(rejudge_rule(1, rid, client=FakeGateLLM(default_verdict="relevant")))
    assert res is not None
    assert res["rule"]["status"] == "disabled"
    assert res["targets"] == 2
    assert res["gate"]["relevant"] == 2 and res["gate"]["by_rule"] == 0
    verdicts = _verdict_rows()
    assert verdicts[a] == ("relevant", "llm") and verdicts[b] == ("relevant", "llm")


def test_api_rules_review_filter_and_audits(client: Any) -> None:
    _enable(every=1)
    sid = _seed_source("api-audit")
    rid = _rule("news@kronnect.com")
    _seed_msg(sid, "k1", sender="news@kronnect.com")
    asyncio.run(
        run_relevance_gate(1, source_id=sid, client=FakeGateLLM(default_verdict="relevant"))
    )

    flagged = client.get("/relevance/rules?review=true").json()["items"]
    assert [r["id"] for r in flagged] == [rid]
    assert flagged[0]["needs_review"] is True and flagged[0]["contradictions"] == 1

    audits = client.get(f"/relevance/rules/{rid}/audits").json()["items"]
    assert len(audits) == 1 and audits[0]["agrees"] is False

    r = client.post(f"/relevance/rules/{rid}/confirm")
    assert r.status_code == 200 and r.json()["needs_review"] is False
    assert client.get("/relevance/rules?review=true").json()["items"] == []
    assert client.post("/relevance/rules/999999/confirm").status_code == 404

    s = client.get("/relevance/settings").json()
    assert s["audit_sample_every"] == 1
    assert (
        client.patch("/relevance/settings", json={"audit_sample_every": 0}).json()[
            "audit_sample_every"
        ]
        == 0
    )


# ------------------------------------------------- la contradicción tiene consecuencia YA


def _contradict(rule_id: int, *, contradictions: int = 1, audits: int = 1) -> None:
    """Deja la regla como la dejaría el auditor tras contradecirla (sin correr el juez)."""
    with connection() as c:
        c.execute(
            text(
                "UPDATE relevance_gate_rules SET contradictions = :c, audit_count = :a "
                "WHERE id = :id"
            ),
            {"c": contradictions, "a": audits, "id": rule_id},
        )


def test_la_regla_contradicha_deja_de_cortar_y_el_correo_va_al_juez() -> None:
    """El caso real: el boletín de la Salesiana avisaba un cambio de calendario académico y la
    regla del remitente lo descartaba igual, DESPUÉS de que el auditor dijera que era relevante."""
    sid = _seed_source()
    rid = _rule("boletin@u.edu")
    iid = _seed_msg(sid, "b1", sender="boletin@u.edu", subject="cambio de calendario")
    _contradict(rid)
    _enable(0)  # sin muestreo: el juez que corre es el del gate, no el auditor
    llm = FakeGateLLM({iid: "relevant"})
    stats = asyncio.run(run_relevance_gate(1, inbox_ids=[iid], force=True, client=llm))
    assert stats.by_rule == 0, "la regla contradicha no decide sola"
    assert _verdict_rows()[iid] == ("relevant", "llm")


def test_una_sola_contradiccion_alcanza_aunque_no_pida_revision() -> None:
    """La regla de la Salesiana tenía 1 contradicción en 3 auditorías: con el umbral de
    `needs_review` (2, o 1 muy temprana) habría seguido vetando."""
    sid = _seed_source()
    rid = _rule("boletin3@u.edu")
    iid = _seed_msg(sid, "b3", sender="boletin3@u.edu", subject="plazo de inscripcion")
    _contradict(rid, contradictions=1, audits=3)
    assert needs_review(_rule_row(rid) | {"status": "active"}) is False
    _enable(0)
    stats = asyncio.run(
        run_relevance_gate(1, inbox_ids=[iid], force=True, client=FakeGateLLM({iid: "relevant"}))
    )
    assert stats.by_rule == 0 and _verdict_rows()[iid] == ("relevant", "llm")


def test_la_regla_sin_contradicciones_sigue_cortando_sin_juez() -> None:
    """Lo no relevante sigue siendo no relevante, y gratis: las reglas sanas no cambian."""
    sid = _seed_source()
    rid = _rule("noreply@ruido.com")
    iid = _seed_msg(sid, "r1", sender="noreply@ruido.com", subject="promo")
    _enable(0)
    llm = FakeGateLLM()
    stats = asyncio.run(run_relevance_gate(1, inbox_ids=[iid], force=True, client=llm))
    assert stats.by_rule == 1 and llm.gate_calls == 0
    assert _verdict_rows()[iid] == ("not_relevant", "rule")
    with connection() as c:
        assert (
            int(
                c.execute(
                    text("SELECT rule_id FROM relevance_verdicts WHERE inbox_id = :i"), {"i": iid}
                ).scalar_one()
            )
            == rid
        )


def test_confirmar_la_regla_le_devuelve_el_veto() -> None:
    """El dueño mira, dice «igual no me sirve» y la regla vuelve a cortar sola."""
    sid = _seed_source()
    rid = _rule("boletin2@u.edu")
    _contradict(rid)
    _enable(0)
    primero = _seed_msg(sid, "c1", sender="boletin2@u.edu", subject="uno")
    stats = asyncio.run(
        run_relevance_gate(1, inbox_ids=[primero], force=True, client=FakeGateLLM())
    )
    assert stats.by_rule == 0
    with connection() as c:
        confirm_rule(c, rid, 1)
    segundo = _seed_msg(sid, "c2", sender="boletin2@u.edu", subject="dos")
    stats = asyncio.run(
        run_relevance_gate(1, inbox_ids=[segundo], force=True, client=FakeGateLLM())
    )
    assert stats.by_rule == 1, "confirmada, vuelve a decidir sola"


def _rule_compuesta(domain: str, pattern: str, *, effect: str = "block") -> int:
    """Regla dominio + patrón: la forma que mina el LLM, y la que se superpone con la de
    remitente."""
    with connection() as c:
        return int(
            c.execute(
                text(
                    """
                    INSERT INTO relevance_gate_rules
                        (user_id, status, proposed_by, effect, sender_kind, sender_value,
                         pattern, match_field)
                    VALUES (1, 'active', 'llm', :effect, 'sender_domain', :dom, :pat, 'subject')
                    RETURNING id
                    """
                ),
                {"effect": effect, "dom": domain, "pat": pattern},
            ).scalar_one()
        )


def test_la_sospecha_es_por_correo_no_por_regla() -> None:
    """El caso real: al boletín de la Salesiana lo atrapaban DOS reglas (el remitente y el dominio
    con patrón). Suspender la primera solo le pasaba la decisión a la segunda y el correo se
    descartaba igual."""
    sid = _seed_source()
    por_remitente = _rule("boletin@u.edu.co")
    _rule_compuesta("u.edu.co", "boletin semanal")
    iid = _seed_msg(
        sid, "d1", sender="boletin@u.edu.co", subject="boletin semanal: cambio de fechas"
    )
    _contradict(por_remitente)  # solo la PRIMERA está contradicha
    _enable(0)
    stats = asyncio.run(
        run_relevance_gate(1, inbox_ids=[iid], force=True, client=FakeGateLLM({iid: "relevant"}))
    )
    assert stats.by_rule == 0, "la segunda regla no puede tomar la decision de la suspendida"
    assert _verdict_rows()[iid] == ("relevant", "llm")


def test_dos_reglas_sanas_siguen_cortando_gratis() -> None:
    """Superponerse no es el problema; el problema es superponerse con una contradicha."""
    sid = _seed_source()
    _rule("otro@ruido.com")
    _rule_compuesta("ruido.com", "promo")
    iid = _seed_msg(sid, "d2", sender="otro@ruido.com", subject="promo de la semana")
    _enable(0)
    llm = FakeGateLLM()
    stats = asyncio.run(run_relevance_gate(1, inbox_ids=[iid], force=True, client=llm))
    assert stats.by_rule == 1 and llm.gate_calls == 0
    assert _verdict_rows()[iid] == ("not_relevant", "rule")
