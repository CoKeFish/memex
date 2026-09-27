"""Streaming + herramientas para APIs OpenAI-compatible (`/chat/completions` con `stream: true`).

Compartido por `DeepSeekClient` y `OpenAIClient` (mismo dialecto SSE): cada línea `data: {...}`
trae un `choices[0].delta` con pedazos de `content` y/o fragmentos de `tool_calls` indexados (el
`id`/`name` vienen en el primer fragmento, los `arguments` gotean en JSON parcial); el último chunk
trae `finish_reason` y, con `stream_options.include_usage`, el `usage`.

`ChunkAccumulator` es PURO (testeable sin red): recibe chunks ya parseados y emite los eventos de
`memex.llm.tools`. `stream_openai_tools` es la parte HTTP: abre el stream con el mismo criterio de
retry que `complete` (429/5xx/red ANTES del primer byte; 402 → `LLMQuotaError`) y traduce líneas
SSE a eventos.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from memex.llm._openai import parse_usage
from memex.llm.client import LLMError, LLMQuotaError, LLMUsage
from memex.llm.pricing import ModelPricing, compute_cost
from memex.llm.tools import StreamDone, StreamEvent, TextDelta, ToolCall, ToolCallsEvent

_BODY_PREVIEW_MAX = 500


@dataclass
class _PartialCall:
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class ChunkAccumulator:
    """Junta los deltas de un stream OpenAI-compatible y produce eventos."""

    model: str
    text_parts: list[str] = field(default_factory=list)
    calls: dict[int, _PartialCall] = field(default_factory=dict)
    finish_reason: str | None = None
    usage: LLMUsage | None = None

    def feed(self, chunk: Any) -> list[StreamEvent]:
        """Un chunk (dict) → 0..1 `TextDelta`. Guarda tool_calls/finish/usage para `finish()`."""
        if not isinstance(chunk, dict):
            return []
        if isinstance(chunk.get("usage"), dict):
            self.usage = parse_usage(chunk["usage"])
        if isinstance(chunk.get("model"), str) and chunk["model"]:
            self.model = str(chunk["model"])
        choices = chunk.get("choices")
        if not (isinstance(choices, list) and choices and isinstance(choices[0], dict)):
            return []
        first: dict[str, Any] = choices[0]
        if isinstance(first.get("finish_reason"), str):
            self.finish_reason = first["finish_reason"]
        delta = first.get("delta")
        if not isinstance(delta, dict):
            return []
        out: list[StreamEvent] = []
        content = delta.get("content")
        if isinstance(content, str) and content:
            self.text_parts.append(content)
            out.append(TextDelta(content))
        raw_calls = delta.get("tool_calls")
        if isinstance(raw_calls, list):
            for frag in raw_calls:
                if not isinstance(frag, dict):
                    continue
                idx = frag.get("index")
                index = int(idx) if isinstance(idx, int) else len(self.calls)
                partial = self.calls.setdefault(index, _PartialCall())
                if isinstance(frag.get("id"), str) and frag["id"]:
                    partial.id = frag["id"]
                fn = frag.get("function")
                if isinstance(fn, dict):
                    if isinstance(fn.get("name"), str) and fn["name"] and not partial.name:
                        partial.name = fn["name"]
                    if isinstance(fn.get("arguments"), str):
                        partial.arguments += fn["arguments"]
        return out

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    def tool_calls(self) -> tuple[ToolCall, ...]:
        calls: list[ToolCall] = []
        for index in sorted(self.calls):
            p = self.calls[index]
            if not p.name:
                continue
            calls.append(
                ToolCall(
                    id=p.id or f"call_{index}", name=p.name, arguments=parse_arguments(p.arguments)
                )
            )
        return tuple(calls)

    def finish(self, *, latency_ms: int, pricing: Mapping[str, ModelPricing]) -> list[StreamEvent]:
        """Los eventos de cierre: `ToolCallsEvent` si hubo llamadas y SIEMPRE un `StreamDone`."""
        out: list[StreamEvent] = []
        calls = self.tool_calls()
        if calls:
            out.append(ToolCallsEvent(calls))
        usage = self.usage or LLMUsage(0, 0, 0)
        cost = compute_cost(self.model, usage, pricing=pricing, at=datetime.now(UTC))
        out.append(
            StreamDone(
                model=self.model,
                usage=usage,
                cost_usd=cost,
                latency_ms=latency_ms,
                finish_reason=self.finish_reason,
                text=self.text,
                tool_calls=calls,
            )
        )
        return out


def parse_arguments(raw: str) -> Mapping[str, Any]:
    """JSON de argumentos → dict; vacío → {}; inválido → {"_raw": texto} (el caller decide)."""
    s = raw.strip()
    if not s:
        return {}
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return data if isinstance(data, dict) else {"_raw": raw}


def parse_sse_line(line: str) -> Any | None:
    """`data: {...}` → dict; `data: [DONE]`, vacías y comentarios → None."""
    s = line.strip()
    if not s.startswith("data:"):
        return None
    payload = s[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


async def stream_openai_tools(
    http: httpx.AsyncClient,
    *,
    path: str,
    body: dict[str, Any],
    model: str,
    pricing: Mapping[str, ModelPricing],
    error_cls: type[LLMError],
    max_retries: int,
    backoff_base: float,
    log: Any,
    log_event: str,
) -> AsyncIterator[StreamEvent]:
    """Abre `POST path` en streaming y traduce SSE → eventos. Retry (429/5xx/red) solo ANTES de que
    llegue el primer byte; un corte a mitad del stream levanta `error_cls` (el caller decide)."""
    body = {**body, "stream": True, "stream_options": {"include_usage": True}}
    started = time.monotonic()
    acc = ChunkAccumulator(model=model)
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            async with http.stream("POST", path, json=body) as resp:
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    preview = (await resp.aread()).decode("utf-8", errors="replace")
                    last_exc = error_cls(
                        resp.status_code,
                        f"server/rate error {resp.status_code}",
                        body=preview[:_BODY_PREVIEW_MAX] or None,
                    )
                    log.warning(f"{log_event}.retryable", status=resp.status_code, attempt=attempt)
                elif resp.status_code == 402:
                    preview = (await resp.aread()).decode("utf-8", errors="replace")
                    raise LLMQuotaError(
                        402, "insufficient balance", body=preview[:_BODY_PREVIEW_MAX] or None
                    )
                elif 400 <= resp.status_code < 500:
                    preview = (await resp.aread()).decode("utf-8", errors="replace")
                    raise error_cls(
                        resp.status_code,
                        f"client error {resp.status_code}",
                        body=preview[:_BODY_PREVIEW_MAX] or None,
                    )
                else:
                    async for line in resp.aiter_lines():
                        chunk = parse_sse_line(line)
                        if chunk is None:
                            continue
                        for ev in acc.feed(chunk):
                            yield ev
                    latency_ms = int((time.monotonic() - started) * 1000)
                    done_events = acc.finish(latency_ms=latency_ms, pricing=pricing)
                    done = done_events[-1]
                    assert isinstance(done, StreamDone)
                    log.info(
                        log_event,
                        model=done.model,
                        prompt_tokens=done.usage.prompt_tokens,
                        completion_tokens=done.usage.completion_tokens,
                        cache_hit_tokens=done.usage.cache_hit_tokens,
                        cost_usd=str(done.cost_usd),
                        latency_ms=latency_ms,
                        finish_reason=done.finish_reason,
                        tool_calls=len(done.tool_calls),
                        chars=len(done.text),
                    )
                    for ev in done_events:
                        yield ev
                    return
        except (httpx.TransportError, httpx.TimeoutException) as e:
            if acc.text_parts or acc.calls:
                # Ya habíamos emitido parte de la respuesta: no se puede reintentar en silencio.
                raise error_cls(0, f"stream cortado: {e}") from e
            last_exc = e
            log.warning(
                f"{log_event}.network_error",
                exc_type=type(e).__name__,
                exc=str(e),
                attempt=attempt,
            )
        if attempt < max_retries:
            await asyncio.sleep(backoff_base * (2**attempt))
    if isinstance(last_exc, LLMError):
        raise last_exc
    raise error_cls(0, f"network error on POST {path}") from last_exc


__all__ = ["ChunkAccumulator", "parse_arguments", "parse_sse_line", "stream_openai_tools"]
