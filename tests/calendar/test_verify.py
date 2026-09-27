"""Segunda verificación de fechas de calendar (`calendar/verify.py`): regla de los 9 meses contra
la recepción, parseo del juez y aplicación de su veredicto (fix / keep / discard / error)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from memex.llm import ChatMessage, LLMError, LLMResult, LLMUsage, ResponseFormat
from memex.modules.calendar.schema import CalendarEventItem
from memex.modules.calendar.verify import (
    PURPOSE,
    VERIFY_SYSTEM_PROMPT,
    needs_second_look,
    parse_verdict,
    verify_dates,
)
from memex.modules.contract import VerifyContext

_RECEIVED = date(2026, 5, 28)


# ----- regla de disparo ------------------------------------------------------------ #


def test_needs_second_look_nine_months_either_side() -> None:
    assert needs_second_look(date(2025, 6, 5), _RECEIVED)  # ~12 meses atrás
    assert needs_second_look(date(2025, 8, 28), _RECEIVED)  # exactamente 9 meses atrás
    assert needs_second_look(date(2027, 2, 28), _RECEIVED)  # exactamente 9 meses adelante
    assert needs_second_look(date(2027, 6, 1), _RECEIVED)  # 12 meses adelante


def test_needs_second_look_inside_window_is_false() -> None:
    assert not needs_second_look(date(2026, 6, 5), _RECEIVED)  # la semana siguiente
    assert not needs_second_look(date(2025, 8, 29), _RECEIVED)  # 9 meses menos un día
    assert not needs_second_look(date(2027, 2, 27), _RECEIVED)
    assert not needs_second_look(date(2026, 1, 20), _RECEIVED)  # recap de 4 meses atrás


# ----- parseo del juez ------------------------------------------------------------- #


def test_parse_verdict_fix_and_fallbacks() -> None:
    v = parse_verdict(
        '{"verdict": "fix", "starts_on": "2026-06-05", "ends_on": null, '
        '"confidence": 0.9, "rationale": "plantilla vieja"}'
    )
    assert (v.verdict, v.starts_on, v.ends_on, v.confidence) == ("fix", date(2026, 6, 5), None, 0.9)
    assert parse_verdict("no es json").verdict == "keep"
    assert parse_verdict('{"verdict": "fix", "confidence": 0.9}').verdict == "keep"  # fix sin fecha
    assert parse_verdict('{"verdict": "raro", "confidence": 2}').confidence == 1.0


# ----- aplicación del veredicto ------------------------------------------------------ #


class _Judge:
    """LLM falso: responde `content` fijo a la pregunta del juez; falla si `error`."""

    def __init__(self, content: str, *, error: bool = False) -> None:
        self.content = content
        self.error = error
        self.calls: list[str] = []

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        assert messages[0].content == VERIFY_SYSTEM_PROMPT
        self.calls.append(messages[-1].content)
        if self.error:
            raise LLMError(500, "boom")
        return LLMResult(
            content=self.content,
            model="fake",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=1, total_tokens=11),
            cost_usd=Decimal("0"),
            latency_ms=1,
        )


def _ctx(llm: _Judge, text: str = "Fecha: Viernes, 5 de junio de 2025") -> VerifyContext:
    return VerifyContext(
        user_id=1,
        client_for=lambda consumer: _consumer_client(consumer, llm),
        occurred_at_by_id={7: datetime(2026, 5, 28, 15, 0, tzinfo=UTC)},
        rendered_by_id={7: text},
    )


def _consumer_client(consumer: str, llm: _Judge) -> _Judge:
    assert (
        consumer == PURPOSE
    )  # el juez pide SU consumidor (codex por registro), no el de extracción
    return llm


def _event(starts_on: date, **over: object) -> CalendarEventItem:
    base: dict[str, object] = {
        "source_inbox_ids": (7,),
        "title": "Ingeniería Project Fest 2610",
        "starts_on": starts_on,
        "evidence": "Fecha: Viernes, 5 de junio de 2025",
    }
    base.update(over)
    return CalendarEventItem(**base)


def _cal(item: object) -> CalendarEventItem:
    assert isinstance(item, CalendarEventItem)
    return item


def _fix(starts_on: str, conf: float = 0.9) -> str:
    return json.dumps(
        {
            "verdict": "fix",
            "starts_on": starts_on,
            "ends_on": None,
            "confidence": conf,
            "rationale": "plantilla reciclada",
        }
    )


def test_far_date_is_judged_and_fixed() -> None:
    judge = _Judge(_fix("2026-06-05"))
    out = asyncio.run(verify_dates([_event(date(2025, 6, 5))], _ctx(judge)))
    assert len(judge.calls) == 1
    assert "enviado: 2026-05-28" in judge.calls[0]
    assert "Viernes, 5 de junio de 2025" in judge.calls[0]  # el juez ve el mensaje
    item = out.items[0]
    assert isinstance(item, CalendarEventItem)
    assert item.starts_on == date(2026, 6, 5)
    assert item.verification is not None
    assert (item.verification.original, item.verification.corrected) == (
        "2025-06-05",
        "2026-06-05",
    )
    assert [c.purpose for c in out.calls] == [PURPOSE]
    assert out.calls[0].metadata["applied"] == "fix"


def test_near_date_is_not_judged() -> None:
    judge = _Judge(_fix("2026-06-05"))
    out = asyncio.run(verify_dates([_event(date(2026, 6, 5))], _ctx(judge)))
    assert judge.calls == []
    assert out.calls == []
    assert out.items[0].verification is None


def test_keep_leaves_item_untouched() -> None:
    judge = _Judge('{"verdict": "keep", "confidence": 0.8, "rationale": "congreso anunciado"}')
    out = asyncio.run(verify_dates([_event(date(2027, 11, 23))], _ctx(judge)))
    assert _cal(out.items[0]).starts_on == date(2027, 11, 23)
    assert out.items[0].verification is None
    assert len(out.calls) == 1


def test_low_confidence_fix_is_kept() -> None:
    judge = _Judge(_fix("2026-06-05", conf=0.3))
    out = asyncio.run(verify_dates([_event(date(2025, 6, 5))], _ctx(judge)))
    assert _cal(out.items[0]).starts_on == date(2025, 6, 5)
    assert out.calls[0].metadata["applied"] == "keep"


def test_discard_drops_item() -> None:
    judge = _Judge('{"verdict": "discard", "confidence": 0.9, "rationale": "no es una fecha"}')
    out = asyncio.run(verify_dates([_event(date(2025, 6, 5))], _ctx(judge)))
    assert out.items == []
    assert len(out.calls) == 1


def test_llm_error_is_best_effort() -> None:
    judge = _Judge("", error=True)
    out = asyncio.run(verify_dates([_event(date(2025, 6, 5))], _ctx(judge)))
    assert _cal(out.items[0]).starts_on == date(2025, 6, 5)
    assert out.calls == []


def test_ends_on_follows_fix() -> None:
    judge = _Judge(
        json.dumps(
            {
                "verdict": "fix",
                "starts_on": "2026-06-05",
                "ends_on": "2026-06-07",
                "confidence": 0.95,
                "rationale": "año viejo",
            }
        )
    )
    out = asyncio.run(
        verify_dates([_event(date(2025, 6, 5), ends_on=date(2025, 6, 7))], _ctx(judge))
    )
    item = out.items[0]
    assert isinstance(item, CalendarEventItem)
    assert (item.starts_on, item.ends_on) == (date(2026, 6, 5), date(2026, 6, 7))
