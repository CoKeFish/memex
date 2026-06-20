"""Clasificador LLM de tipo (`desconocido` → persona/org/producto) con un LLMClient FALSO (sin red).

Cubre: promueve con confianza alta; baja confianza / kind fuera de lista / respuesta no parseable →
queda `desconocido` (sesgo a no adivinar); idempotencia (re-correr no re-promueve); el contexto
(dominio + asunto) llega al prompt; la arista `afiliado` se re-proyecta bajo el slug nuevo y no
queda huérfana en `identidades:desconocido`."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.modules.identidades.classify import (
    ClassifyView,
    _fmt_view,
    _parse_classification,
    run_classify,
)
from memex.relations.deterministic import weave_afiliacion
from memex.relations.edges import list_edges


class FakeLLM:
    """Devuelve siempre el mismo `content`; cuenta llamadas. Cumple el Protocol LLMClient."""

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


def _exec(sql: str, **params: Any) -> Any:
    with connection() as c:
        result = c.execute(text(sql), params)
        return result.scalar() if result.returns_rows else None


def _seed_desconocido(email: str, name: str, subject: str = "asunto de prueba") -> int:
    """Siembra una entidad `desconocido` (con su email, la org del dominio, su afiliación —arista
    `afiliado` incluida— y un correo donde fue remitente) directamente. El camino del remitente ya
    NO crea `desconocido` para correo corporativo (cuelga el email en la org); el clasificador opera
    sobre `desconocido` de cualquier origen — esto recrea ese estado de entrada."""
    domain = email.split("@", 1)[1]
    src = int(
        _exec(
            "INSERT INTO sources (user_id, name, type) VALUES (1, :n, 'imap') RETURNING id",
            n=f"src-{email}",
        )
    )
    payload = {"from": {"email": email, "name": name}, "folder": "Inbox", "subject": subject}
    mid = int(
        _exec(
            "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
            "VALUES (1, :s, :ext, NOW(), CAST(:p AS JSONB)) RETURNING id",
            s=src,
            ext=f"m-{email}",
            p=json.dumps(payload),
        )
    )
    # org del dominio (reusar si ya existe; varios seeds comparten dominio), nombrada por el dominio
    org = _exec(
        "SELECT identity_id FROM mod_identidades_identifiers "
        "WHERE user_id = 1 AND kind = 'domain' AND value_norm = :d",
        d=domain,
    )
    if org is None:
        org = int(
            _exec(
                "INSERT INTO mod_identidades (user_id, kind, display_name, source, interest) "
                "VALUES (1, 'organizacion', :n, 'extraction', FALSE) RETURNING id",
                n=domain,
            )
        )
        _exec(
            "INSERT INTO mod_identidades_identifiers "
            "(user_id, identity_id, platform, kind, value, value_norm, source) "
            "VALUES (1, :i, 'domain', 'domain', :d, :d, 'extraction')",
            i=org,
            d=domain,
        )
    org = int(org)
    eid = int(
        _exec(
            "INSERT INTO mod_identidades (user_id, kind, display_name, source, interest) "
            "VALUES (1, 'desconocido', :n, 'extraction', FALSE) RETURNING id",
            n=name,
        )
    )
    _exec(
        "INSERT INTO mod_identidades_identifiers "
        "(user_id, identity_id, platform, kind, value, value_norm, source) "
        "VALUES (1, :i, 'email', 'email', :e, :e, 'extraction')",
        i=eid,
        e=email,
    )
    _exec(
        "INSERT INTO mod_identidades_person_orgs (user_id, person_id, org_id, role, source) "
        "VALUES (1, :p, :o, NULL, 'extraction')",
        p=eid,
        o=org,
    )
    _exec(
        "INSERT INTO mod_identidades_mentions "
        "(user_id, source_inbox_ids, mentioned_name, mentioned_kind, resolved_kind, "
        " resolved_identity_id, resolution_method, email) "
        "VALUES (1, ARRAY[:m], :n, 'unknown', 'desconocido', :i, 'sender', :e)",
        m=mid,
        n=name,
        i=eid,
        e=email,
    )
    # arista `afiliado` en el grafo (la que el clasificador re-proyecta al promover el kind).
    with connection() as c:
        weave_afiliacion(c, 1, eid)
    return eid


def _kind(identity_id: int) -> str:
    return str(_exec("SELECT kind FROM mod_identidades WHERE id = :i", i=identity_id))


# ----- _parse_classification (puro) ---------------------------------------------- #


def test_parse_valid() -> None:
    d = _parse_classification('{"kind": "persona", "confidence": 0.9, "rationale": "r"}')
    assert (d.kind, d.confidence) == ("persona", 0.9)


def test_parse_garbage_to_desconocido() -> None:
    d = _parse_classification("no soy json")
    assert d.kind == "desconocido" and d.confidence == 0.0 and d.rationale == "parse_fallback"


def test_parse_out_of_list_kind_to_desconocido() -> None:
    # un kind fuera de la lista promovible → desconocido (no se promueve a un tipo inventado)
    assert _parse_classification('{"kind": "animal", "confidence": 0.9}').kind == "desconocido"
    assert _parse_classification('{"kind": "desconocido", "confidence": 0.9}').kind == "desconocido"


def test_parse_bool_confidence_is_zero() -> None:
    assert _parse_classification('{"kind": "persona", "confidence": true}').confidence == 0.0


def test_parse_clamps_confidence() -> None:
    assert _parse_classification('{"kind": "persona", "confidence": 5}').confidence == 1.0
    assert _parse_classification('{"kind": "persona", "confidence": -2}').confidence == 0.0


def test_fmt_view_incluye_contexto() -> None:
    v = ClassifyView(
        id=1,
        display_name="ielec",
        identifiers=("email:email:ielec@javeriana.edu.co",),
        affiliated_org="Javeriana",
        subjects=("Inscripciones electiva",),
    )
    s = _fmt_view(v)
    assert "ielec@javeriana.edu.co" in s and "Javeriana" in s and "Inscripciones electiva" in s


# ----- worker (DB + LLM falso) --------------------------------------------------- #


@pytest.mark.asyncio
async def test_promotes_confident() -> None:
    eid = _seed_desconocido("ielec@javeriana.edu.co", "Carrera de Ingeniería Electrónica")
    assert _kind(eid) == "desconocido"
    fake = FakeLLM('{"kind": "organizacion", "confidence": 0.9, "rationale": "facultad"}')
    stats = await run_classify(1, client=fake)
    assert (stats.promoted, stats.pending) == (1, 0)
    assert _kind(eid) == "organizacion"


@pytest.mark.asyncio
async def test_low_confidence_stays_pending() -> None:
    eid = _seed_desconocido("alguien@acme.com", "Alguien")
    fake = FakeLLM('{"kind": "organizacion", "confidence": 0.4}')
    stats = await run_classify(1, client=fake, min_confidence=0.7)
    assert (stats.promoted, stats.pending) == (0, 1)
    assert _kind(eid) == "desconocido"  # no se forzó


@pytest.mark.asyncio
async def test_parse_error_stays_pending() -> None:
    eid = _seed_desconocido("otro@acme.com", "Otro")
    stats = await run_classify(1, client=FakeLLM("no es json"))
    assert stats.promoted == 0
    assert _kind(eid) == "desconocido"


@pytest.mark.asyncio
async def test_idempotent_rerun_noop() -> None:
    _seed_desconocido("tercero@acme.com", "Tercero")
    await run_classify(1, client=FakeLLM('{"kind": "organizacion", "confidence": 0.9}'))
    fake2 = FakeLLM('{"kind": "organizacion", "confidence": 0.9}')
    stats2 = await run_classify(1, client=fake2)  # ya no hay desconocido en el backlog
    assert stats2.items == 0 and fake2.calls == 0


@pytest.mark.asyncio
async def test_context_reaches_prompt() -> None:
    _seed_desconocido(
        "ielec@javeriana.edu.co", "Carrera de Ingeniería Electrónica", "Horario de lab"
    )

    class CapturingLLM(FakeLLM):
        def __init__(self, content: str) -> None:
            super().__init__(content)
            self.last_user = ""

        async def complete(
            self,
            messages: Sequence[ChatMessage],
            *,
            model: str | None = None,
            response_format: ResponseFormat = "text",
            temperature: float | None = None,
            max_tokens: int | None = None,
        ) -> LLMResult:
            self.last_user = messages[-1].content
            return await super().complete(messages)

    fake = CapturingLLM('{"kind": "desconocido", "confidence": 0.2}')
    await run_classify(1, client=fake)
    assert "javeriana.edu.co" in fake.last_user  # el dominio llega al prompt
    assert "Horario de lab" in fake.last_user  # el asunto del correo llega al prompt


@pytest.mark.asyncio
async def test_promotion_reweaves_edge_no_orphan() -> None:
    # al promover desconocido→org, la arista `afiliado` se re-proyecta bajo identidades:org y la
    # vieja (identidades:desconocido) NO queda huérfana (weave_afiliacion + reconcile_graph).
    _seed_desconocido("ielec@javeriana.edu.co", "Carrera de Ingeniería Electrónica")
    await run_classify(1, client=FakeLLM('{"kind": "organizacion", "confidence": 0.9}'))
    with connection() as c:
        edges = list_edges(c, 1, producer="identidades")
    src_slugs = {e.src.slug for e in edges}
    assert "identidades:desconocido" not in src_slugs
    assert "identidades:org" in src_slugs
