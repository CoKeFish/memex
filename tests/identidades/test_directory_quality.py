"""Calidad del directorio (experimento 2026-09-02 → main): las reglas que sacan el ruido y
devuelven la granularidad institucional.

- una mención `link=mencion` NO crea ficha (queda como avistamiento sin identidad);
- una org que resuelve por email/dominio a una org de nombre distinto es una SUB-UNIDAD (hija);
- un dominio relay (stripe.com…) no identifica a la org de la mención;
- el resolver tipifica relaciones (`parte_de`/`fabricado_por`/`miembro_de`) y descarta «usa»;
- una relación cuyo target se fusionó en la misma decisión se re-apunta al superviviente;
- afiliar contra un id absorbido usa al superviviente; contra uno inexistente no rompe la ventana.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import LLMClient
from memex.modules.contract import ModuleContext
from memex.modules.identidades.merge import merge_identities
from memex.modules.identidades.module import (
    IdentidadesModule,
    _affiliate,
    load_known_index,
)
from memex.modules.identidades.resolve_context import ResolverInput
from memex.modules.identidades.resolve_llm import (
    Merge,
    Parent,
    ResolverDecision,
    _parse_relations,
    apply_resolution,
)
from memex.modules.identidades.schema import IdentityItem
from memex.modules.identidades.senders import _org_for_domain

pytestmark = pytest.mark.usefixtures("seed_source")


def _org(name: str, *, domain: str | None = None) -> int:
    with connection() as c:
        oid = int(
            c.execute(
                text(
                    "INSERT INTO mod_identidades (user_id, kind, display_name, source) "
                    "VALUES (1, 'organizacion', :n, 'extraction') RETURNING id"
                ),
                {"n": name},
            ).scalar_one()
        )
        if domain:
            c.execute(
                text(
                    "INSERT INTO mod_identidades_identifiers "
                    "(user_id, identity_id, platform, kind, value, value_norm, source) "
                    "VALUES (1, :i, 'domain', 'domain', :d, :d, 'extraction')"
                ),
                {"i": oid, "d": domain},
            )
        return oid


def _persona(name: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_identidades (user_id, kind, display_name, source) "
                    "VALUES (1, 'persona', :n, 'extraction') RETURNING id"
                ),
                {"n": name},
            ).scalar_one()
        )


def _row(identity_id: int) -> dict[str, Any] | None:
    with connection() as c:
        r = (
            c.execute(text("SELECT * FROM mod_identidades WHERE id = :i"), {"i": identity_id})
            .mappings()
            .first()
        )
        return dict(r) if r else None


def _identities(name_like: str) -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(
                text(
                    "SELECT * FROM mod_identidades WHERE user_id = 1 AND display_name ILIKE :n "
                    "ORDER BY id"
                ),
                {"n": name_like},
            )
            .mappings()
            .all()
        ]


def _mentions(name: str) -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(
                text(
                    "SELECT * FROM mod_identidades_mentions WHERE user_id = 1 "
                    "AND mentioned_name = :n ORDER BY id"
                ),
                {"n": name},
            )
            .mappings()
            .all()
        ]


def _email_owner(email: str) -> int | None:
    with connection() as c:
        v = c.execute(
            text(
                "SELECT identity_id FROM mod_identidades_identifiers "
                "WHERE user_id = 1 AND kind = 'email' AND value_norm = :e"
            ),
            {"e": email},
        ).scalar()
        return int(v) if v is not None else None


async def _persist(items: list[IdentityItem]) -> int:
    with connection() as conn:
        ctx = ModuleContext(
            user_id=1,
            conn=conn,
            llm=cast(LLMClient, None),  # dedup determinista, sin LLM
            deps={},
            summary_id=None,
            inbox_ids=(5,),
        )
        return await IdentidadesModule().persist(ctx, items)


# ----------------------------------------------------------------------- menciones sin vínculo


@pytest.mark.asyncio
async def test_mention_link_does_not_create_identity() -> None:
    """Capgemini nombrada en un digest de vacantes: avistamiento sí, ficha no."""
    await _persist(
        [
            IdentityItem(
                source_inbox_ids=(5,),
                name="Capgemini Engineering",
                kind="organizacion",
                link="mencion",
                evidence="7 vacantes en Capgemini Engineering",
            )
        ]
    )
    assert _identities("Capgemini%") == []
    (m,) = _mentions("Capgemini Engineering")
    assert m["resolved_identity_id"] is None and m["resolution_method"] == "unresolved"


@pytest.mark.asyncio
async def test_mention_link_still_attaches_to_existing_identity() -> None:
    """Si la ficha YA existe, la mención se ata (evidencia), aunque sea solo nombrada."""
    oid = _org("Anthropic")
    await _persist(
        [IdentityItem(source_inbox_ids=(5,), name="Anthropic", kind="organizacion", link="mencion")]
    )
    (m,) = _mentions("Anthropic")
    assert m["resolved_identity_id"] == oid
    assert len(_identities("Anthropic")) == 1


@pytest.mark.asyncio
async def test_real_link_creates_identity() -> None:
    await _persist(
        [IdentityItem(source_inbox_ids=(5,), name="Nequi", kind="organizacion", link="contraparte")]
    )
    assert len(_identities("Nequi")) == 1


# ----------------------------------------------------------------------- sub-unidades


@pytest.mark.asyncio
async def test_subunit_under_shared_domain_becomes_child() -> None:
    """«Carrera de Ingeniería Electrónica» con ielec@salesiana.edu.co NO es la universidad: es su
    hija, y el email pasa a ser suyo. Una segunda mención resuelve a la hija sin duplicar."""
    puj = _org("Pontificia Universidad Salesiana", domain="salesiana.edu.co")
    item = IdentityItem(
        source_inbox_ids=(5,),
        name="Carrera de Ingeniería Electrónica",
        kind="organizacion",
        email="ielec@salesiana.edu.co",
        link="remitente",
    )
    await _persist([item])
    (child,) = _identities("Carrera de Ingenier%")
    assert child["parent_identity_id"] == puj
    assert _email_owner("ielec@salesiana.edu.co") == child["id"]
    (m,) = _mentions("Carrera de Ingeniería Electrónica")
    assert m["resolved_identity_id"] == child["id"]

    await _persist([item])  # segunda vez: misma hija, por email
    assert len(_identities("Carrera de Ingenier%")) == 1
    assert all(x["resolved_identity_id"] == child["id"] for x in _mentions(item.name))


@pytest.mark.asyncio
async def test_similar_name_under_domain_is_the_same_org() -> None:
    """«Universidad Salesiana» ⊂ «Pontificia Universidad Salesiana» por contención: NO se parte."""
    puj = _org("Pontificia Universidad Salesiana", domain="salesiana.edu.co")
    await _persist(
        [
            IdentityItem(
                source_inbox_ids=(5,),
                name="Universidad Salesiana",
                kind="organizacion",
                email="admisiones@salesiana.edu.co",
                link="remitente",
            )
        ]
    )
    (m,) = _mentions("Universidad Salesiana")
    assert m["resolved_identity_id"] == puj
    assert len(_identities("%Salesiana%")) == 1


# ----------------------------------------------------------------------- dominios relay


def test_relay_domain_does_not_identify_the_org() -> None:
    """El recibo de Railway llega desde stripe.com: la mención no es Stripe."""
    _org("Stripe", domain="stripe.com")
    with connection() as c:
        index = load_known_index(c, 1)
        res = index.resolve(
            IdentityItem(
                source_inbox_ids=(5,),
                name="Railway Corporation",
                kind="organizacion",
                email="invoice+statements@stripe.com",
                link="contraparte",
            )
        )
        assert res.identity_id is None
        assert _org_for_domain(c, 1, "stripe.com", "Railway Corporation", index) is None


# ----------------------------------------------------------------------- resolver tipado


def test_parse_relations_honors_relation_kind() -> None:
    kinds = {1: "producto", 2: "organizacion", 3: "persona", 4: "organizacion"}
    valid = set(kinds)
    raw = [
        {"relation_kind": "fabricado_por", "source_id": 1, "target_id": 2, "confidence": 0.9},
        {"relation_kind": "usa", "source_id": 1, "target_id": 4, "confidence": 0.9},  # descartada
        {"relation_kind": "miembro_de", "source_id": 3, "target_id": 2, "role": "docente"},
        {"relation_kind": "parte_de", "source_id": 3, "target_id": 2},  # persona: incoherente
        {"relation_kind": "miembro_de", "source_id": 4, "target_id": 2},  # org: incoherente
    ]
    parents, affils = _parse_relations(raw, valid, kinds)
    assert [(p.child_id, p.parent_id) for p in parents] == [(1, 2)]
    assert [(a.person_id, a.org_id, a.role) for a in affils] == [(3, 2, "docente")]


def test_apply_resolution_remaps_relation_after_merge() -> None:
    """Fusión A→B y relación C ⊂ A en la MISMA decisión: C debe colgar de B (antes se perdía)."""
    a = _org("Centro Britano Americano (dup)")
    b = _org("Centro Britano Americano")
    c_id = _org("Sede Centro")
    decision = ResolverDecision(
        merges=(Merge(survivor_id=b, absorbed_id=a, confidence=0.95),),
        parents=(Parent(child_id=c_id, parent_id=a, parent_name=None, confidence=0.95),),
        sender=None,
    )
    ctx = ResolverInput(inbox_id=5, subject="", body="", identities=(), candidates=())
    with connection() as conn:
        stats = apply_resolution(conn, 1, ctx, decision, min_merge=0.75, min_parent=0.8)
    assert stats.merged == 1 and stats.linked == 1
    assert _row(a) is None
    assert (_row(c_id) or {})["parent_identity_id"] == b


# ----------------------------------------------------------------------- afiliar sin romper


def test_affiliate_uses_survivor_and_skips_missing() -> None:
    p = _persona("Juan David Cruz")
    old = _org("Britano (viejo)")
    new = _org("Centro Britano Americano")
    with connection() as conn:
        assert merge_identities(conn, 1, new, old)
        _affiliate(conn, 1, p, old, "asesor", source="extraction")  # id absorbido → superviviente
        _affiliate(conn, 1, p, 999_999, "x", source="extraction")  # inexistente → se omite
        rows = conn.execute(
            text("SELECT org_id, role FROM mod_identidades_person_orgs WHERE person_id = :p"),
            {"p": p},
        ).all()
    assert [(r[0], r[1]) for r in rows] == [(new, "asesor")]
