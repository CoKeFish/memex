"""CodexClient — wrapper experimental de `codex exec` con un stub de binario (sin red)."""

from __future__ import annotations

import asyncio
import sys
import textwrap
from decimal import Decimal
from pathlib import Path

import pytest

from memex.llm.client import ChatMessage, LLMClient, LLMError
from memex.llm.codex import CodexClient, CodexError

#: Stub que imita `codex exec`: lee el prompt de stdin, escribe el mensaje final en el
#: archivo de `-o` y sale 0. `FAIL` en el prompt → exit 3 con stderr (camino de error).
_STUB = textwrap.dedent(
    """
    import sys

    args = sys.argv[1:]
    out_path = args[args.index("-o") + 1]
    prompt = sys.stdin.read()
    if "FAIL" in prompt:
        sys.stderr.write("boom del stub")
        sys.exit(3)
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
    assert r.cost_usd == Decimal(0)  # la suscripción no factura por token: costo no medido
    assert r.usage.prompt_tokens == 0 and r.usage.completion_tokens == 0
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
