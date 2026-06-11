"""Perilla `llm_on_past_events` (module_settings.config): default, upsert y el GATE de gasto.

Con la perilla APAGADA (default) los pasos que GASTAN LLM (dedup FASE 2 y merge) saltean los
pares/grupos ya vencidos — quedan tal cual y se retoman al prenderla. Lo determinista no mira
la perilla.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.modules.calendar.cli import main as cal_main
from memex.modules.calendar.dedup_llm import run_dedup_phase2
from memex.modules.calendar.merge_llm import run_merge
from memex.modules.calendar.settings import llm_on_past_events, set_llm_on_past_events

_PAST = date.today() - timedelta(days=10)
_FUTURE = date.today() + timedelta(days=10)


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


def _set_flag(value: bool) -> None:
    with connection() as c:
        set_llm_on_past_events(c, 1, value)


def _seed_event(title: str, *, starts_on: date) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_calendar_events "
                    "(user_id, source_inbox_ids, title, starts_on) "
                    "VALUES (1, ARRAY[]::bigint[], :t, :d) RETURNING id"
                ),
                {"t": title, "d": starts_on},
            ).scalar_one()
        )


def _seed_pair(a: int, b: int) -> None:
    lo, hi = min(a, b), max(a, b)
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_calendar_dedup_candidates "
                "(user_id, event_a_id, event_b_id, reason, score) "
                "VALUES (1, :a, :b, 'time+title', 0.9)"
            ),
            {"a": lo, "b": hi},
        )


def _pair_status(a: int, b: int) -> str:
    lo, hi = min(a, b), max(a, b)
    with connection() as c:
        return str(
            c.execute(
                text(
                    "SELECT status FROM mod_calendar_dedup_candidates "
                    "WHERE event_a_id = :a AND event_b_id = :b"
                ),
                {"a": lo, "b": hi},
            ).scalar_one()
        )


def _seed_merge_group(*, starts_on: date) -> int:
    a = _seed_event("Evento A", starts_on=starts_on)
    b = _seed_event("Evento A bis", starts_on=starts_on)
    with connection() as c:
        cons = int(
            c.execute(
                text(
                    "INSERT INTO mod_calendar_consolidated "
                    "(user_id, title, starts_on, winner_event_id) "
                    "VALUES (1, 'Evento A', :d, :w) RETURNING id"
                ),
                {"d": starts_on, "w": a},
            ).scalar_one()
        )
        for eid in (a, b):
            c.execute(
                text(
                    "INSERT INTO mod_calendar_event_links (user_id, consolidated_id, event_id) "
                    "VALUES (1, :c, :e)"
                ),
                {"c": cons, "e": eid},
            )
    return cons


# ----- helper get/set --------------------------------------------------------------- #


def test_default_is_off() -> None:
    with connection() as c:
        assert llm_on_past_events(c, 1) is False  # sin fila / sin clave → no gastar


def test_set_and_read_back() -> None:
    _set_flag(True)
    with connection() as c:
        assert llm_on_past_events(c, 1) is True
    _set_flag(False)
    with connection() as c:
        assert llm_on_past_events(c, 1) is False


def test_set_preserves_existing_module_row() -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO module_settings (user_id, module_slug, enabled, config) "
                "VALUES (1, 'calendar', TRUE, CAST('{\"otra\": 1}' AS JSONB))"
            )
        )
    _set_flag(True)
    with connection() as c:
        row = c.execute(
            text(
                "SELECT enabled, config FROM module_settings "
                "WHERE user_id = 1 AND module_slug = 'calendar'"
            )
        ).first()
    assert row is not None
    assert row[0] is True  # enabled intacto
    assert row[1]["otra"] == 1  # el resto del config no se pisa
    assert row[1]["llm_on_past_events"] is True


# ----- gate del dedup FASE 2 --------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dedup2_skips_past_pairs_when_off() -> None:
    past_a = _seed_event("Taller viejo", starts_on=_PAST)
    past_b = _seed_event("Taller viejo bis", starts_on=_PAST)
    fut_a = _seed_event("Cita próxima", starts_on=_FUTURE)
    fut_b = _seed_event("Cita próxima bis", starts_on=_FUTURE)
    _seed_pair(past_a, past_b)
    _seed_pair(fut_a, fut_b)

    fake = FakeLLM('{"same": true, "confidence": 0.9, "rationale": "mismo"}')
    stats = await run_dedup_phase2(1, client=fake)

    assert fake.calls == 1  # solo el par vigente gastó LLM
    assert stats.pairs == 1
    assert _pair_status(fut_a, fut_b) == "confirmed"
    assert _pair_status(past_a, past_b) == "candidate"  # queda pendiente, sin gasto


@pytest.mark.asyncio
async def test_dedup2_judges_past_pairs_when_on() -> None:
    _set_flag(True)
    past_a = _seed_event("Taller viejo", starts_on=_PAST)
    past_b = _seed_event("Taller viejo bis", starts_on=_PAST)
    _seed_pair(past_a, past_b)

    fake = FakeLLM('{"same": true, "confidence": 0.9, "rationale": "mismo"}')
    await run_dedup_phase2(1, client=fake)

    assert fake.calls == 1
    assert _pair_status(past_a, past_b) == "confirmed"


@pytest.mark.asyncio
async def test_dedup2_pair_current_if_one_side_future() -> None:
    # Multi-día que termina en el futuro: NO está vencido aunque arranque en el pasado.
    a = _seed_event("Conferencia larga", starts_on=_PAST)
    with connection() as c:
        c.execute(
            text("UPDATE mod_calendar_events SET ends_on = :e WHERE id = :i"),
            {"e": _FUTURE, "i": a},
        )
    b = _seed_event("Conferencia larga bis", starts_on=_PAST)
    _seed_pair(a, b)

    fake = FakeLLM('{"same": true, "confidence": 0.9, "rationale": "mismo"}')
    await run_dedup_phase2(1, client=fake)
    assert fake.calls == 1  # un lado sigue vigente → se juzga


# ----- gate del merge ----------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_merge_skips_past_groups_when_off() -> None:
    _seed_merge_group(starts_on=_PAST)
    fut_cons = _seed_merge_group(starts_on=_FUTURE)

    fake = FakeLLM('{"title": "Evento A", "location": "", "description": ""}')
    stats = await run_merge(1, client=fake)

    assert fake.calls == 1  # solo el grupo vigente
    assert stats.consolidated == 1
    with connection() as c:
        sig = c.execute(
            text("SELECT merge_signature FROM mod_calendar_consolidated WHERE id = :i"),
            {"i": fut_cons},
        ).scalar()
    assert sig is not None


@pytest.mark.asyncio
async def test_merge_processes_past_groups_when_on() -> None:
    _set_flag(True)
    _seed_merge_group(starts_on=_PAST)
    fake = FakeLLM('{"title": "Evento A", "location": "", "description": ""}')
    stats = await run_merge(1, client=fake)
    assert fake.calls == 1
    assert stats.merged == 1


# ----- CLI set-llm-past ---------------------------------------------------------------- #


def test_cli_set_llm_past(capsys: pytest.CaptureFixture[str]) -> None:
    assert cal_main(["set-llm-past", "on", "--json"]) == 0
    out: Any = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["llm_on_past_events"] is True
    with connection() as c:
        assert llm_on_past_events(c, 1) is True

    assert cal_main(["set-llm-past", "off", "--json"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["llm_on_past_events"] is False
