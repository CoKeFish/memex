"""Higiene del directorio (`identidades/verify_llm.py`): re-resolución determinista de menciones
sueltas + segunda verificación con juez (LLM falso): reglas, veredictos, aplicación,
idempotencia."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.modules.identidades.verify_llm import (
    flagged_identities,
    parse_verdict,
    reresolve_unresolved,
    run_verify,
)


class FakeJudge:
    """Responde `content` fijo; cuenta llamadas. Cumple el Protocol LLMClient."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.calls += 1
        return LLMResult(
            content=self.content,
            model="fake",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="stop",
        )


def _identity(c: Any, kind: str, name: str, *, email: str | None = None) -> int:
    iid = int(
        c.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name, source) "
                "VALUES (1, :k, :n, 'extraction') RETURNING id"
            ),
            {"k": kind, "n": name},
        ).scalar_one()
    )
    if email:
        c.execute(
            text(
                "INSERT INTO mod_identidades_identifiers "
                "(user_id, identity_id, platform, kind, value, value_norm) "
                "VALUES (1, :i, 'email', 'email', :v, :v)"
            ),
            {"i": iid, "v": email},
        )
    return iid


def _inbox(c: Any, body: str, ext: str = "m1") -> int:
    sid = c.execute(
        text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, 'imap') RETURNING id"),
        {"n": f"src-{ext}"},
    ).scalar_one()
    return int(
        c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :s, :e, NOW(), CAST(:p AS JSONB)) RETURNING id"
            ),
            {"s": sid, "e": ext, "p": json.dumps({"subject": "x", "body_text": body})},
        ).scalar_one()
    )


def _mention(
    c: Any,
    name: str,
    kind: str,
    *,
    inbox_id: int | None = None,
    email: str | None = None,
    resolved: int | None = None,
    method: str = "unresolved",
) -> int:
    return int(
        c.execute(
            text(
                "INSERT INTO mod_identidades_mentions (user_id, source_inbox_ids, evidence, "
                "mentioned_name, mentioned_kind, email, resolved_identity_id, resolution_method) "
                "VALUES (1, CAST(:ids AS BIGINT[]), :ev, :n, :k, :e, :r, :m) RETURNING id"
            ),
            {
                "ids": [inbox_id] if inbox_id else [],
                "ev": f"…{name}…",
                "n": name,
                "k": kind,
                "e": email,
                "r": resolved,
                "m": method,
            },
        ).scalar_one()
    )


def _resolved(c: Any, mention_id: int) -> tuple[int | None, str]:
    row = c.execute(
        text(
            "SELECT resolved_identity_id, resolution_method FROM mod_identidades_mentions "
            "WHERE id = :m"
        ),
        {"m": mention_id},
    ).one()
    return (int(row[0]) if row[0] is not None else None, str(row[1]))


# ----- re-resolución determinista ------------------------------------------------- #


def test_reresolve_attaches_by_email_added_later() -> None:
    # La mención se guardó sin identidad; después la ficha ganó ese email → hoy resuelve.
    with connection() as c:
        owner = _identity(c, "persona", "Danilo Salgado", email="salgado@uni.edu")
        m = _mention(c, "Danilo Bernardino Salgado", "persona", email="salgado@uni.edu")
        stats = reresolve_unresolved(c, 1)
        assert (stats.scanned, stats.resolved) == (1, 1)
        assert stats.by_method == {"email": 1}
        assert _resolved(c, m) == (owner, "email")


def test_reresolve_by_exact_name_respects_kind_group() -> None:
    with connection() as c:
        org = _identity(c, "organizacion", "Sideral")
        by_name = _mention(c, "Sideral", "organizacion")
        wrong_kind = _mention(c, "Sideral", "persona")  # persona ∥ org: no se ata cruzado
        stats = reresolve_unresolved(c, 1)
        assert stats.resolved == 1
        assert _resolved(c, by_name) == (org, "exact_name")
        assert _resolved(c, wrong_kind) == (None, "unresolved")


def test_reresolve_scoped_to_window_and_idempotent() -> None:
    with connection() as c:
        org = _identity(c, "organizacion", "Acme")
        i1 = _inbox(c, "Acme", "a")
        i2 = _inbox(c, "Acme", "b")
        m1 = _mention(c, "Acme", "organizacion", inbox_id=i1)
        m2 = _mention(c, "Acme", "organizacion", inbox_id=i2)
        assert reresolve_unresolved(c, 1, inbox_ids=[i1]).resolved == 1
        assert _resolved(c, m1)[0] == org and _resolved(c, m2)[0] is None
        assert reresolve_unresolved(c, 1).resolved == 1  # la otra
        assert reresolve_unresolved(c, 1).resolved == 0  # nada más que hacer


# ----- reglas de marcado ------------------------------------------------------------- #


def test_rules_flag_single_word_prefix_and_short_org() -> None:
    with connection() as c:
        ant = _identity(c, "persona", "Ant")
        _identity(c, "persona", "Ant Wilson")
        _identity(c, "persona", "Juan Pérez")  # dos palabras: no se marca
        amazon = _identity(c, "organizacion", "Amazon")
        _identity(c, "organizacion", "Amazon Web Services")
        x = _identity(c, "organizacion", "X")
        _identity(c, "organizacion", "IFCX", email="hola@ifcx.org")  # con identificador: no
        cases = flagged_identities(c, 1)
    by_id = {cs.identity_id: cs for cs in cases}
    assert set(by_id) == {ant, amazon, x}
    assert by_id[ant].rule.startswith("A") and by_id[amazon].rule.startswith("B")
    assert by_id[x].rule.startswith("D")
    assert [cnd["name"] for cnd in by_id[ant].candidates] == ["Ant Wilson"]


