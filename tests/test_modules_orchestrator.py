"""Orquestador de extracción contra la DB (sembrada), con un LLM falso (sin red).

Cubre: camino feliz, short-circuit del ruteo (1 módulo → sin LLM de ruteo), idempotencia,
descarte por atribución, pre-filtro consumes_kinds, módulo deshabilitado, best-effort, split de
ruteo en chunks (Etapa A) y extracción agrupada grouped/all (Etapa B).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from memex.core.deadletter import MAX_WORK_ATTEMPTS, STAGE_EXTRACT, list_review, requeue
from memex.db import connection
from memex.llm import ChatMessage, LLMError, LLMQuotaError, LLMResult, LLMUsage, ResponseFormat
from memex.modules.calendar.module import CalendarModule
from memex.modules.finance.module import FinanceModule
from memex.modules.grouping import GROUPED_SYSTEM_PROMPT
from memex.modules.orchestrator import run_extraction
from memex.modules.routing import ROUTING_SYSTEM_PROMPT

#: Marcador que precede al JSON de mensajes en TODOS los prompts (ruteo, agrupado, per_module).
_MESSAGES_MARKER = "Mensajes (JSON):\n"
#: system prompt de extracción per-module → slug (para que el fake emita el schema correcto).
_PROMPT_TO_SLUG = {
    FinanceModule.extraction_prompt: "finance",
    CalendarModule.extraction_prompt: "calendar",
}


def _item(slug: str, msg: dict[str, Any], bogus: int | None) -> dict[str, Any]:
    """Un item válido para el schema de `slug`, atribuido a `msg` (o a `bogus` si se fuerza)."""
    sid = bogus if bogus is not None else msg["id"]
    if slug == "calendar":
        return {
            "source_inbox_ids": [sid],
            "title": "Evento de prueba",
            "starts_on": "2026-06-01",
            "evidence": msg["text"],
        }
    return {
        "source_inbox_ids": [sid],
        "amount": f"{sid}.00",  # distinto por mensaje → vértices distintos (v2 no los colapsa)
        "currency": "ARS",
        "counterparty": "Test",
        "occurred_on": None,
        "description": "gasto de prueba",
        "evidence": msg["text"],
    }


class FakeExtractLLM:
    """Satisface LLMClient. Ramifica por el system prompt: ruteo → `{"modules":[...]}`; agrupado →
    `{"<slug>":[items]}`; per_module → `{"items":[...]}` con el schema del módulo.

    - `bogus_id`: cita un id fuera del lote (alucinación) en vez de los reales.
    - `empty`: devuelve content vacío (en CUALQUIER llamada).
    - `truncated`: emite `finish_reason="length"` (cortada por max_tokens), con content válido.
    - `fail_on_call`: lanza LLMError en la N-ésima llamada.
    - `quota_on_call`: lanza LLMQuotaError (402) en la N-ésima llamada (saldo agotado → aborta).
    - `route_choose`: slugs que el router elige (None = todos los del chunk).
    """

    def __init__(
        self,
        *,
        bogus_id: int | None = None,
        empty: bool = False,
        truncated: bool = False,
        fail_on_call: int | None = None,
        quota_on_call: int | None = None,
        route_choose: list[str] | None = None,
    ) -> None:
        self.calls = 0
        self._bogus = bogus_id
        self._empty = empty
        self._truncated = truncated
        self._fail_on = fail_on_call
        self._quota_on = quota_on_call
        self._route_choose = route_choose

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
        if self._fail_on is not None and self.calls == self._fail_on:
            raise LLMError(500, "boom")
        if self._quota_on is not None and self.calls == self._quota_on:
            raise LLMQuotaError(402, "insufficient balance")

        if self._empty:
            content = ""
        else:
            system = messages[0].content
            user = messages[-1].content
            if system == ROUTING_SYSTEM_PROMPT:
                content = self._route(user)
            elif system == GROUPED_SYSTEM_PROMPT:
                content = self._grouped(user)
            else:
                content = self._per_module(system, user)
        return LLMResult(
            content=content,
            model="fake",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="length" if self._truncated else "stop",
        )

    @staticmethod
    def _messages(user: str) -> list[dict[str, Any]]:
        parsed: list[dict[str, Any]] = json.loads(user.split(_MESSAGES_MARKER, 1)[1])
        return parsed

    def _route(self, user: str) -> str:
        catalog = user.split(_MESSAGES_MARKER, 1)[0]
        offered = [
            line[2 : line.index(":")] for line in catalog.splitlines() if line.startswith("- ")
        ]
        chosen = (
            offered
            if self._route_choose is None
            else [s for s in offered if s in self._route_choose]
        )
        return json.dumps({"modules": chosen})

    def _grouped(self, user: str) -> str:
        head, _, tail = user.partition(_MESSAGES_MARKER)
        slugs = [
            line[len("### Módulo: ") :].strip()
            for line in head.splitlines()
            if line.startswith("### Módulo: ")
        ]
        msgs: list[dict[str, Any]] = json.loads(tail)
        out = {slug: [_item(slug, m, self._bogus) for m in msgs] for slug in slugs}
        return json.dumps(out)

    def _per_module(self, system: str, user: str) -> str:
        slug = _PROMPT_TO_SLUG.get(system, "finance")
        return json.dumps({"items": [_item(slug, m, self._bogus) for m in self._messages(user)]})


# ----- helpers ------------------------------------------------------------------- #


def _seed(source_id: int, ext: str, tier: str, payload: dict[str, Any], minute: int = 0) -> int:
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
                "occ": datetime(2026, 5, 28, 12, minute, tzinfo=UTC),
                "p": json.dumps(payload),
            },
        ).scalar()
        c.execute(
            text("INSERT INTO classifications (user_id, inbox_id, tier) VALUES (1, :iid, :tier)"),
            {"iid": iid, "tier": tier},
        )
    assert iid is not None
    return int(iid)


def _new_source(name: str, source_type: str) -> int:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id"),
            {"n": name, "t": source_type},
        ).scalar()
    assert sid is not None
    return int(sid)


def _enable(slug: str = "finance") -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO module_settings (user_id, module_slug, enabled) "
                "VALUES (1, :slug, TRUE) "
                "ON CONFLICT (user_id, module_slug) DO UPDATE SET enabled = TRUE"
            ),
            {"slug": slug},
        )


def _count(table: str) -> int:
    with connection() as c:
        return int(c.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0)


def _count_purpose(purpose: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text("SELECT count(*) FROM llm_calls WHERE purpose = :p"), {"p": purpose}
            ).scalar()
            or 0
        )


# ----- camino feliz + short-circuit ---------------------------------------------- #


def test_extracts_expenses_from_batch_window(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]  # type 'imap' → EMAIL
    _enable()
    _seed(sid, "m1", "batch", {"subject": "luz", "body_text": "pagué $4500"}, minute=0)
    _seed(sid, "m2", "batch", {"subject": "agua", "body_text": "pagué $1200"}, minute=1)

    fake = FakeExtractLLM()
    stats = asyncio.run(run_extraction(1, client=fake))

    assert fake.calls == 1  # una ventana batch → una llamada de extracción
    assert stats.items == 2  # un gasto por mensaje
    assert _count("mod_finance_transactions") == 2
    assert _count("module_extractions") == 2


def test_short_circuit_no_routing_llm(seed_source: dict[str, Any]) -> None:
    _enable()
    _seed(seed_source["id"], "m1", "batch", {"body_text": "pagué $4500"})

    asyncio.run(run_extraction(1, client=FakeExtractLLM()))

    assert _count_purpose("module_route") == 0  # un solo módulo → no se gasta LLM en rutear
    assert _count_purpose("extract_finance") == 1


def test_individual_one_call_each(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    _enable()
    _seed(sid, "i1", "individual", {"body_text": "pagué $4500"})
    _seed(sid, "i2", "individual", {"body_text": "pagué $1200"})

    fake = FakeExtractLLM()
    stats = asyncio.run(run_extraction(1, client=fake))

    assert fake.calls == 2
    assert stats.items == 2


# ----- idempotencia -------------------------------------------------------------- #


def test_idempotent(seed_source: dict[str, Any]) -> None:
    _enable()
    _seed(seed_source["id"], "m1", "individual", {"body_text": "pagué $4500"})

    asyncio.run(run_extraction(1, client=FakeExtractLLM()))
    second = asyncio.run(run_extraction(1, client=FakeExtractLLM()))

    assert second.items == 0
    assert _count("mod_finance_transactions") == 1


# ----- atribución alucinada ------------------------------------------------------ #


def test_attribution_miss_discarded(seed_source: dict[str, Any]) -> None:
    _enable()
    _seed(seed_source["id"], "m1", "individual", {"body_text": "pagué $4500"})

    stats = asyncio.run(run_extraction(1, client=FakeExtractLLM(bogus_id=999999)))

    assert stats.items == 0
    assert stats.discarded == 1
    assert _count("mod_finance_transactions") == 0
    # el mensaje igual quedó marcado (considerado) → no se reprocesa en loop
    assert _count("module_extractions") == 1


# ----- pre-filtro consumes_kinds ------------------------------------------------- #


def test_social_source_excluded(seed_source: dict[str, Any]) -> None:
    """finance no consume SOCIAL → un post de instagram no entra al work-set."""
    ig = _new_source("ig", "instagram")
    _enable()
    _seed(ig, "p1", "batch", {"text": "mirá esta promo $999"})

    fake = FakeExtractLLM()
    stats = asyncio.run(run_extraction(1, client=fake))

    assert fake.calls == 0
    assert stats.items == 0
    assert _count("mod_finance_transactions") == 0


# ----- módulo deshabilitado ------------------------------------------------------ #


def test_disabled_module_no_work(seed_source: dict[str, Any]) -> None:
    _seed(seed_source["id"], "m1", "batch", {"body_text": "pagué $4500"})  # finance NO habilitado

    fake = FakeExtractLLM()
    stats = asyncio.run(run_extraction(1, client=fake))

    assert fake.calls == 0
    assert stats.items == 0


def test_blacklist_not_extracted(seed_source: dict[str, Any]) -> None:
    _enable()
    _seed(seed_source["id"], "m1", "blacklist", {"body_text": "promo"})

    fake = FakeExtractLLM()
    stats = asyncio.run(run_extraction(1, client=fake))

    assert fake.calls == 0
    assert stats.items == 0


# ----- best-effort --------------------------------------------------------------- #


def test_error_mid_run_is_best_effort(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    _enable()
    _seed(sid, "i1", "individual", {"body_text": "pagué $4500"})
    _seed(sid, "i2", "individual", {"body_text": "pagué $1200"})

    fake = FakeExtractLLM(fail_on_call=2)  # la 2da ventana explota
    stats = asyncio.run(run_extraction(1, client=fake))

    assert stats.items == 1
    assert stats.errors == 1
    assert _count("mod_finance_transactions") == 1
    # la ventana que falló sigue pendiente → otra corrida la procesa, sin duplicar la 1ra
    second = asyncio.run(run_extraction(1, client=FakeExtractLLM()))
    assert second.items == 1
    assert _count("mod_finance_transactions") == 2


def test_quota_error_aborts_run(seed_source: dict[str, Any]) -> None:
    """402/saldo agotado NO es best-effort: aborta la corrida (se propaga) y las ventanas
    restantes no se procesan. Lo ya persistido queda (no hay rollback global)."""
    sid = seed_source["id"]
    _enable()
    _seed(sid, "i1", "individual", {"body_text": "pagué $4500"}, minute=0)
    _seed(sid, "i2", "individual", {"body_text": "pagué $1200"}, minute=1)

    fake = FakeExtractLLM(quota_on_call=2)  # la 1ra ventana extrae; la 2da se queda sin saldo
    with pytest.raises(LLMQuotaError):
        asyncio.run(run_extraction(1, client=fake))

    assert fake.calls == 2  # abortó en la 2da, no siguió de largo
    assert _count("mod_finance_transactions") == 1  # la 1ra ventana sí persistió


# ----- dead-letter (gap c): veneno → 'pendiente de revisión' tras N fallos ------------- #


def test_poison_window_dead_lettered_after_max_attempts(seed_source: dict[str, Any]) -> None:
    """Una ventana que falla SIEMPRE: tras MAX_WORK_ATTEMPTS fallos el mensaje pasa a 'review' y el
    workset lo excluye. `requeue` lo devuelve y con un fake sano se extrae."""
    _enable()
    iid = _seed(seed_source["id"], "i1", "individual", {"body_text": "pagué $4500"})

    for _ in range(MAX_WORK_ATTEMPTS):  # cada corrida: 1 ventana, falla en la 1ra llamada
        stats = asyncio.run(run_extraction(1, client=FakeExtractLLM(fail_on_call=1)))
        assert stats.errors == 1

    # en 'review' → excluido del workset → no hay más trabajo
    after = asyncio.run(run_extraction(1, client=FakeExtractLLM(fail_on_call=1)))
    assert after.windows == 0
    assert iid in [it["inbox_id"] for it in list_review(1, STAGE_EXTRACT)]

    # requeue → vuelve al workset; con un fake sano se extrae
    assert requeue(1, STAGE_EXTRACT, iid) is True
    recovered = asyncio.run(run_extraction(1, client=FakeExtractLLM()))
    assert recovered.items == 1
    assert _count("mod_finance_transactions") == 1


def test_quota_abort_does_not_dead_letter(seed_source: dict[str, Any]) -> None:
    """El 402/saldo aborta la corrida pero NO cuenta como fallo de dead-letter: el mensaje no se
    manda a revisión (un saldo recargado debe poder reintentarlo)."""
    _enable()
    _seed(seed_source["id"], "i1", "individual", {"body_text": "pagué $4500"})

    with pytest.raises(LLMQuotaError):
        asyncio.run(run_extraction(1, client=FakeExtractLLM(quota_on_call=1)))

    assert list_review(1, STAGE_EXTRACT) == []


# ----- Etapa A: split de ruteo en chunks ----------------------------------------- #


def test_route_chunking_one_call_per_chunk(seed_source: dict[str, Any]) -> None:
    """Con route_chunk_size=1 y 2 candidatos → una llamada de ruteo por chunk (2), unión = ambos."""
    sid = seed_source["id"]
    _enable("finance")
    _enable("calendar")
    _seed(sid, "m1", "batch", {"body_text": "pagué $4500"}, minute=0)
    _seed(sid, "m2", "batch", {"body_text": "pagué $1200"}, minute=1)

    fake = FakeExtractLLM()
    stats = asyncio.run(run_extraction(1, route_chunk_size=1, client=fake))

    assert _count_purpose("module_route") == 2  # un chunk por módulo
    assert stats.items == 4  # finance 2 + calendar 2 (chunking del ruteo, no de la extracción)
    assert _count("mod_finance_transactions") == 2
    assert _count("mod_calendar_events") == 2


def test_no_chunking_single_route_call(seed_source: dict[str, Any]) -> None:
    """Default (sin chunk) con 2 candidatos → una sola llamada de ruteo."""
    _enable("finance")
    _enable("calendar")
    _seed(seed_source["id"], "m1", "batch", {"body_text": "pagué $4500"})

    asyncio.run(run_extraction(1, client=FakeExtractLLM()))

    assert _count_purpose("module_route") == 1


# ----- Etapa B: extracción agrupada (grouped / all) ------------------------------ #


def test_grouped_single_extraction_call(seed_source: dict[str, Any]) -> None:
    """grouped: finance+calendar en UNA llamada (`extract_grouped`), persistencia y cursor por
    módulo en su propia tx."""
    sid = seed_source["id"]
    _enable("finance")
    _enable("calendar")
    _seed(sid, "m1", "batch", {"body_text": "pagué $4500"}, minute=0)
    _seed(sid, "m2", "batch", {"body_text": "pagué $1200"}, minute=1)

    stats = asyncio.run(
        run_extraction(1, batching_policy="grouped", group_size=2, client=FakeExtractLLM())
    )

    assert _count_purpose("extract_grouped") == 1
    assert _count_purpose("extract_finance") == 0
    assert _count_purpose("extract_calendar") == 0
    assert _count("mod_finance_transactions") == 2
    assert _count("mod_calendar_events") == 2
    assert _count("module_extractions") == 4  # 2 mensajes x 2 módulos
    assert set(stats.by_module) == {"finance", "calendar"}


def test_default_policy_groups_one_call(seed_source: dict[str, Any]) -> None:
    """DEFAULT (sin pasar batching_policy) = grouped: un correo individual con 2 módulos elegidos se
    extrae en UNA sola llamada `extract_grouped`, NO una por módulo. Lockea la invariante (el
    benchmark muestra que separar por módulo reenvía el correo N veces → más caro)."""
    sid = seed_source["id"]
    _enable("finance")
    _enable("calendar")
    _seed(sid, "i1", "individual", {"body_text": "pagué $4500"})

    asyncio.run(run_extraction(1, client=FakeExtractLLM()))

    assert _count_purpose("extract_grouped") == 1
    assert _count_purpose("extract_finance") == 0
    assert _count_purpose("extract_calendar") == 0


def test_per_module_opt_in_splits_calls(seed_source: dict[str, Any]) -> None:
    """`per_module` NO se quitó: como opción explícita parte en una llamada por módulo
    (`extract_<slug>`), sin `extract_grouped`."""
    sid = seed_source["id"]
    _enable("finance")
    _enable("calendar")
    _seed(sid, "i1", "individual", {"body_text": "pagué $4500"})

    asyncio.run(run_extraction(1, batching_policy="per_module", client=FakeExtractLLM()))

    assert _count_purpose("extract_finance") == 1
    assert _count_purpose("extract_calendar") == 1
    assert _count_purpose("extract_grouped") == 0


def test_all_policy_single_group(seed_source: dict[str, Any]) -> None:
    _enable("finance")
    _enable("calendar")
    _seed(seed_source["id"], "m1", "batch", {"body_text": "pagué $4500"})

    asyncio.run(run_extraction(1, batching_policy="all", client=FakeExtractLLM()))

    assert _count_purpose("extract_grouped") == 1
    assert _count("mod_finance_transactions") == 1
    assert _count("mod_calendar_events") == 1


def test_grouped_idempotent(seed_source: dict[str, Any]) -> None:
    _enable("finance")
    _enable("calendar")
    _seed(seed_source["id"], "m1", "batch", {"body_text": "pagué $4500"})

    asyncio.run(run_extraction(1, batching_policy="grouped", group_size=2, client=FakeExtractLLM()))
    second = asyncio.run(
        run_extraction(1, batching_policy="grouped", group_size=2, client=FakeExtractLLM())
    )

    assert second.items == 0
    assert _count("mod_finance_transactions") == 1
    assert _count("mod_calendar_events") == 1


def test_grouped_empty_content_retryable(seed_source: dict[str, Any]) -> None:
    """Content vacío en la llamada agrupada → error, sin cursor (reintentable)."""
    _enable("finance")
    _enable("calendar")
    _seed(seed_source["id"], "m1", "batch", {"body_text": "pagué $4500"})

    stats = asyncio.run(
        run_extraction(
            1, batching_policy="grouped", group_size=2, client=FakeExtractLLM(empty=True)
        )
    )

    assert stats.errors == 1
    assert _count("mod_finance_transactions") == 0
    assert _count("module_extractions") == 0  # sin cursor → reintentable


# ----- truncado (finish_reason != stop) → reintentable, sin pérdida silenciosa --------- #


def test_truncated_extract_retryable(seed_source: dict[str, Any]) -> None:
    """Respuesta truncada (finish_reason='length') → error, NO se cursorea (reintentable). El bug
    era: parse_items da [] sobre el JSON cortado y se cursoreaba '0 items' (pérdida silenciosa)."""
    _enable()
    _seed(seed_source["id"], "m1", "individual", {"body_text": "pagué $4500"})

    stats = asyncio.run(run_extraction(1, client=FakeExtractLLM(truncated=True)))

    assert stats.errors == 1
    assert stats.items == 0
    assert _count("mod_finance_transactions") == 0
    assert _count("module_extractions") == 0  # sin cursor → reintentable

    # Segunda corrida con respuesta completa → se extrae (prueba que NO se perdió el mensaje).
    second = asyncio.run(run_extraction(1, client=FakeExtractLLM()))
    assert second.items == 1
    assert _count("mod_finance_transactions") == 1


def test_truncated_grouped_retryable(seed_source: dict[str, Any]) -> None:
    """Truncado en la llamada agrupada → ningún módulo del grupo se cursorea (reintentable)."""
    _enable("finance")
    _enable("calendar")
    _seed(seed_source["id"], "m1", "batch", {"body_text": "pagué $4500"})

    stats = asyncio.run(
        run_extraction(
            1, batching_policy="grouped", group_size=2, client=FakeExtractLLM(truncated=True)
        )
    )

    assert stats.errors == 1
    assert _count("mod_finance_transactions") == 0
    assert _count("mod_calendar_events") == 0
    assert _count("module_extractions") == 0  # sin cursor → reintentable


# ----- seam de identidad a través del orquestador (optional_deps + FASE 2 topo) -------- #


def _seeded_identity(name: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_identidades (user_id, kind, display_name) "
                    "VALUES (1, 'organizacion', :n) RETURNING id"
                ),
                {"n": name},
            ).scalar_one()
        )


def _finance_fk() -> int | None:
    with connection() as c:
        val = c.execute(
            text("SELECT counterparty_identity_id FROM mod_finance_transactions WHERE user_id = 1")
        ).scalar_one()
    return int(val) if val is not None else None


def test_orchestrator_resolves_counterparty_identity(seed_source: dict[str, Any]) -> None:
    """End-to-end: con identidades ACTIVO, finanzas recibe su handle (`ctx.deps`, vía
    `optional_deps`) en FASE 2 y persiste el FK de la contraparte. `route_choose=['finance']`: no se
    extrae en esta ventana pero igual provee el dominio (handle ligado a active_by_slug)."""
    _enable("finance")
    _enable("identidades")
    oid = _seeded_identity("Test")  # el fake emite counterparty="Test"
    _seed(seed_source["id"], "m1", "individual", {"body_text": "pagué $4500"})

    asyncio.run(run_extraction(1, client=FakeExtractLLM(route_choose=["finance"])))

    assert _finance_fk() == oid


def test_orchestrator_finance_runs_without_identidades(seed_source: dict[str, Any]) -> None:
    """Dependencia BLANDA: identidades APAGADO → finanzas corre igual (no se dropea) y el FK queda
    NULL aunque la identidad exista (sin handle no se resuelve; el dedup cae al texto)."""
    _enable("finance")  # identidades NO habilitado
    _seeded_identity("Test")
    _seed(seed_source["id"], "m1", "individual", {"body_text": "pagué $4500"})

    stats = asyncio.run(run_extraction(1, client=FakeExtractLLM()))

    assert stats.items == 1
    assert _finance_fk() is None


def test_read_extractions_debug_exposes_finance_seam(seed_source: dict[str, Any]) -> None:
    """read_extractions_debug (vista DEBUG): expone el estado INTERNO de finance — la contraparte
    resuelta a identidad + el outcome de dedup. Solo módulos con CAP_DEBUG_INBOX (calendar/
    hackathones NO aparecen aunque estén registrados)."""
    from memex.modules.orchestrator import read_extractions_debug

    _enable("finance")
    _enable("identidades")
    oid = _seeded_identity("Test")
    iid = _seed(seed_source["id"], "m1", "individual", {"body_text": "pagué $4500"})
    asyncio.run(run_extraction(1, client=FakeExtractLLM(route_choose=["finance"])))

    debug = read_extractions_debug(1, iid)
    assert "calendar" not in debug and "hackathones" not in debug  # no declaran CAP_DEBUG_INBOX
    assert len(debug["finance"]) == 1
    row = debug["finance"][0]
    assert row["counterparty_identity_id"] == oid
    assert row["counterparty_identity_name"] == "Test"
    assert row["processing_outcome"] in {"pending", "unique", "duplicate"}
    assert row["dedup_candidates"] == []  # único movimiento → sin pares (la query igual corre)


def test_identidades_debug_for_inbox_exposes_resolution_and_merge() -> None:
    """IdentidadesModule.debug_for_inbox: por mención, método de resolución, identidad resuelta y
    los candidatos de merge que la tocan (con el nombre de la otra). Ejercita el SQL completo."""
    from memex.modules.identidades.module import IdentidadesModule

    with connection() as c:
        a = c.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1,'organizacion','Acme') RETURNING id"
            )
        ).scalar_one()
        b = c.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1,'organizacion','Acme Inc') RETURNING id"
            )
        ).scalar_one()
        lo, hi = sorted((int(a), int(b)))
        c.execute(
            text(
                """INSERT INTO mod_identidades_merge_candidates
                   (user_id, identity_a_id, identity_b_id, reason, score, status)
                   VALUES (1, :lo, :hi, 'trgm_name', 0.7, 'candidate')"""
            ),
            {"lo": lo, "hi": hi},
        )
        c.execute(
            text(
                """INSERT INTO mod_identidades_mentions
                   (user_id, source_inbox_ids, mentioned_name, mentioned_kind,
                    resolved_kind, resolved_identity_id, resolution_method)
                   VALUES (1, ARRAY[55]::bigint[], 'Acme', 'organizacion',
                           'organizacion', :rid, 'fuzzy')"""
            ),
            {"rid": lo},
        )

    with connection() as c:
        rows = IdentidadesModule().debug_for_inbox(c, 1, [55])

    assert len(rows) == 1
    m = rows[0]
    assert m["resolution_method"] == "fuzzy"
    assert m["resolved_identity_id"] == lo
    assert len(m["merge_candidates"]) == 1
    cand = m["merge_candidates"][0]
    assert cand["other_identity_id"] == hi
    assert cand["other_identity_name"] == "Acme Inc"
    assert cand["status"] == "candidate"
    assert cand["score"] == 0.7


def test_read_extractions_de_hardcoded_returns_all_module_keys() -> None:
    """read_extractions itera el registry (de-hardcodeado): TODAS las claves de módulo presentes
    —incluida identidades, que antes no aparecía— aun sin datos, para no soltar una clave que el
    front lee. `done=False` cuando ningún módulo corrió sobre el mensaje."""
    from memex.modules import known_modules
    from memex.modules.orchestrator import read_extractions

    ext = read_extractions(1, 999_999)

    assert ext["done"] is False
    assert ext["modules"] == []
    for slug in known_modules():
        assert ext[slug] == [], f"la clave {slug} debe existir y venir vacía"
    assert {"finance", "calendar", "hackathones", "identidades"} <= set(ext)
