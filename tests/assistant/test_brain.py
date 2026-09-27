"""Cerebro: prompt, parseo del JSONL de codex y del mensaje final, auditoría de escrituras.
Sin codex real: el binario se reemplaza por un stub de Python."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from memex.assistant.brain import (
    CodexBrain,
    CodexBusyError,
    CodexUnavailableError,
    TurnRequest,
    build_prompt,
    looks_like_write,
    parse_events,
    parse_final,
)


def _ev(kind: str, **item: object) -> str:
    return json.dumps({"type": "item.completed", "item": {"type": kind, **item}})


def test_parse_events_commands_message_and_last_usage() -> None:
    lines = [
        "plain noise",
        _ev("command_execution", command="memex finance summary --q uber --json", exit_code=0),
        _ev("command_execution", command="memex asuntos list --q x", exit_code=2),
        _ev("agent_message", text="parcial"),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 5}}),
        _ev("agent_message", text='{"reply": "ok", "proposed_commands": []}'),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 300, "output_tokens": 20}}),
    ]
    commands, last, tokens, errors = parse_events("\n".join(lines))
    assert commands == [
        ("memex finance summary --q uber --json", 0),
        ("memex asuntos list --q x", 2),
    ]
    assert json.loads(last)["reply"] == "ok"
    assert tokens == 320  # el último turn.completed, no la suma
    assert errors == []


def test_parse_events_collects_runtime_errors() -> None:
    lines = [
        _ev("error", message="Code Mode is unavailable because failed to spawn code-mode host"),
        _ev("agent_message", text="No pude ejecutar el comando"),
    ]
    commands, last, _tokens, errors = parse_events("\n".join(lines))
    assert commands == []
    assert last == "No pude ejecutar el comando"
    assert errors == ["Code Mode is unavailable because failed to spawn code-mode host"]


def test_parse_final_schema_fences_and_fallback() -> None:
    reply, cmds = parse_final(
        '{"reply": "Hola", "proposed_commands": ["memex finance register --amount 1", " "]}'
    )
    assert (reply, cmds) == ("Hola", ["memex finance register --amount 1"])
    reply, cmds = parse_final('```json\n{"reply": "Con fences", "proposed_commands": []}\n```')
    assert (reply, cmds) == ("Con fences", [])
    reply, cmds = parse_final("texto plano sin esquema")
    assert (reply, cmds) == ("texto plano sin esquema", [])
    reply, cmds = parse_final('{"otra": 1}')
    assert cmds == [] and "otra" in reply


def test_looks_like_write_ignores_help_and_queries() -> None:
    assert looks_like_write("uv run memex finance register --amount 25000")
    assert looks_like_write("memex identidad set-parent --id 1 --parent 2")
    assert not looks_like_write("uv run memex finance register --help")
    assert not looks_like_write("uv run memex bienestar habit add -h")
    assert not looks_like_write("memex finance summary --q uber --json")
    assert not looks_like_write("ls -la")


def test_build_prompt_includes_help_history_pending_and_images() -> None:
    req = TurnRequest(
        text="¿Cuánto gasté en Uber?",
        images=[Path("recibo.jpg")],
        history=[("usuario", "hola"), ("asistente", "hola, ¿qué necesitás?")],
        pending=["memex finance register --amount 1"],
    )
    prompt = build_prompt(
        req, help_text="DOC-DEL-CLI", memex_invocation="python -m memex.agent_cli"
    )
    assert "DOC-DEL-CLI" in prompt
    assert "python -m memex.agent_cli <dominio>" in prompt
    assert "[usuario] hola" in prompt and "[asistente] hola" in prompt
    assert "memex finance register --amount 1" in prompt
    assert "1 imagen(es)" in prompt
    assert prompt.rstrip().endswith("Pedido del usuario: ¿Cuánto gasté en Uber?")


def _stub(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "codex_stub.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(script)]


@pytest.mark.asyncio
async def test_run_parses_stub_output_and_images_flags(tmp_path: Path) -> None:
    binary = _stub(
        tmp_path,
        """
        import json, sys
        args = sys.argv[1:]
        prompt = sys.stdin.read()
        assert "-i" in args and "--output-schema" in args, args
        out = args[args.index("-o") + 1]
        final = json.dumps(
            {"reply": "Gastaste 1", "proposed_commands": ["memex finance register --amount 1"]}
        )
        def ev(kind, **item):
            return json.dumps({"type": "item.completed", "item": {"type": kind, **item}})
        print(ev("command_execution", command="memex finance summary --json", exit_code=0))
        print(ev("agent_message", text=final))
        usage = {"input_tokens": 10, "output_tokens": 2}
        print(json.dumps({"type": "turn.completed", "usage": usage}))
        open(out, "w", encoding="utf-8").write(final)
        """,
    )
    brain = CodexBrain(help_text="DOC", memex_invocation="memex", workdir=tmp_path, binary=binary)
    img = tmp_path / "foto.jpg"
    img.write_bytes(b"x")
    res = await brain.run(TurnRequest(text="hola", images=[img]))
    assert res.reply == "Gastaste 1"
    assert res.proposed_commands == ["memex finance register --amount 1"]
    assert res.commands == [("memex finance summary --json", 0)]
    assert res.tokens == 12 and res.unconfirmed_writes == []


@pytest.mark.asyncio
async def test_run_classifies_failures(tmp_path: Path) -> None:
    busy = _stub(tmp_path, "import sys; sys.stderr.write('usage limit reached'); sys.exit(1)")
    brain = CodexBrain(help_text="DOC", memex_invocation="memex", workdir=tmp_path, binary=busy)
    with pytest.raises(CodexBusyError):
        await brain.run(TurnRequest(text="hola"))
    gone = _stub(tmp_path, "import sys; sys.stderr.write('please log in'); sys.exit(1)")
    brain = CodexBrain(help_text="DOC", memex_invocation="memex", workdir=tmp_path, binary=gone)
    with pytest.raises(CodexUnavailableError):
        await brain.run(TurnRequest(text="hola"))


@pytest.mark.asyncio
async def test_run_raises_when_codex_could_not_execute_anything(tmp_path: Path) -> None:
    # 2026-09-15: sin `codex-code-mode-host` codex sale con exit 0, emite un item `error` y el
    # modelo contesta «no pude acceder al CLI» como si fuera un dato. Eso es intervención humana.
    broken = _stub(
        tmp_path,
        """
        import json
        def ev(kind, **item):
            return json.dumps({"type": "item.completed", "item": {"type": kind, **item}})
        print(ev("error", message="Code Mode is unavailable because failed to spawn host"))
        print(ev("agent_message", text=json.dumps({"reply": "No pude", "proposed_commands": []})))
        """,
    )
    brain = CodexBrain(help_text="DOC", memex_invocation="memex", workdir=tmp_path, binary=broken)
    with pytest.raises(CodexUnavailableError, match="no pudo ejecutar comandos"):
        await brain.run(TurnRequest(text="hola"))