# ----- parseo del juez --------------------------------------------------------------- #


def test_parse_verdict_validates_target_and_fallbacks() -> None:
    v = parse_verdict('{"verdict": "attach", "target_id": 7, "confidence": 0.9}', {7})
    assert (v.verdict, v.target_id, v.confidence) == ("attach", 7, 0.9)
    assert parse_verdict('{"verdict": "attach", "target_id": 99}', {7}).verdict == "separate"
    assert parse_verdict("basura", set()).verdict == "separate"
    assert parse_verdict('{"verdict": "drop", "confidence": 2}', set()).confidence == 1.0


# ----- aplicación de veredictos ------------------------------------------------------ #


def _attach(target: int) -> str:
    return json.dumps(
        {"verdict": "attach", "target_id": target, "confidence": 0.95, "rationale": "misma firma"}
    )


def test_attach_merges_into_target_and_marks_verified() -> None:
    with connection() as c:
        ant = _identity(c, "persona", "Ant")
        wilson = _identity(c, "persona", "Ant Wilson")
        i = _inbox(c, "Thanks, Ant")
        m = _mention(c, "Ant", "persona", inbox_id=i, resolved=ant, method="created")
    judge = FakeJudge(_attach(wilson))
    stats = asyncio.run(run_verify(1, client=judge))
    assert (stats.cases, stats.attached, judge.calls) == (1, 1, 1)
    with connection() as c:
        assert _resolved(c, m)[0] == wilson  # la mención re-apuntó al superviviente
        gone = c.execute(text("SELECT 1 FROM mod_identidades WHERE id = :i"), {"i": ant}).first()
        verified = c.execute(
            text("SELECT metadata->>'verified_verdict' FROM mod_identidades WHERE id = :i"),
            {"i": wilson},
        ).scalar()
    assert gone is None and verified == "attach"


def test_drop_removes_identity_and_frees_mentions() -> None:
    with connection() as c:
        cat = _identity(c, "persona", "Catherine")
        i = _inbox(c, "Catherine at Notion: your invoice")
        m = _mention(c, "Catherine", "persona", inbox_id=i, resolved=cat, method="created")
    judge = FakeJudge(
        '{"verdict": "drop", "target_id": null, "confidence": 0.99, "rationale": "automatico"}'
    )
    stats = asyncio.run(run_verify(1, client=judge))
    assert (stats.dropped, stats.cases) == (1, 1)
    with connection() as c:
        assert _resolved(c, m) == (None, "unresolved")
        assert (
            c.execute(text("SELECT 1 FROM mod_identidades WHERE id = :i"), {"i": cat}).first()
            is None
        )


def test_separate_keeps_and_low_confidence_does_not_apply() -> None:
    with connection() as c:
        erik = _identity(c, "persona", "Erik")
        _identity(c, "persona", "Erika Correa")
    judge = FakeJudge(_attach(erik + 1).replace("0.95", "0.4"))  # attach con confianza baja
    stats = asyncio.run(run_verify(1, client=judge))
    assert (stats.kept, stats.attached) == (1, 0)
    with connection() as c:
        row = c.execute(
            text("SELECT metadata->>'verified_at' FROM mod_identidades WHERE id = :i"), {"i": erik}
        ).scalar()
    assert row is not None
    # verificada → ya no se vuelve a marcar (incremental)
    assert asyncio.run(run_verify(1, client=judge)).cases == 0
    assert judge.calls == 1


def test_dry_run_judges_without_writing() -> None:
    with connection() as c:
        cat = _identity(c, "persona", "Catherine")
    judge = FakeJudge('{"verdict": "drop", "confidence": 0.99, "rationale": "x"}')
    stats = asyncio.run(run_verify(1, client=judge, dry_run=True))
    assert (stats.cases, stats.dropped, judge.calls) == (1, 0, 1)
    with connection() as c:
        assert c.execute(text("SELECT 1 FROM mod_identidades WHERE id = :i"), {"i": cat}).first()
        n = c.execute(
            text("SELECT count(*) FROM llm_calls WHERE purpose = 'identidades_verify'")
        ).scalar()
    assert n == 1  # la llamada sí se registra


def test_drop_refused_when_identity_holds_hierarchy() -> None:
    # «Amazon» sin menciones pero madre de «Amazon Web Services»: es un nodo de jerarquía, no
    # una ficha sin evidencia. El juez pide drop; el sistema se niega y la conserva verificada.
    with connection() as c:
        amazon = _identity(c, "organizacion", "Amazon")
        aws = _identity(c, "organizacion", "Amazon Web Services")
        c.execute(
            text("UPDATE mod_identidades SET parent_identity_id = :p WHERE id = :c"),
            {"p": amazon, "c": aws},
        )
    judge = FakeJudge('{"verdict": "drop", "confidence": 1.0, "rationale": "sin menciones"}')
    stats = asyncio.run(run_verify(1, client=judge))
    assert (stats.cases, stats.dropped, stats.kept) == (1, 0, 1)
    with connection() as c:
        parent = c.execute(
            text("SELECT parent_identity_id FROM mod_identidades WHERE id = :i"), {"i": aws}
        ).scalar()
        verified = c.execute(
            text("SELECT metadata->>'verified_at' FROM mod_identidades WHERE id = :i"),
            {"i": amazon},
        ).scalar()
    assert parent == amazon and verified is not None
