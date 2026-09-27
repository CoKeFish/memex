"""FallbackClient: disparo (cuota/red-5xx/timeout), no-disparo (4xx), agotamiento y auditoría."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from memex.llm.client import (
    ChatMessage,
    LLMError,
    LLMQuotaError,
    LLMResult,
    LLMUsage,
    ResponseFormat,
)
from memex.llm.fallback import FallbackClient, _should_fallback


def _ok(model: str = "deepseek-chat") -> LLMResult:
    return LLMResult(
        content="hola", model=model, usage=LLMUsage(0, 0, 0), cost_usd=Decimal(0), latency_ms=1
    )


class _Stub:
    """Cliente que devuelve un resultado fijo o levanta un error fijo; cuenta llamadas y cierre."""

    def __init__(self, *, result: LLMResult | None = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls = 0
        self.closed = False

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
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result

    async def aclose(self) -> None:
        self.closed = True


def _run(client: FallbackClient) -> LLMResult:
    return asyncio.run(client.complete([ChatMessage("user", "x")]))


@pytest.mark.parametrize(
    "error",
    [
        LLMQuotaError(402, "saldo agotado"),
        LLMError(0, "network/timeout"),
        LLMError(503, "server error"),
        LLMError(429, "rate"),
        LLMError(529, "overloaded"),
    ],
)
def test_falls_back_on_retryable_errors(error: Exception) -> None:
    primary = _Stub(error=error)
    backup = _Stub(result=_ok("claude-opus-4-8"))
    result = _run(FallbackClient([("deepseek", primary), ("anthropic", backup)]))
    assert result.model == "claude-opus-4-8"
    assert primary.calls == 1 and backup.calls == 1


def test_does_not_fall_back_on_non_quota_4xx() -> None:
    primary = _Stub(error=LLMError(400, "bad request"))
    backup = _Stub(result=_ok())
    with pytest.raises(LLMError) as ei:
        _run(FallbackClient([("deepseek", primary), ("anthropic", backup)]))
    assert ei.value.status_code == 400
    assert backup.calls == 0  # no se intentó el siguiente


def test_first_success_short_circuits() -> None:
    primary = _Stub(result=_ok("deepseek-chat"))
    backup = _Stub(result=_ok("claude-opus-4-8"))
    result = _run(FallbackClient([("deepseek", primary), ("anthropic", backup)]))
    assert result.model == "deepseek-chat"
    assert backup.calls == 0


def test_all_fail_raises_last() -> None:
    c1 = _Stub(error=LLMQuotaError(402, "no money"))
    c2 = _Stub(error=LLMError(503, "down"))
    with pytest.raises(LLMError) as ei:
        _run(FallbackClient([("deepseek", c1), ("anthropic", c2)]))
    assert ei.value.status_code == 503  # el último


def test_served_event_audits_provider_and_prior_failures(sink_capture: Any) -> None:
    primary = _Stub(error=LLMQuotaError(402, "saldo"))
    backup = _Stub(result=_ok("claude-opus-4-8"))
    _run(FallbackClient([("deepseek", primary), ("anthropic", backup)]))

    records = []
    while not sink_capture.empty():
        records.append(sink_capture.get_nowait())
    served = [r for r in records if r["event"] == "llm.fallback.served"]
    failed = [r for r in records if r["event"] == "llm.fallback.attempt_failed"]
    # los kwargs custom van serializados en la columna `fields` (JSON), no top-level
    assert served and failed
    served_f = json.loads(served[0]["fields"])
    failed_f = json.loads(failed[0]["fields"])
    assert served_f["provider"] == "anthropic" and served_f["prior_failures"] == ["deepseek"]
    assert failed_f["provider"] == "deepseek" and failed_f["error_class"] == "LLMQuotaError"


def test_aclose_closes_all_wrapped() -> None:
    c1, c2 = _Stub(result=_ok()), _Stub(result=_ok())
    asyncio.run(FallbackClient([("a", c1), ("b", c2)]).aclose())
    assert c1.closed and c2.closed


@pytest.mark.parametrize(
    ("err", "expected"),
    [
        (LLMQuotaError(402, "x"), True),
        (LLMError(0, "net"), True),
        (LLMError(503, "x"), True),
        (LLMError(429, "x"), True),
        (LLMError(529, "x"), True),
        (LLMError(400, "x"), False),
        (LLMError(404, "x"), False),
        (LLMError(422, "x"), False),
    ],
)
def test_should_fallback_classification(err: LLMError, expected: bool) -> None:
    assert _should_fallback(err) is expected


def test_empty_chain_rejected() -> None:
    with pytest.raises(ValueError, match="al menos un cliente"):
        FallbackClient([])
