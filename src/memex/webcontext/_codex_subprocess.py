"""`run_codex` — un subproceso `codex exec` por búsqueda, factorizado para webcontext.

Espeja la mecánica de `memex.llm.codex.CodexClient.complete` (subprocess + tmp + timeout + lectura
del `-o` + `--json` para el usage) PERO sin importar la capa LLM (el dueño marcó ese acople como
contaminante) y agregando los `extra_args` del contexto web (`-c web_search_mode=live`,
`--output-schema <file>`). El `usage` se parsea acá: `turn.completed.usage` es ACUMULATIVO
(no por-turno) → se toma el ÚLTIMO `turn.completed`, no se suma (openai/codex#17539, igual que
`memex.llm.codex._parse_usage`).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memex.webcontext.client import WebContextProviderError, WebContextUsage

_STDERR_PREVIEW_MAX = 500


@dataclass(frozen=True)
class CodexRun:
    """Resultado crudo de un `codex exec`: el mensaje final (`-o`), el usage y la latencia."""

    last_message: str
    usage: WebContextUsage
    latency_ms: int


def _as_int(value: Any) -> int:
    """Int defensivo (réplica local de `memex.llm._openai.as_int`, sin acoplar a la capa LLM)."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _parse_usage(stdout: bytes) -> WebContextUsage:
    """Tokens del JSONL de `codex exec --json` (best-effort): el ÚLTIMO `turn.completed.usage`.

    `cached_input_tokens` es subconjunto de `input_tokens`; `reasoning_output_tokens` ya está en
    `output_tokens`. Líneas no-JSON o una salida sin `usage` degradan a `WebContextUsage()` (ceros).
    """
    last: dict[str, Any] | None = None
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                last = usage
    if last is None:
        return WebContextUsage()
    return WebContextUsage(
        input_tokens=_as_int(last.get("input_tokens")),
        output_tokens=_as_int(last.get("output_tokens")),
        cached_input_tokens=_as_int(last.get("cached_input_tokens")),
        reasoning_tokens=_as_int(last.get("reasoning_output_tokens")),
    )


async def run_codex(
    binary: Sequence[str],
    prompt: str,
    *,
    extra_args: Sequence[str],
    sandbox: str,
    timeout_s: float,
) -> CodexRun:
    """Corre `codex exec` (prompt por stdin, mensaje final por `-o`, `--json` para el usage).

    `extra_args` se insertan antes del `-` final (p. ej. `web_search_mode` y `--output-schema`).
    Levanta `WebContextProviderError` ante exit!=0, timeout, binario inejecutable o salida vacía.
    """
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="memex-webctx-codex-") as tmp:
        out_file = Path(tmp) / "last_message.txt"
        args = [
            *binary,
            "exec",
            "--json",  # stdout → JSONL estructurado (tokens vía _parse_usage)
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "-s",
            sandbox,
            "-C",
            tmp,
            "-o",
            str(out_file),
            *extra_args,
            "-",  # prompt por stdin
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise WebContextProviderError(0, f"no se pudo lanzar codex: {e}") from e
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")), timeout=timeout_s
            )
        except TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise WebContextProviderError(0, f"codex exec superó el timeout ({timeout_s}s)") from e

        latency_ms = int((time.monotonic() - started) * 1000)
        if proc.returncode != 0:
            preview = stderr.decode("utf-8", errors="replace")[:_STDERR_PREVIEW_MAX]
            raise WebContextProviderError(
                0, f"codex exec falló (exit {proc.returncode})", body=preview or None
            )
        content = out_file.read_text(encoding="utf-8").strip() if out_file.exists() else ""

    if not content:
        raise WebContextProviderError(0, "codex exec terminó sin mensaje final (salida vacía)")
    return CodexRun(last_message=content, usage=_parse_usage(stdout), latency_ms=latency_ms)
