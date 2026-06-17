"""CodexClient — wrapper experimental de `codex exec` con un stub de binario (sin red)."""

from __future__ import annotations

import asyncio
import sys
import textwrap
from decimal import Decimal
from pathlib import Path

import pytest

from memex.llm.client import ChatMessage, LLMClient, LLMError, LLMUsage
from memex.llm.codex import CodexClient, CodexError

#: Stub que imita `codex exec --json`: lee el prompt de stdin, emite JSONL por stdout (ruido +
#: dos `turn.completed` con usage ACUMULATIVO creciente) y escribe el mensaje final en el archivo
#: de `-o`; sale 0. `FAIL` en el prompt → exit 3 con stderr (camino de error).
_STUB = textwrap.dedent(
    """
    import json
    import sys

    args = sys.argv[1:]
    out_path = args[args.index("-o") + 1]
    prompt = sys.stdin.read()
    if "FAIL" in prompt:
        sys.stderr.write("boom del stub")
        sys.exit(3)
    print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
    print("ruido no-json que debe ignorarse")
    print(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 100, "cached_input_tokens": 0,
        "output_tokens": 10, "reasoning_output_tokens": 0}}))
    print(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 12416, "cached_input_tokens": 11136,
        "output_tokens": 49, "reasoning_output_tokens": 42}}))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write('{"verdicts": []}\\n')
        f.write("ARGS:" + "|".join(args))
    """
)


def _client(tmp_path: Path, **kwargs: object) -> CodexClient:
    stub = tmp_path / "codex_stub.py"
    stub.write_text(_STUB, encoding="utf-8")
    return CodexClient(binary=(sys.executable, str(stub)), **kwargs)  # type: ignore[arg-type]


def test_complete_captures_last_message_and_zero_cost(tmp_path: Path) -> None:
    c = _client(tmp_path)
    r = asyncio.run(c.complete([ChatMessage("system", "sos un portero"), ChatMessage("user", "x")]))
    assert r.content.startswith('{"verdicts": []}')
    # Flags pactados con codex exec 0.128: stdin (-), -o, ephemeral, sandbox read-only.
    assert "exec" in r.content and "--ephemeral" in r.content and "read-only" in r.content
    assert r.content.rstrip().endswith("|-")  # el prompt va por stdin
    assert r.model == "codex/default"
    assert "--json" in r.content  # stdout = JSONL estructurado (de ahí salen los tokens)
    assert r.cost_usd == Decimal(0)  # la suscripción no factura por token: USD no medido
    # usage del ÚLTIMO turn.completed (acumulativo: 12416, NO el primero 100 ni la suma 12516).
    assert r.usage.prompt_tokens == 12416
    assert r.usage.completion_tokens == 49
    assert r.usage.total_tokens == 12416 + 49
    assert r.usage.cache_hit_tokens == 11136
    assert r.usage.cache_miss_tokens == 12416 - 11136
    assert r.usage.reasoning_tokens == 42
    assert r.finish_reason == "stop"


def test_model_flag_is_constructor_not_per_call(tmp_path: Path) -> None:
    c = _client(tmp_path, model="gpt-5.1-codex")
    # el `model` por llamada se IGNORA (pertenece al proveedor default del gate)
    r = asyncio.run(c.complete([ChatMessage("user", "x")], model="claude-opus-4-8"))
    assert r.model == "codex/gpt-5.1-codex"
    assert "-m|gpt-5.1-codex" in r.content.replace("ARGS:", "|")


def test_nonzero_exit_raises_with_stderr_preview(tmp_path: Path) -> None:
    c = _client(tmp_path)
    with pytest.raises(CodexError) as exc:
        asyncio.run(c.complete([ChatMessage("user", "FAIL")]))
    assert "exit 3" in str(exc.value)
    assert exc.value.body == "boom del stub"


def test_codex_client_satisfies_llmclient_protocol() -> None:
    assert issubclass(CodexClient, LLMClient)


def test_codex_error_subclasses_llm_error() -> None:
    assert issubclass(CodexError, LLMError)


def test_build_gate_client_selects_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """El builder del gate respeta settings.provider (codex resuelve el binario del PATH)."""
    import shutil

    from memex.relevance.providers import build_gate_client
    from memex.relevance.settings import GateSettings

    monkeypatch.setattr(shutil, "which", lambda _: "C:/fake/codex.cmd")
    client = build_gate_client(GateSettings(provider="codex", codex_model="gpt-5.1"))
    assert isinstance(client, CodexClient)


def test_sandbox_from_env_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """El compose fija MEMEX_CODEX_SANDBOX=danger-full-access (el contenedor ES el sandbox)."""
    monkeypatch.setenv("MEMEX_CODEX_SANDBOX", "danger-full-access")
    c = _client(tmp_path)
    r = asyncio.run(c.complete([ChatMessage("user", "x")]))
    assert "danger-full-access" in r.content
    monkeypatch.setenv("MEMEX_CODEX_SANDBOX", "invalido")
    with pytest.raises(CodexError):
        _client(tmp_path)


#: Stub que devuelve el JSON envuelto en fences + prosa (codex hace esto a veces).
_FENCED_STUB = textwrap.dedent(
    """
    import sys

    args = sys.argv[1:]
    out_path = args[args.index("-o") + 1]
    sys.stdin.read()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write('Claro, aca va:\\n```json\\n{"modules": ["finance"]}\\n```\\n')
    """
)


def _fenced_client(tmp_path: Path) -> CodexClient:
    stub = tmp_path / "codex_fenced_stub.py"
    stub.write_text(_FENCED_STUB, encoding="utf-8")
    return CodexClient(binary=(sys.executable, str(stub)))


def test_json_object_normalizes_fenced_output(tmp_path: Path) -> None:
    """JSON por prompt: con response_format=json_object, los fences/prosa se extraen."""
    c = _fenced_client(tmp_path)
    r = asyncio.run(c.complete([ChatMessage("user", "x")], response_format="json_object"))
    assert r.content == '{"modules": ["finance"]}'


def test_text_format_passes_fences_through(tmp_path: Path) -> None:
    """Sin json_object NO se sanea: el summarizer consume texto tal cual."""
    c = _fenced_client(tmp_path)
    r = asyncio.run(c.complete([ChatMessage("user", "x")]))
    assert r.content.startswith("Claro") and "```json" in r.content


#: Stub que emite JSONL SIN ningún `turn.completed` → el cliente degrada a LLMUsage(0,0,0).
_NO_USAGE_STUB = textwrap.dedent(
    """
    import json
    import sys

    args = sys.argv[1:]
    out_path = args[args.index("-o") + 1]
    sys.stdin.read()
    print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message"}}))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("hola")
    """
)


def test_usage_absent_degrades_to_zero(tmp_path: Path) -> None:
    """Sin `turn.completed`/usage en el stdout: usage en cero, sin romper la completion."""
    stub = tmp_path / "codex_no_usage_stub.py"
    stub.write_text(_NO_USAGE_STUB, encoding="utf-8")
    c = CodexClient(binary=(sys.executable, str(stub)))
    r = asyncio.run(c.complete([ChatMessage("user", "x")]))
    assert r.content == "hola"
    assert r.usage == LLMUsage(0, 0, 0)
    assert r.cost_usd == Decimal(0)
