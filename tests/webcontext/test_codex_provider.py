"""CodexWebContextProvider con un stub de binario (sin codex real ni red).

Mirror de tests/llm/test_codex.py: tests SINCRÓNICOS con `asyncio.run` (el subprocess de codex en
Windows quiere el Proactor loop, que `asyncio.run` garantiza). El stub lee el prompt de stdin, emite
JSONL de `--json` con `turn.completed.usage` y escribe el perfil en el archivo de `-o`.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from memex.webcontext import (
    WebContextConfig,
    WebContextConfigError,
    WebContextFormatError,
    WebContextProvider,
    WebContextProviderError,
)
from memex.webcontext.codex import CodexWebContextProvider

#: Stub OK: exige los flags extra, emite usage acumulativo y escribe un perfil válido. El kind
#: "producto" del stub se re-inyecta al kind del caller en validate_profile.
_OK_STUB = textwrap.dedent(
    """
    import json, sys
    args = sys.argv[1:]
    assert "--output-schema" in args, "falta --output-schema"
    assert "-c" in args and "web_search_mode=live" in args, "falta web_search_mode=live"
    assert "--json" in args, "falta --json"
    out_path = args[args.index("-o") + 1]
    sys.stdin.read()
    print(json.dumps({"type": "thread.started"}))
    print("ruido no-json que se ignora")
    print(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 1200, "cached_input_tokens": 1000,
        "output_tokens": 30, "reasoning_output_tokens": 5}}))
    profile = {
        "name": "Rappi", "kind": "producto", "one_liner": "superapp",
        "sector": "tech", "country": "Colombia", "founded": "2015",
        "key_facts": ["unicornio"], "sources": ["https://es.wikipedia.org/wiki/Rappi"],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(profile))
    """
)

#: Stub que cuenta corridas (counter junto al propio stub, estable entre intentos): el 1º intento
#: escribe JSON inválido, el 2º uno válido → ejercita el retry de formato.
_RETRY_STUB = textwrap.dedent(
    """
    import json, os, sys
    here = os.path.dirname(os.path.abspath(__file__))
    counter = os.path.join(here, "n.txt")
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(n + 1))
    args = sys.argv[1:]
    out_path = args[args.index("-o") + 1]
    sys.stdin.read()
    print(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 5, "cached_input_tokens": 0,
        "output_tokens": 1, "reasoning_output_tokens": 0}}))
    with open(out_path, "w", encoding="utf-8") as f:
        if n == 0:
            f.write("esto no es json")
        else:
            f.write(json.dumps({"name": "X", "kind": "organizacion",
                "one_liner": "y", "sector": "s", "country": "c"}))
    """
)

#: Stub que SIEMPRE escribe JSON inválido (cuenta corridas para verificar que reintentó).
_BAD_STUB = textwrap.dedent(
    """
    import json, os, sys
    here = os.path.dirname(os.path.abspath(__file__))
    counter = os.path.join(here, "n.txt")
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(n + 1))
    args = sys.argv[1:]
    out_path = args[args.index("-o") + 1]
    sys.stdin.read()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("nunca valida")
    """
)

#: Stub que falla (exit != 0) con stderr.
_FAIL_STUB = textwrap.dedent(
    """
    import sys
    sys.stdin.read()
    sys.stderr.write("boom del stub")
    sys.exit(3)
    """
)


def _config() -> WebContextConfig:
    return WebContextConfig.from_env({}, provider="codex")


def _provider(tmp_path: Path, stub_src: str) -> CodexWebContextProvider:
    stub = tmp_path / "codex_stub.py"
    stub.write_text(stub_src, encoding="utf-8")
    return CodexWebContextProvider(_config(), binary=(sys.executable, str(stub)))


def test_satisfies_protocol(tmp_path: Path) -> None:
    assert isinstance(_provider(tmp_path, _OK_STUB), WebContextProvider)


def test_search_ok_parses_profile_and_usage(tmp_path: Path) -> None:
    result = asyncio.run(_provider(tmp_path, _OK_STUB).search("Rappi", "organizacion"))
    assert result.profile.name == "Rappi"
    assert result.profile.kind == "organizacion"  # re-inyectado (el stub puso "producto")
    assert result.profile.country == "Colombia"
    assert result.provider == "codex"
    assert result.tokens is not None
    assert result.tokens.input_tokens == 1200
    assert result.tokens.cached_input_tokens == 1000
    assert result.tokens.output_tokens == 30


def test_format_retry_then_success(tmp_path: Path) -> None:
    result = asyncio.run(_provider(tmp_path, _RETRY_STUB).search("X", "organizacion"))
    assert result.profile.name == "X"
    assert int((tmp_path / "n.txt").read_text()) == 2  # corrió 2 veces (1 retry)


def test_format_invalid_twice_raises(tmp_path: Path) -> None:
    with pytest.raises(WebContextFormatError):
        asyncio.run(_provider(tmp_path, _BAD_STUB).search("X", "organizacion"))
    assert int((tmp_path / "n.txt").read_text()) == 2  # agotó los 2 intentos


def test_nonzero_exit_raises_provider_error(tmp_path: Path) -> None:
    with pytest.raises(WebContextProviderError) as exc:
        asyncio.run(_provider(tmp_path, _FAIL_STUB).search("X", "producto"))
    assert exc.value.body == "boom del stub"


def test_binary_missing_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(WebContextConfigError):
        CodexWebContextProvider(_config())


def test_invalid_sandbox_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMEX_CODEX_SANDBOX", "invalido")
    with pytest.raises(WebContextConfigError):
        _provider(tmp_path, _OK_STUB)
