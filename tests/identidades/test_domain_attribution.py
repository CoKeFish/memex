"""Fallback `attribute_domain` (off/desconectado): el LLM elige cuál identidad EXISTENTE es dueña de
un dominio huérfano; si hay una clara, le cuelga el dominio como atributo. No crea orgs."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.modules.identidades.domain_attribution import (
    attribute_domain,
    attribute_domains_for_window,
)
from memex.modules.identidades.module import _insert_identifier
from memex.modules.identidades.settings import upsert_settings


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
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="stop",
        )


def _org(name: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_identidades (user_id,kind,display_name,source) "
                    "VALUES (1,'organizacion',:n,'extraction') RETURNING id"
                ),
                {"n": name},
            ).scalar_one()
        )


def _inbox_from(email: str) -> int:
    with connection() as c:
        src = int(
            c.execute(
                text("INSERT INTO sources (user_id,name,type) VALUES (1,'m','imap') RETURNING id")
            ).scalar_one()
        )
        payload = json.dumps({"subject": "x", "from": {"email": email, "name": "x"}})
        return int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id,source_id,external_id,occurred_at,payload) "
                    "VALUES (1,:s,'e',NOW(),CAST(:p AS JSONB)) RETURNING id"
                ),
                {"s": src, "p": payload},
            ).scalar_one()
        )


def _mention(inbox: int, ident: int) -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_identidades_mentions (user_id,source_inbox_ids,mentioned_name,"
                "resolved_identity_id,resolved_kind,resolution_method) "
                "VALUES (1,ARRAY[:i],'x',:o,'organizacion','exact_name')"
            ),
            {"i": inbox, "o": ident},
        )


def _domains(identity_id: int) -> set[str]:
    with connection() as c:
        return {
            str(r[0])
            for r in c.execute(
                text(
                    "SELECT value_norm FROM mod_identidades_identifiers "
                    "WHERE identity_id=:i AND kind='domain'"
                ),
                {"i": identity_id},
            ).all()
        }


@pytest.mark.asyncio
async def test_attribute_domain_picks_owner_and_attaches() -> None:
    # co-ocurrencia: la org aparece en un correo enviado desde @javeriana.edu.co → candidata.
    org = _org("Pontificia Universidad Javeriana")
    _mention(_inbox_from("rector@javeriana.edu.co"), org)
    llm = FakeLLM(json.dumps({"owner_id": org, "confidence": 0.95}))
    with connection() as c:
        res = await attribute_domain(c, 1, "javeriana.edu.co", llm=llm)
    assert llm.calls == 1
    assert res.owner_id == org and res.applied
    assert "javeriana.edu.co" in _domains(org)  # el dominio quedó como ATRIBUTO de la org real


@pytest.mark.asyncio
async def test_attribute_domain_no_owner_when_llm_declines() -> None:
    org = _org("Acme")
    _mention(_inbox_from("info@acme.com"), org)
    llm = FakeLLM(json.dumps({"owner_id": None, "confidence": 0.0}))
    with connection() as c:
        res = await attribute_domain(c, 1, "acme.com", llm=llm)
    assert res.owner_id is None and not res.applied
    assert "acme.com" not in _domains(org)


@pytest.mark.asyncio
async def test_attribute_domain_already_owned_skips_llm() -> None:
    org = _org("Acme")
    with connection() as c:
        _insert_identifier(c, 1, org, "domain", "domain", "acme.com", "acme.com", source="test")
    llm = FakeLLM("{}")
    with connection() as c:
        res = await attribute_domain(c, 1, "acme.com", llm=llm)
    assert llm.calls == 0  # ya es atributo de alguien → ni se llama al LLM
    assert res.owner_id == org and not res.applied


@pytest.mark.asyncio
async def test_attribute_domain_no_candidates_skips_llm() -> None:
    llm = FakeLLM("{}")
    with connection() as c:
        res = await attribute_domain(c, 1, "ghostxyz.com", llm=llm)
    assert llm.calls == 0  # sin candidatas → ni se llama al LLM
    assert res.owner_id is None and res.candidates == 0


@pytest.mark.asyncio
async def test_attribute_domains_for_window_attaches_when_resolver_on() -> None:
    # CONECTADO al pipeline: con resolver ON, ata los dominios corporativos sin dueña de la ventana.
    with connection() as c:
        upsert_settings(c, 1, resolver_enabled=True)
    org = _org("Pontificia Universidad Javeriana")
    inb = _inbox_from("rector@javeriana.edu.co")
    _mention(inb, org)  # co-ocurrencia con el dominio
    llm = FakeLLM(json.dumps({"owner_id": org, "confidence": 0.95}))
    attached = await attribute_domains_for_window(1, [inb], llm=llm)
    assert attached == 1
    assert "javeriana.edu.co" in _domains(org)


@pytest.mark.asyncio
async def test_attribute_domain_offers_hierarchy_ancestor() -> None:
    # Incluye los ANCESTROS: la RAÍZ (que no co-ocurre directo) se ofrece vía el padre de la
    # sub-unidad → el LLM puede atribuir el dominio a la org de más alto nivel, no a la sub-unidad.
    root = _org("Pontificia Universidad Javeriana")
    sub = _org("Javeriana Secretaria General")
    with connection() as c:
        c.execute(
            text("UPDATE mod_identidades SET parent_identity_id=:p WHERE id=:s"),
            {"p": root, "s": sub},
        )
    _mention(_inbox_from("secretaria@javeriana.edu.co"), sub)  # solo la SUB co-ocurre
    llm = FakeLLM(json.dumps({"owner_id": root, "confidence": 0.95}))  # el LLM elige la RAÍZ
    with connection() as c:
        res = await attribute_domain(c, 1, "javeriana.edu.co", llm=llm)
    # si el ancestro NO se ofreciera, owner_id=root se descartaría (no candidata) → no ataría
    assert res.owner_id == root and res.applied
    assert "javeriana.edu.co" in _domains(root)


@pytest.mark.asyncio
async def test_attribute_domains_for_window_noop_when_resolver_off() -> None:
    with connection() as c:
        upsert_settings(c, 1, resolver_enabled=False)
    org = _org("Acme")
    inb = _inbox_from("info@acme.com")
    _mention(inb, org)
    llm = FakeLLM(json.dumps({"owner_id": org, "confidence": 0.95}))
    attached = await attribute_domains_for_window(1, [inb], llm=llm)
    assert attached == 0
    assert llm.calls == 0  # gate apagado → ni se llama al LLM
    assert "acme.com" not in _domains(org)
