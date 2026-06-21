"""Resolvedor contextual: parseo defensivo + aplicación (merge/jerarquía/remitente) sin red real."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, text

from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.modules.identidades.resolve_context import EmailIdentity, ResolverInput
from memex.modules.identidades.resolve_llm import (
    Affiliation,
    Merge,
    Parent,
    ResolverDecision,
    SenderDisposition,
    _serialize,
    apply_resolution,
    parse_resolution,
    resolve_email,
)


class FakeLLM:
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


# --- parseo (puro) ----------------------------------------------------------------- #


def test_parse_relations_routes_by_kind() -> None:
    # RELACIONES unificadas, ruteadas por kind: org→org = jerarquía; persona→org = afiliación.
    content = json.dumps(
        {
            "merges": [{"keep_id": 1, "drop_id": 2, "confidence": 0.9}],
            "relations": [
                {"source_id": 3, "target_id": 1, "confidence": 0.8},
                {"source_id": 4, "target_id": 1, "role": "Presidenta", "confidence": 0.85},
            ],
            "sender": {"is_person": False, "owner_id": 1, "confidence": 0.7},
        }
    )
    kinds = {1: "organizacion", 2: "organizacion", 3: "organizacion", 4: "persona"}
    d = parse_resolution(content, {1, 2, 3, 4}, kinds)
    assert d.merges == (Merge(1, 2, 0.9),)
    assert d.parents == (Parent(3, 1, None, 0.8),)  # org→org
    assert d.affiliations == (Affiliation(4, 1, "Presidenta", 0.85),)  # persona→org
    assert d.sender == SenderDisposition(False, 1, None, 0.7)


def test_parse_relations_target_name_for_hierarchy() -> None:
    # org source con target_name (la org padre no está en las listas) → Parent por nombre.
    content = json.dumps(
        {"relations": [{"source_id": 1, "target_name": "Universidad X", "confidence": 0.9}]}
    )
    d = parse_resolution(content, {1}, {1: "organizacion"})
    assert d.parents == (Parent(1, None, "Universidad X", 0.9),)


def test_parse_relations_persona_needs_existing_org() -> None:
    # Afiliación exige org EXISTENTE: persona con solo target_name (sin org id) → se descarta.
    content = json.dumps(
        {"relations": [{"source_id": 1, "target_name": "Acme", "confidence": 0.9}]}
    )
    d = parse_resolution(content, {1}, {1: "persona"})
    assert d.affiliations == () and d.parents == ()


def test_parse_relations_target_must_be_org() -> None:
    # target que no es organización → se descarta (el target siempre es una org).
    content = json.dumps({"relations": [{"source_id": 1, "target_id": 2, "confidence": 0.9}]})
    d = parse_resolution(content, {1, 2}, {1: "persona", 2: "persona"})
    assert d.affiliations == () and d.parents == ()


def test_parse_drops_ids_outside_valid_set() -> None:
    content = json.dumps({"merges": [{"keep_id": 1, "drop_id": 99, "confidence": 1}]})
    assert parse_resolution(content, {1, 2}, {1: "organizacion"}).merges == ()


def test_parse_garbage_is_empty() -> None:
    d = parse_resolution("no soy json", {1}, {1: "persona"})
    assert d.merges == () and d.parents == () and d.sender is None and d.affiliations == ()


# --- aplicación (DB) --------------------------------------------------------------- #


def _source(c: Connection) -> int:
    return int(
        c.execute(
            text("INSERT INTO sources (user_id,name,type) VALUES (1,'m','imap') RETURNING id")
        ).scalar_one()
    )


def _inbox(c: Connection, src: int) -> int:
    return int(
        c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :s, 'e', NOW(), '{}'::jsonb) RETURNING id"
            ),
            {"s": src},
        ).scalar_one()
    )


def _identity(c: Connection, kind: str, name: str) -> int:
    return int(
        c.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name, source) "
                "VALUES (1, :k, :n, 'extraction') RETURNING id"
            ),
            {"k": kind, "n": name},
        ).scalar_one()
    )


def _mention(
    c: Connection, inbox: int, ident: int, kind: str, method: str, email: str | None
) -> None:
    c.execute(
        text(
            "INSERT INTO mod_identidades_mentions "
            "(user_id, source_inbox_ids, mentioned_name, resolved_identity_id, resolved_kind, "
            " resolution_method, email) VALUES (1, ARRAY[:i], 'x', :rid, :rk, :m, :e)"
        ),
        {"i": inbox, "rid": ident, "rk": kind, "m": method, "e": email},
    )


def _ids(c: Connection, identity_id: int) -> set[tuple[str, str]]:
    return {
        (str(r.kind), str(r.value_norm))
        for r in c.execute(
            text("SELECT kind, value_norm FROM mod_identidades_identifiers WHERE identity_id = :i"),
            {"i": identity_id},
        ).all()
    }


def _ei(identity_id: int, kind: str, name: str, **kw: Any) -> EmailIdentity:
    return EmailIdentity(
        identity_id=identity_id,
        kind=kind,
        display_name=name,
        is_sender=kw.get("is_sender", False),
        sender_email=kw.get("sender_email"),
        resolved_context=False,
        identifiers=kw.get("identifiers", ()),
        parent_name=kw.get("parent_name"),
        children=kw.get("children", ()),
    )


def _ctx(inbox: int, idents: list[EmailIdentity]) -> ResolverInput:
    return ResolverInput(
        inbox_id=inbox, subject="s", body="b", identities=tuple(idents), candidates=()
    )


def _exists(c: Connection, identity_id: int) -> bool:
    return (
        c.execute(text("SELECT 1 FROM mod_identidades WHERE id=:b"), {"b": identity_id}).first()
        is not None
    )


def test_apply_merge_absorbs_and_keeps_alias(conn: Connection) -> None:
    src = _source(conn)
    mid = _inbox(conn, src)
    a = _identity(conn, "organizacion", "Pontificia Universidad Javeriana")
    b = _identity(conn, "organizacion", "javeriana.edu.co")
    _mention(conn, mid, a, "organizacion", "exact_name", None)
    _mention(conn, mid, b, "organizacion", "sender", None)
    ctx = _ctx(
        mid,
        [_ei(a, "organizacion", "Pontificia"), _ei(b, "organizacion", "javeriana.edu.co")],
    )
    decision = ResolverDecision(merges=(Merge(a, b, 0.9),), parents=(), sender=None)
    stats = apply_resolution(conn, 1, ctx, decision, min_merge=0.75, min_parent=0.8)
    assert stats.merged == 1
    assert not _exists(conn, b)
    aliases = conn.execute(
        text("SELECT aliases FROM mod_identidades WHERE id=:a"), {"a": a}
    ).scalar()
    assert "javeriana.edu.co" in list(aliases or [])


def test_apply_cross_kind_merge_desconocido_into_org(conn: Connection) -> None:
    # el resolvedor ahora puede fundir CROSS-KIND: una `desconocido` absorbida en la org.
    src = _source(conn)
    mid = _inbox(conn, src)
    org = _identity(conn, "organizacion", "tusclases.co")
    desc = _identity(conn, "desconocido", "info@tusclases.co")
    _mention(conn, mid, org, "organizacion", "exact_name", None)
    _mention(conn, mid, desc, "desconocido", "sender", "info@tusclases.co")
    ctx = _ctx(
        mid,
        [_ei(org, "organizacion", "tusclases.co"), _ei(desc, "desconocido", "info@tusclases.co")],
    )
    decision = ResolverDecision(merges=(Merge(org, desc, 0.9),), parents=(), sender=None)
    stats = apply_resolution(conn, 1, ctx, decision, min_merge=0.75, min_parent=0.8)
    assert stats.merged == 1
    assert not _exists(conn, desc)
    assert (
        conn.execute(text("SELECT kind FROM mod_identidades WHERE id=:o"), {"o": org}).scalar_one()
        == "organizacion"
    )


def test_apply_low_confidence_merge_skipped(conn: Connection) -> None:
    src = _source(conn)
    mid = _inbox(conn, src)
    a = _identity(conn, "organizacion", "Acme")
    b = _identity(conn, "organizacion", "Acme Inc")
    _mention(conn, mid, a, "organizacion", "exact_name", None)
    decision = ResolverDecision(merges=(Merge(a, b, 0.4),), parents=(), sender=None)
    stats = apply_resolution(
        conn,
        1,
        _ctx(mid, [_ei(a, "organizacion", "Acme")]),
        decision,
        min_merge=0.75,
        min_parent=0.8,
    )
    assert stats.merged == 0
    assert _exists(conn, b)


def test_apply_sender_mailbox_attaches_email_to_org(conn: Connection) -> None:
    src = _source(conn)
    mid = _inbox(conn, src)
    org = _identity(conn, "organizacion", "Acme")
    _mention(conn, mid, org, "organizacion", "sender", "info@acme.com")
    ctx = _ctx(
        mid, [_ei(org, "organizacion", "Acme", is_sender=True, sender_email="info@acme.com")]
    )
    decision = ResolverDecision(
        merges=(), parents=(), sender=SenderDisposition(False, org, None, 0.9)
    )
    stats = apply_resolution(conn, 1, ctx, decision, min_merge=0.75, min_parent=0.8)
    assert stats.contacts == 1
    assert ("email", "info@acme.com") in _ids(conn, org)


def test_apply_sender_org_merged_away_no_crash(conn: Connection) -> None:
    # regresión (smoke 100): si un merge de ESTE apply funde la org del remitente, la disposición
    # re-lee la org VIVA del mention (no el id colgante de ctx) → no crashea con FK.
    src = _source(conn)
    mid = _inbox(conn, src)
    a = _identity(conn, "organizacion", "Acme por dominio")
    b = _identity(conn, "organizacion", "Acme")
    _mention(conn, mid, a, "organizacion", "sender", "juan@acme.com")  # remitente → org A (prov.)
    ctx = _ctx(
        mid,
        [_ei(a, "organizacion", "Acme por dominio", is_sender=True, sender_email="juan@acme.com")],
    )
    # merge A→B (A se borra; el mention se re-apunta a B) + disposición persona del remitente.
    decision = ResolverDecision(
        merges=(Merge(b, a, 0.9),),
        parents=(),
        sender=SenderDisposition(True, None, "Juan Pérez", 0.9),
    )
    stats = apply_resolution(conn, 1, ctx, decision, min_merge=0.75, min_parent=0.8)
    assert stats.merged == 1 and stats.persons == 1
    assert not _exists(conn, a)  # A fundida en B
    aff = conn.execute(
        text("SELECT org_id FROM mod_identidades_person_orgs WHERE user_id = 1")
    ).scalar()
    assert aff is not None and int(aff) == b  # afiliado a la org VIVA (B), no al colgante (A)


def test_apply_sender_person_creates_ficha_and_repoints(conn: Connection) -> None:
    src = _source(conn)
    mid = _inbox(conn, src)
    org = _identity(conn, "organizacion", "Acme")
    _mention(conn, mid, org, "organizacion", "sender", "juan@acme.com")
    ctx = _ctx(
        mid, [_ei(org, "organizacion", "Acme", is_sender=True, sender_email="juan@acme.com")]
    )
    decision = ResolverDecision(
        merges=(), parents=(), sender=SenderDisposition(True, None, "Juan Pérez", 0.9)
    )
    stats = apply_resolution(conn, 1, ctx, decision, min_merge=0.75, min_parent=0.8)
    assert stats.persons == 1
    person = conn.execute(
        text("SELECT id FROM mod_identidades WHERE kind='persona' AND display_name='Juan Pérez'")
    ).scalar()
    assert person is not None
    assert ("email", "juan@acme.com") in _ids(conn, int(person))
    rid = conn.execute(
        text(
            "SELECT resolved_identity_id FROM mod_identidades_mentions "
            "WHERE :i = ANY(source_inbox_ids) AND resolution_method='sender'"
        ),
        {"i": mid},
    ).scalar()
    assert rid is not None and int(rid) == int(person)


def test_apply_marks_resolved_context(conn: Connection) -> None:
    src = _source(conn)
    mid = _inbox(conn, src)
    org = _identity(conn, "organizacion", "Acme")
    _mention(conn, mid, org, "organizacion", "exact_name", None)
    apply_resolution(
        conn,
        1,
        _ctx(mid, [_ei(org, "organizacion", "Acme")]),
        ResolverDecision((), (), None),
        min_merge=0.75,
        min_parent=0.8,
    )
    marked = conn.execute(
        text("SELECT metadata->>'resolved_context_at' FROM mod_identidades WHERE id=:o"), {"o": org}
    ).scalar()
    assert marked is not None


@pytest.mark.asyncio
async def test_resolve_email_calls_llm_and_parses(conn: Connection) -> None:
    llm = FakeLLM(json.dumps({"merges": [], "relations": [], "sender": None}))
    ctx = _ctx(1, [_ei(7, "organizacion", "Acme")])
    decision, result = await resolve_email(llm, ctx)
    assert llm.calls == 1
    assert result.model == "fake"
    assert decision.merges == () and decision.sender is None


def test_serialize_shows_identity_data_and_hierarchy() -> None:
    # El resolver recibe la identidad REAL con sus DATOS (email/dominio = atributos) + su jerarquía.
    ctx = _ctx(
        1,
        [
            _ei(
                1,
                "organizacion",
                "javeriana.edu.co",
                identifiers=("domain:javeriana.edu.co", "email:a@x"),
                children=("RAS Javeriana IEEE",),
            ),
            _ei(5, "organizacion", "RAS Javeriana IEEE", parent_name="javeriana.edu.co"),
        ],
    )
    out = _serialize(ctx)
    assert "datos=[domain:javeriana.edu.co, email:a@x]" in out  # el correo es DATO de la identidad
    assert "hijos=[RAS Javeriana IEEE]" in out  # jerarquía hacia abajo
    assert "padre='javeriana.edu.co'" in out  # jerarquía hacia arriba


def test_apply_affiliation_persona_to_org(conn: Connection) -> None:
    # El LLM decide la afiliación persona→org (con rol); apply_resolution la crea en person_orgs.
    mid = _inbox(conn, _source(conn))
    org = _identity(conn, "organizacion", "RAS Javeriana IEEE")
    person = _identity(conn, "persona", "Valeria Caycedo")
    ctx = _ctx(mid, [_ei(org, "organizacion", "RAS"), _ei(person, "persona", "Valeria")])
    decision = ResolverDecision((), (), None, (Affiliation(person, org, "Presidenta", 0.9),))
    stats = apply_resolution(conn, 1, ctx, decision, min_merge=0.75, min_parent=0.8)
    assert stats.affiliated == 1
    row = conn.execute(
        text("SELECT org_id, role FROM mod_identidades_person_orgs WHERE person_id = :p"),
        {"p": person},
    ).one()
    assert int(row[0]) == org and row[1] == "Presidenta"


def test_apply_affiliation_rejects_wrong_kinds(conn: Connection) -> None:
    # Afiliación org→org (kinds equivocados) → se ignora; afiliación es estrictamente persona→org.
    mid = _inbox(conn, _source(conn))
    a = _identity(conn, "organizacion", "Acme")
    b = _identity(conn, "organizacion", "Beta")
    ctx = _ctx(mid, [_ei(a, "organizacion", "Acme"), _ei(b, "organizacion", "Beta")])
    decision = ResolverDecision((), (), None, (Affiliation(a, b, None, 0.9),))
    stats = apply_resolution(conn, 1, ctx, decision, min_merge=0.75, min_parent=0.8)
    assert stats.affiliated == 0
    cnt = conn.execute(
        text("SELECT count(*) FROM mod_identidades_person_orgs WHERE user_id = 1")
    ).scalar()
    assert cnt == 0


def test_apply_hierarchy_skips_persona_child(conn: Connection) -> None:
    # Una PERSONA nunca cuelga en jerarquía (eso es afiliación) → su parent se ignora.
    mid = _inbox(conn, _source(conn))
    org = _identity(conn, "organizacion", "Javeriana")
    person = _identity(conn, "persona", "Claudia")
    ctx = _ctx(mid, [_ei(org, "organizacion", "Javeriana"), _ei(person, "persona", "Claudia")])
    decision = ResolverDecision((), (Parent(person, org, None, 0.95),), None)
    stats = apply_resolution(conn, 1, ctx, decision, min_merge=0.75, min_parent=0.8)
    assert stats.linked == 0
    parent = conn.execute(
        text("SELECT parent_identity_id FROM mod_identidades WHERE id = :id"), {"id": person}
    ).scalar()
    assert parent is None
