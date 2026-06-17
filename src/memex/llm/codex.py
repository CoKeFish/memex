"""CodexClient — proveedor vía `codex exec` (suscripción ChatGPT del dueño, sin API key).

Proveedor de primera clase del gate de relevancia (`settings.provider='codex'`). Comparación
real contra Opus (mismos 30 correos): acuerdo 30/30; latencia ~8x mayor (codex es un agente,
no una completion). Sus limitaciones son estructurales (documentadas acá, no arreglables sin
API real):

- **Sin costo en USD, pero CON tokens**: la suscripción no factura por token → `cost_usd=0`
  en las filas de `llm_calls` (/métricas ciega en $ para estas llamadas, aunque consuman los
  límites de la suscripción). Los **tokens sí** se capturan: `codex exec --json` emite el
  `usage` real en el evento `turn.completed` (ver `_parse_usage`), así que `LLMUsage` se puebla
  y /métricas ve el volumen.
- **Auth de sesión**: requiere una sesión de `codex login` accesible. En el host: el login
  propio. En el contenedor: el binario viene en la imagen (Dockerfile) y `CODEX_HOME` apunta
  a `/secrets/codex` (compose) — copiar ahí el `auth.json` del host; si la sesión muere, las
  corridas fallan con `CodexError` (re-copiar/re-login).
- **Sin JSON forzado ni retries**: el JSON se pide por prompt (como con Anthropic);
  `response_format="json_object"` activa el saneo de la salida (`normalize_json_output`:
  extrae el JSON de fences/prosa SOLO si parsea; si no, pasa crudo y el parser del caller
  degrada seguro). Un fallo del CLI es un error reintentable de la ventana, sin backoff
  propio.
- **`model` por llamada se IGNORA**: el `settings.model` del gate pertenece al proveedor
  Anthropic; acá el modelo se fija al construir el cliente (`codex_model` de settings o
  `--codex-model`) o se deja el default del CLI. `result.model` reporta `codex/<m|default>`
  para que los veredictos distingan proveedor.

Mecánica: un subproceso `codex exec` por completion — prompt por stdin (evita el límite de
longitud de argv en Windows), mensaje final capturado con `-o <archivo>`, `--json` para que el
stdout sea JSONL estructurado (de donde `_parse_usage` lee los tokens), `--ephemeral` (sin
archivos de sesión), sandbox configurable y cwd en un directorio temporal (el agente no debe
mirar ningún repo).

Sandbox (`MEMEX_CODEX_SANDBOX`, default `read-only`): el sandbox propio de codex (landlock)
NO funciona dentro de docker → el compose lo fija en `danger-full-access` para el contenedor
(que ya ES el sandbox). En el host queda el default conservador.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from memex.llm._json import normalize_json_output
from memex.llm._openai import as_int
from memex.llm.client import ChatMessage, LLMError, LLMResult, LLMUsage, ResponseFormat
from memex.logging import get_logger

_STDERR_PREVIEW_MAX = 500
#: codex puede tardar (es un agente, no una completion): budget generoso por llamada.
_DEFAULT_TIMEOUT_S = 300.0
#: Sandbox del CLI (ver docstring del módulo). Env para que el compose lo fije sin código.
_SANDBOX_ENV = "MEMEX_CODEX_SANDBOX"
_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
_DEFAULT_SANDBOX = "read-only"


def _parse_usage(stdout: bytes) -> LLMUsage:
    """Tokens del JSONL de `codex exec --json` (telemetría best-effort).

    `turn.completed.usage` es ACUMULATIVO por sesión (running total), no por-turno → se toma
    el ÚLTIMO `turn.completed`, NO se suman (openai/codex#17539). El mapeo espeja
    `_openai.parse_usage`: `cached_input_tokens` es subconjunto de `input_tokens` (no se suma
    aparte) y `reasoning_output_tokens` ya está incluido en `output_tokens`. Robustez: líneas
    no-JSON o una salida sin `usage` degradan a `LLMUsage(0, 0, 0)` sin romper la completion.
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
        return LLMUsage(0, 0, 0)
    prompt = as_int(last.get("input_tokens"))
    completion = as_int(last.get("output_tokens"))
    hit = as_int(last.get("cached_input_tokens"))
    return LLMUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cache_hit_tokens=hit,
        cache_miss_tokens=max(prompt - hit, 0),
        reasoning_tokens=as_int(last.get("reasoning_output_tokens")),
    )


