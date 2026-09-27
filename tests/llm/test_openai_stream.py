"""Streaming + herramientas OpenAI-compatible: acumulador puro, parser SSE y el método
`stream_tools` de DeepSeekClient sobre HTTP (respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from memex.llm._openai_stream import ChunkAccumulator, parse_arguments, parse_sse_line
from memex.llm.client import LLMQuotaError
from memex.llm.config import LLMConfig
from memex.llm.deepseek import DeepSeekClient
from memex.llm.pricing import MODEL_PRICING
from memex.llm.tools import (
    AgentMessage,
    StreamDone,
    TextDelta,
    ToolCall,
    ToolCallsEvent,
    ToolSpec,
)


def _chunk(
    delta: dict[str, object], finish: str | None = None, usage: dict[str, int] | None = None
) -> dict[str, object]:
    out: dict[str, object] = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        out["usage"] = usage
    return out


def test_accumulator_text_then_tool_fragments() -> None:
    acc = ChunkAccumulator(model="deepseek-chat")
    assert acc.feed(_chunk({"role": "assistant", "content": ""})) == []
    assert acc.feed(_chunk({"content": "Ho"})) == [TextDelta("Ho")]
    assert acc.feed(_chunk({"content": "la"})) == [TextDelta("la")]
    # los argumentos gotean en dos fragmentos del MISMO índice
    acc.feed(
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "memex", "arguments": '{"comm'},
                    }
                ]
            }
        )
    )
    acc.feed(
        _chunk(
            {"tool_calls": [{"index": 0, "function": {"arguments": 'and": "tareas list --json"}'}}]}
        )
    )
    acc.feed(
        _chunk(
            {},
            finish="tool_calls",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )
    )
    events = acc.finish(latency_ms=12, pricing=MODEL_PRICING)
    assert isinstance(events[0], ToolCallsEvent)
    assert events[0].calls == (
        ToolCall(id="call_1", name="memex", arguments={"command": "tareas list --json"}),
    )
    done = events[-1]
    assert isinstance(done, StreamDone)
    assert (
        done.text == "Hola" and done.usage.total_tokens == 15 and done.finish_reason == "tool_calls"
    )
    assert done.cost_usd > 0


def test_parse_helpers() -> None:
    assert parse_sse_line("data: [DONE]") is None
    assert parse_sse_line(": keep-alive") is None
    assert parse_sse_line('data: {"a": 1}') == {"a": 1}
    assert parse_sse_line("data: {not json") is None
    assert parse_arguments("") == {}
    assert parse_arguments('{"x": 1}') == {"x": 1}
    assert parse_arguments("[1,2]") == {"_raw": "[1,2]"}
    assert parse_arguments("{bad") == {"_raw": "{bad"}


def test_agent_message_openai_shapes() -> None:
    call = ToolCall("c1", "memex", {"command": "x"})
    assistant = AgentMessage("assistant", "", tool_calls=(call,)).as_openai()
    assert assistant["content"] is None and assistant["tool_calls"][0]["function"] == {
        "name": "memex",
        "arguments": '{"command": "x"}',
    }
    tool = AgentMessage("tool", "salida", tool_call_id="c1", name="memex").as_openai()
    assert tool == {"role": "tool", "content": "salida", "tool_call_id": "c1", "name": "memex"}
    spec = ToolSpec("f", "d", {"type": "object", "properties": {}}).as_openai()
    assert spec["type"] == "function" and spec["function"]["name"] == "f"


def _sse(*chunks: dict[str, object]) -> bytes:
    import json

    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode("utf-8")


def _client() -> DeepSeekClient:
    cfg = LLMConfig.from_env({"DEEPSEEK_API_KEY": "k"}, base_url="https://ds.test/v1")
    return DeepSeekClient(cfg, pricing=MODEL_PRICING)


@pytest.mark.asyncio
@respx.mock
async def test_deepseek_stream_tools_over_http() -> None:
    route = respx.post("https://ds.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=_sse(
                _chunk({"content": "Ten"}),
                _chunk({"content": "és 2"}),
                _chunk(
                    {},
                    finish="stop",
                    usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
                ),
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    async with _client() as client:
        events = [
            ev
            async for ev in client.stream_tools(
                [AgentMessage("user", "hola")],
                tools=[ToolSpec("memex", "d", {"type": "object", "properties": {}})],
            )
        ]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Ten", "és 2"]
    done = events[-1]
    assert isinstance(done, StreamDone) and done.text == "Tenés 2" and done.usage.total_tokens == 10
    sent = route.calls[0].request
    import json

    body = json.loads(sent.content)
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["tools"][0]["function"]["name"] == "memex"
    assert body["messages"] == [{"role": "user", "content": "hola"}]


@pytest.mark.asyncio
@respx.mock
async def test_deepseek_stream_quota_aborts() -> None:
    respx.post("https://ds.test/v1/chat/completions").mock(
        return_value=httpx.Response(402, text='{"error": "insufficient balance"}')
    )
    async with _client() as client:
        with pytest.raises(LLMQuotaError):
            async for _ in client.stream_tools([AgentMessage("user", "hola")]):
                pass
