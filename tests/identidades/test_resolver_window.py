"""E2E del driver por-ventana del resolvedor (`run_resolver_window`) con LLM falso (sin red).

Cubre: gate apagado → no-op (ni se llama al LLM); buzón de remitente → email atado a la org y
correo marcado (la 2da corrida se SALTA: incremental); fusión dominio↔nombre con contexto.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.modules.identidades.resolve_llm import run_resolver_window
from memex.modules.identidades.senders import weave_email_senders
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


def _source(c: object) -> int:  # c: Connection (committed)
    return int(
        c.execute(  # type: ignore[attr-defined]
            text("INSERT INTO sources (user_id,name,type) VALUES (1,'m','imap') RETURNING id")
        ).scalar_one()
    )


def _enable() -> None:
    with connection() as c:
        upsert_settings(c, 1, resolver_enabled=True)


def _seed_inbox(email: str, name: str) -> int:
    with connection() as c:
        src = _source(c)
        payload = json.dumps({"subject": "hola", "from": {"email": email, "name": name}})
        return int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id,source_id,external_id,occurred_at,payload) "
                    "VALUES (1,:s,'e',NOW(),CAST(:p AS JSONB)) RETURNING id"
                ),
                {"s": src, "p": payload},
            ).scalar_one()
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


def _mention(inbox: int, ident: int, method: str, email: str | None) -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_identidades_mentions (user_id,source_inbox_ids,mentioned_name,"
                "resolved_identity_id,resolved_kind,resolution_method,email) "
                "VALUES (1,ARRAY[:i],'x',:o,'organizacion',:m,:e)"
            ),
            {"i": inbox, "o": ident, "m": method, "e": email},
        )


def _identifiers(identity_id: int) -> set[tuple[str, str]]:
    with connection() as c:
        return {
            (str(r.kind), str(r.value_norm))
            for r in c.execute(
                text(
                    "SELECT kind,value_norm FROM mod_identidades_identifiers WHERE identity_id=:i"
                ),
                {"i": identity_id},
            ).all()
        }


def _exists(identity_id: int) -> bool:
    with connection() as c:
        return (
            c.execute(text("SELECT 1 FROM mod_identidades WHERE id=:i"), {"i": identity_id}).first()
            is not None
        )


@pytest.mark.asyncio
async def test_gate_off_is_noop() -> None:
    mid = _seed_inbox("info@acme.com", "Acme")
    org = _org("Acme")
    _mention(mid, org, "sender", "info@acme.com")
    llm = FakeLLM("{}")
    stats = await run_resolver_window(1, [mid], client=llm)
    assert llm.calls == 0  # gate apagado por default → ni se llama al LLM
    assert stats.contacts == 0


@pytest.mark.asyncio
async def test_sender_mailbox_attached_then_skipped() -> None:
    _enable()
    mid = _seed_inbox("info@acme.com", "Acme")
    org = _org("Acme")
    _mention(mid, org, "sender", "info@acme.com")
    content = json.dumps(
        {
            "merges": [],
            "parents": [],
            "sender": {"is_person": False, "owner_id": org, "confidence": 0.9},
        }
    )
    llm = FakeLLM(content)
    stats = await run_resolver_window(1, [mid], client=llm)
    assert llm.calls == 1
    assert stats.contacts == 1
    assert ("email", "info@acme.com") in _identifiers(org)
    # 2da corrida: ya resuelto + email asociado → skip incremental (0 llamadas)
    llm2 = FakeLLM(content)
    await run_resolver_window(1, [mid], client=llm2)
    assert llm2.calls == 0


@pytest.mark.asyncio
async def test_merge_domain_org_into_named_org() -> None:
    _enable()
    mid = _seed_inbox("rector@javeriana.edu.co", "Javeriana")
    named = _org("Pontificia Universidad Javeriana")
    dom = _org("javeriana.edu.co")
    _mention(mid, named, "exact_name", None)
    _mention(mid, dom, "sender", "rector@javeriana.edu.co")
    content = json.dumps(
        {
            "merges": [{"keep_id": named, "drop_id": dom, "confidence": 0.95}],
            "parents": [],
            "sender": None,
        }
    )
    stats = await run_resolver_window(1, [mid], client=FakeLLM(content))
    assert stats.merged == 1
    assert not _exists(dom)  # el domino-org se fundió en la org nombrada


@pytest.mark.asyncio
async def test_corporate_sender_without_known_org_is_leftover() -> None:
    # UN DOMINIO NO ES UNA IDENTIDAD: sin org real conocida, el remitente corporativo queda LEFTOVER
    # — NO se crea ficha nombrada por el dominio (el viejo "#1"). Lo ata luego `attribute_domain`.
    mid = _seed_inbox("navarroff@javeriana.edu.co", "Felix Navarro")  # no-rol, nombre de persona
    with connection() as c:
        inserted = weave_email_senders(c, 1, [mid])
    assert inserted == 0  # leftover → sin mención de remitente
    with connection() as c:
        n = c.execute(
            text("SELECT count(*) FROM mod_identidades WHERE user_id=1 AND display_name=:d"),
            {"d": "javeriana.edu.co"},
        ).scalar()
    assert n == 0  # no se creó ficha-dominio


@pytest.mark.asyncio
async def test_corporate_role_sender_with_org_name_creates_by_name() -> None:
    # Rol/relay que trae el nombre de la org → se crea por NOMBRE (no por dominio), con el dominio
    # atado como atributo.
    mid = _seed_inbox("noreply@nequi.com.co", "Nequi")  # rol + nombre de org
    with connection() as c:
        weave_email_senders(c, 1, [mid])
    with connection() as c:
        names = {
            str(r[0])
            for r in c.execute(
                text(
                    "SELECT display_name FROM mod_identidades "
                    "WHERE user_id=1 AND kind='organizacion'"
                )
            ).all()
        }
    assert "Nequi" in names  # por NOMBRE
    assert "nequi.com.co" not in names  # NO por dominio