class CodexError(LLMError):
    """Raised cuando `codex exec` falla (exit != 0, timeout, binario ausente o salida vacía)."""


class CodexClient:
    """Cliente experimental que satisface el Protocol `LLMClient` envolviendo `codex exec`.

    `binary` es inyectable para tests (p. ej. un stub de Python); por default se resuelve
    `codex` del PATH (en Windows, `shutil.which` encuentra el `.cmd` del npm install).
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        binary: Sequence[str] | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        sandbox: str | None = None,
    ) -> None:
        self._model = model
        self._timeout_s = timeout_s
        self._log = get_logger("memex.llm.codex")
        resolved_sandbox = (
            sandbox if sandbox is not None else os.environ.get(_SANDBOX_ENV, _DEFAULT_SANDBOX)
        )
        if resolved_sandbox not in _SANDBOX_MODES:
            raise CodexError(
                0, f"sandbox inválido: {resolved_sandbox!r}; válidos: {_SANDBOX_MODES}"
            )
        self._sandbox = resolved_sandbox
        if binary is not None:
            self._binary = list(binary)
        else:
            resolved = shutil.which("codex")
            if resolved is None:
                raise CodexError(
                    0, "binario `codex` no encontrado en PATH (¿npm i -g @openai/codex?)"
                )
            self._binary = [resolved]

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,  # IGNORADO a propósito (ver docstring del módulo)
        response_format: ResponseFormat = "text",
        temperature: float | None = None,  # ignorado (codex no lo expone)
        max_tokens: int | None = None,  # ignorado (codex no lo expone)
    ) -> LLMResult:
        # Un solo prompt: system primero, luego el resto en orden (codex exec recibe texto plano).
        prompt = "\n\n".join(m.content for m in messages)
        model_name = f"codex/{self._model or 'default'}"

        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="memex-codex-") as tmp:
            out_file = Path(tmp) / "last_message.txt"
            args = [
                *self._binary,
                "exec",
                "--json",  # stdout → JSONL estructurado (tokens vía _parse_usage)
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "-s",
                self._sandbox,
                "-C",
                tmp,
                "-o",
                str(out_file),
            ]
            if self._model is not None:
                args += ["-m", self._model]
            args.append("-")  # prompt por stdin

            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,  # JSONL de `--json`: se parsea para el usage
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as e:
                raise CodexError(0, f"no se pudo lanzar codex: {e}") from e
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(prompt.encode("utf-8")), timeout=self._timeout_s
                )
            except TimeoutError as e:
                proc.kill()
                await proc.wait()
                raise CodexError(0, f"codex exec superó el timeout ({self._timeout_s}s)") from e

            latency_ms = int((time.monotonic() - started) * 1000)
            if proc.returncode != 0:
                preview = stderr.decode("utf-8", errors="replace")[:_STDERR_PREVIEW_MAX]
                raise CodexError(
                    0, f"codex exec falló (exit {proc.returncode})", body=preview or None
                )
            content = out_file.read_text(encoding="utf-8").strip() if out_file.exists() else ""

        if not content:
            raise CodexError(0, "codex exec terminó sin mensaje final (salida vacía)")

        # JSON por prompt: el modelo a veces lo envuelve en fences/prosa. Si el caller pidió
        # JSON, se extrae acá (solo si el candidato parsea; si no, pasa crudo y el parser del
        # caller degrada seguro). Los parsers de los workers NO toleran fences — este es el
        # único punto donde se cierra ese hueco sin tocarlos.
        if response_format == "json_object":
            normalized = normalize_json_output(content)
            if normalized != content:
                self._log.info(
                    "llm.codex.json_normalized",
                    raw_chars=len(content),
                    normalized_chars=len(normalized),
                )
                content = normalized

        usage = _parse_usage(stdout)
        self._log.info(
            "llm.codex.complete",
            model=model_name,
            response_format=response_format,
            latency_ms=latency_ms,
            content_chars=len(content),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cache_hit_tokens=usage.cache_hit_tokens,
            reasoning_tokens=usage.reasoning_tokens,
        )
        # Tokens reales del CLI (`--json` → turn.completed.usage); el costo en USD queda 0 porque
        # la suscripción no factura por token (ver el docstring del módulo).
        return LLMResult(
            content=content,
            model=model_name,
            usage=usage,
            cost_usd=Decimal(0),
            latency_ms=latency_ms,
            finish_reason="stop",
        )
