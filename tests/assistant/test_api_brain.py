"""Cerebro por API: loop de herramientas, bloqueo de escrituras en código, `proponer`, streaming
(`on_text`), tope de vueltas y el enrutador (fotos → codex, fallback ante LLMError)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from memex.assistant.api_brain import TOOL_MEMEX, TOOL_PROPOSE, ApiBrain, RoutedBrain, plain_text
from memex.assistant.brain import TurnRequest, TurnResult
from memex.assistant.executor import ExecResult
from memex.assistant.gateway import Gateway, Incoming, Outgoing
from memex.assistant.session import SessionStore
from memex.llm.client import LLMError, LLMUsage
from memex.llm.tools import (
    AgentMessage,
    StreamDone,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallsEvent,
    ToolSpec,
)


def _done(text: str = "", calls: tuple[ToolCall, ...] = ()) -> StreamDone:
    return StreamDone(
        model="m",
        usage=LLMUsage(10, 5, 15),
        cost_usd=Decimal("0.001"),
        latency_ms=5,
        finish_reason="tool_calls" if calls else "stop",
        text=text,
        tool_calls=calls,
    )


def _calls(*pairs: tuple[str, dict[str, object]]) -> tuple[ToolCall, ...]:
    return tuple(ToolCall(f"c{i}", name, args) for i, (name, args) in enumerate(pairs))


class FakeToolClient:
    def __init__(self, rounds: list[list[StreamEvent]]) -> None:
        self.rounds = list(rounds)
        self.seen: list[list[AgentMessage]] = []

    def stream_tools(
        self,
        messages: Sequence[AgentMessage],
        *,
        tools: Sequence[ToolSpec] = (),
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.seen.append(list(messages))
        events = self.rounds.pop(0) if self.rounds else [_done("")]

        async def gen() -> AsyncIterator[StreamEvent]:
            for ev in events:
                yield ev

        return gen()


@dataclass
class FakeExecutor:
    output: str = '{"items": []}'
    ran: list[str] = field(default_factory=list)

    async def run(self, command: str) -> ExecResult:
        self.ran.append(command)
        return ExecResult(command, 0, self.output)


def _brain(client: FakeToolClient, executor: FakeExecutor, **kw: object) -> ApiBrain:
    return ApiBrain(
        client=client,
        help_text="DOC",
        executor=executor,
        model="m",
        record_cost=False,
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_text_only_turn_streams_accumulated_text() -> None:
    client = FakeToolClient([[TextDelta("Ho"), TextDelta("la"), _done("Hola")]])
    seen: list[str] = []

    async def on_text(t: str) -> None:
        seen.append(t)

    res = await _brain(client, FakeExecutor()).run(
        TurnRequest(
            text="hola", history=[("usuario", "antes"), ("asistente", "ok")], on_text=on_text
        )
    )
    assert res.reply == "Hola" and res.commands == [] and res.proposed_commands == []
    assert seen == ["Ho", "Hola"] and res.tokens == 15
    msgs = client.seen[0]
    assert msgs[0].role == "system" and "DOC" in msgs[0].content
    assert [(m.role, m.content) for m in msgs[1:]] == [
        ("user", "antes"),
        ("assistant", "ok"),
        ("user", "hola"),
    ]


@pytest.mark.asyncio
async def test_tool_round_then_answer() -> None:
    calls = _calls((TOOL_MEMEX, {"command": "tareas list --json"}))
    client = FakeToolClient(
        [
            [ToolCallsEvent(calls), _done(calls=calls)],
            [TextDelta("No tenés tareas."), _done("No tenés tareas.")],
        ]
    )
    ex = FakeExecutor()
    res = await _brain(client, ex).run(TurnRequest(text="¿qué tareas tengo?"))
    assert ex.ran == ["memex tareas list --json"]
    assert res.commands == [("memex tareas list --json", 0)]
    assert res.reply == "No tenés tareas."
    second = client.seen[1]
    assert second[-2].role == "assistant" and second[-2].tool_calls == calls
    assert second[-1].role == "tool" and second[-1].tool_call_id == "c0"
    assert second[-1].content == '{"items": []}'


@pytest.mark.asyncio
async def test_write_via_memex_is_rejected_in_code() -> None:
    calls = _calls((TOOL_MEMEX, {"command": "memex tareas add --title x"}))
    client = FakeToolClient([[ToolCallsEvent(calls), _done(calls=calls)], [_done("ok")]])
    ex = FakeExecutor()
    res = await _brain(client, ex).run(TurnRequest(text="agregá x"))
    assert ex.ran == []  # nunca llegó al ejecutor
    tool_msg = client.seen[1][-1]
    assert tool_msg.content.startswith("RECHAZADO") and TOOL_PROPOSE in tool_msg.content
    assert res.commands == [("memex tareas add --title x", None)]
    assert res.unconfirmed_writes == []


@pytest.mark.asyncio
async def test_propose_registers_valid_commands_only() -> None:
    calls = _calls(
        (
            TOOL_PROPOSE,
            {
                "commands": [
                    "memex tareas add --title x",
                    "rm -rf / && memex",
                    "uv run memex finance register --amount 5",
                ]
            },
        )
    )
    client = FakeToolClient(
        [[ToolCallsEvent(calls), _done(calls=calls)], [TextDelta("Listo."), _done("Listo.")]]
    )
    res = await _brain(client, FakeExecutor()).run(TurnRequest(text="agregá"))
    assert res.proposed_commands == [
        "memex tareas add --title x",
        "memex finance register --amount 5",
    ]
    assert "Rechazados" in client.seen[1][-1].content
    assert res.reply == "Listo."


@pytest.mark.asyncio
async def test_max_tool_rounds_exhausted() -> None:
    calls = _calls((TOOL_MEMEX, {"command": "tareas list --json"}))
    client = FakeToolClient([[ToolCallsEvent(calls), _done(calls=calls)] for _ in range(3)])
    res = await _brain(client, FakeExecutor(), max_tool_rounds=1).run(TurnRequest(text="x"))
    assert "agoté" in res.reply and len(client.seen) == 2


@pytest.mark.asyncio
async def test_pending_writes_go_as_system_note() -> None:
    client = FakeToolClient([[_done("ok")]])
    await _brain(client, FakeExecutor()).run(
        TurnRequest(text="sí", pending=["memex tareas add --title x"])
    )
    notes = [m for m in client.seen[0] if m.role == "system"]
    assert len(notes) == 2 and "memex tareas add --title x" in notes[1].content


class _Fixed:
    def __init__(self, reply: str, *, fail: Exception | None = None) -> None:
        self.reply, self.fail, self.calls = reply, fail, 0

    async def run(self, req: TurnRequest) -> TurnResult:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return TurnResult(reply=self.reply)


@pytest.mark.asyncio
async def test_routed_brain_images_and_fallback() -> None:
    api, codex = _Fixed("api"), _Fixed("codex")
    routed = RoutedBrain(primary=api, fallback=codex)
    assert (await routed.run(TurnRequest(text="t"))).reply == "api"
    assert (await routed.run(TurnRequest(text="t", images=[Path("a.jpg")]))).reply == "codex"
    broken = RoutedBrain(primary=_Fixed("", fail=LLMError(500, "caído")), fallback=codex)
    assert (await broken.run(TurnRequest(text="t"))).reply == "codex"
    alone = RoutedBrain(primary=_Fixed("", fail=LLMError(500, "caído")), fallback=None)
    with pytest.raises(LLMError):
        await alone.run(TurnRequest(text="t"))


# ----------------------------------------------------------------- gateway + streamer
class _StreamingBrain:
    async def run(self, req: TurnRequest) -> TurnResult:
        assert req.on_text is not None
        await req.on_text("Hol")
        await req.on_text("Hola")
        return TurnResult(reply="Hola", proposed_commands=["memex tareas add --title x"])


class _Streamer:
    def __init__(self) -> None:
        self.updates: list[str] = []
        self.final: Outgoing | None = None

    async def update(self, text: str) -> None:
        self.updates.append(text)

    async def finish(self, out: Outgoing) -> bool:
        self.final = out
        return True


class _Stt:
    name = "fake"

    async def transcribe(self, path: Path) -> str:
        return "transcripto"


@pytest.mark.asyncio
async def test_gateway_uses_streamer_and_skips_send() -> None:
    sent: list[Outgoing] = []

    async def send(out: Outgoing) -> None:
        sent.append(out)

    gw = Gateway(
        brain=_StreamingBrain(),
        transcriber=_Stt(),
        executor=FakeExecutor(),  # type: ignore[arg-type]
        sessions=SessionStore(),
        allowed_ids=frozenset({1}),
    )
    st = _Streamer()
    await gw.handle(Incoming(chat_id=1, user_id=1, kind="text", text="hola"), send, st)
    assert st.updates == ["Hol", "Hola"]
    assert st.final is not None and st.final.text.startswith("Hola") and st.final.buttons
    assert "Crear tarea: «x»" in st.final.text  # en palabras, no el comando
    assert "memex tareas add" not in st.final.text
    assert sent == []  # el streamer pintó el final; nada por `send`


def test_plain_text_strips_markdown() -> None:
    md = "## Hoy\n**Clase** a las `15:10`"
    assert plain_text(md) == "Hoy\nClase a las 15:10"


@pytest.mark.asyncio
async def test_writes_left_in_prose_become_proposals() -> None:
    """El modelo escribió «propongo esto» con los comandos en el texto y no llamó a `proponer`:
    igual quedan registrados (validados), y las consultas o la basura no."""
    reply = (
        "Propongo esto (se ejecuta al confirmar):\n"
        '- memex asuntos prioridad --id 278 --level alta --reason "voy a participar"\n'
        '- memex tareas add --title "Postular al programa" --asunto 278\n'
        "- memex tareas list --asunto 278\n"
        "- rm -rf / && memex tareas add --title x\n"
        "Decime si querés algo más."
    )
    client = FakeToolClient([[TextDelta(reply), _done(reply)]])
    res = await _brain(client, FakeExecutor()).run(TurnRequest(text="voy a participar"))
    assert res.proposed_commands == [
        'memex asuntos prioridad --id 278 --level alta --reason "voy a participar"',
        'memex tareas add --title "Postular al programa" --asunto 278',
    ]
    assert "Propongo esto" in res.reply


def test_prose_writes_ignores_queries_and_prefixes() -> None:
    from memex.assistant.api_brain import prose_writes

    assert prose_writes("memex tareas list\nmemex tareas show --id 1") == []
    assert prose_writes("  • `uv run memex tareas add --title x`  ") == [
        "memex tareas add --title x"
    ]
    assert prose_writes("hoy no hay nada") == []


def test_the_prompt_points_research_to_plan() -> None:
    from memex.assistant.api_brain import system_prompt

    p = system_prompt(help_text="DOC", now="2026-09-16")
    assert "memex asuntos plan" in p and "no tengo acceso a internet" in p.lower()
    assert "proponer" in p
