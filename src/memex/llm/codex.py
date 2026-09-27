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
  corridas fallan con `CodexUnavailableError` (re-copiar/re-login).
- **Sin JSON forzado**: el JSON se pide por prompt (como con Anthropic);
  `response_format="json_object"` activa el saneo de la salida (`normalize_json_output`:
  extrae el JSON de fences/prosa SOLO si parsea; si no, pasa crudo y el parser del caller
  degrada seguro).
- **`model` por llamada se IGNORA**: el `settings.model` del gate pertenece al proveedor
  Anthropic; acá el modelo se fija al construir el cliente (`codex_model` de settings o
  `--codex-model`) o se deja el default del CLI. `result.model` reporta `codex/<m|default>`
  para que los veredictos distingan proveedor.

Salud del proveedor (2026-09-07, decisión del dueño: «si es por cuota, que se espere»). Un
fallo de `codex exec` se CLASIFICA por el stderr:

- `CodexBusyError` — cuota/rate limit/red (usage limit, 429, reconnecting…): transitorio. El
  cliente ESPERA (`wait_s`, default 5 min) y reintenta la misma llamada hasta `wait_max_s`
  (default 6 h), logueando `llm.codex.waiting`. No cae a otro proveedor: el arbitraje es de
  codex; se espera a que vuelva.
- `CodexUnavailableError` — el modelo no existe / exige CLI más nueva / sesión inválida: hace
  falta una acción humana. Es `LLMQuotaError` (los workers la propagan y ABORTAN el run en vez
  de iterar fallando) y NO dispara fallback (`fallback_ok = False`): degradar el arbitraje a
  otro proveedor en silencio no es lo que se quiere.
- `CodexError` a secas — cualquier otro fallo (exit ≠ 0 desconocido, salida vacía, timeout):
  best-effort por ventana como siempre; con fallback configurado, el siguiente proveedor.

`health_check()` es la sonda mínima (`codex exec "OK"`) para saber si el proveedor está vivo
sin gastar un prompt real: `memex-llm health`.

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
from memex.llm.client import (
    ChatMessage,
    LLMError,
    LLMQuotaError,
    LLMResult,
    LLMUsage,
    ResponseFormat,
)
from memex.logging import get_logger

_STDERR_PREVIEW_MAX = 500
#: codex puede tardar (es un agente, no una completion): budget generoso por llamada.
_DEFAULT_TIMEOUT_S = 300.0
#: Sandbox del CLI (ver docstring del módulo). Env para que el compose lo fije sin código.
_SANDBOX_ENV = "MEMEX_CODEX_SANDBOX"
_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
_DEFAULT_SANDBOX = "read-only"
#: Espera ante cuota/rate limit (segundos entre reintentos y tope total). Env: sin tocar código.
_WAIT_ENV = "MEMEX_CODEX_WAIT_S"
_WAIT_MAX_ENV = "MEMEX_CODEX_WAIT_MAX_S"
_DEFAULT_WAIT_S = 300.0
_DEFAULT_WAIT_MAX_S = 6 * 3600.0

#: Marcas en el stderr de `codex exec` que indican un fallo TRANSITORIO (se espera y reintenta).
_BUSY_MARKS = (
    "usage limit",
    "rate limit",
    "rate_limit",
    "too many requests",
    "429",
    "quota",
    "reconnecting",
    "overloaded",
    "temporarily unavailable",
    "service unavailable",
    "502",
    "503",
    "504",
    "connection reset",
    "connection refused",
    "network",
    "timed out",
)
#: Marcas de un fallo que exige acción humana (modelo/CLI/sesión): se aborta el run.
_UNAVAILABLE_MARKS = (
    "does not exist or you do not have access",
    "requires a newer version",
    "is not supported",
    "not supported",
    "unauthorized",
    "401",
    "not logged in",
    "please log in",
    "login required",
    "invalid_api_key",
    "authentication",
    "auth.json",
)


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


class CodexBusyError(CodexError):
    """Fallo TRANSITORIO (cuota, rate limit, red): el cliente espera y reintenta."""


class CodexUnavailableError(CodexError, LLMQuotaError):
    """Codex no puede servir hasta que alguien actúe (modelo retirado, CLI vieja, sesión caída).
    Es `LLMQuotaError` para que los workers aborten el run; NO cae a otro proveedor."""

    fallback_ok = False


def classify_failure(stderr: str) -> type[CodexError]:
    """Clase de error según el stderr del CLI (ver docstring del módulo)."""
    low = stderr.lower()
    if any(m in low for m in _UNAVAILABLE_MARKS):
        return CodexUnavailableError
    if any(m in low for m in _BUSY_MARKS):
        return CodexBusyError
    return CodexError


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class CodexClient:
    """Cliente que satisface el Protocol `LLMClient` envolviendo `codex exec`.

    `binary` es inyectable para tests (p. ej. un stub de Python); por default se resuelve
    `codex` del PATH (en Windows, `shutil.which` encuentra el `.cmd` del npm install).
    `wait_s`/`wait_max_s`: espera ante un fallo transitorio (cuota) — None = env/default.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        binary: Sequence[str] | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        sandbox: str | None = None,
        wait_s: float | None = None,
        wait_max_s: float | None = None,
    ) -> None:
        self._model = model
        self._timeout_s = timeout_s
        self._wait_s = wait_s if wait_s is not None else _env_float(_WAIT_ENV, _DEFAULT_WAIT_S)
        self._wait_max_s = (
            wait_max_s if wait_max_s is not None else _env_float(_WAIT_MAX_ENV, _DEFAULT_WAIT_MAX_S)
        )
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
                raise CodexUnavailableError(
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
        waited = 0.0
        while True:
            try:
                return await self._run_once(prompt, response_format)
            except CodexBusyError as e:
                if waited >= self._wait_max_s:
                    self._log.error(
                        "llm.codex.wait_exhausted", waited_s=int(waited), error=str(e)[:200]
                    )
                    raise
                self._log.warning(
                    "llm.codex.waiting",
                    reason=(e.body or str(e))[:200],
                    wait_s=self._wait_s,
                    waited_s=int(waited),
                    wait_max_s=self._wait_max_s,
                )
                await asyncio.sleep(self._wait_s)
                waited += self._wait_s

    async def _run_once(self, prompt: str, response_format: ResponseFormat) -> LLMResult:
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
                cls = classify_failure(preview)
                raise cls(0, f"codex exec falló (exit {proc.returncode})", body=preview or None)
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


async def health_check(
    *, binary: Sequence[str] | None = None, model: str | None = None, timeout_s: float = 120.0
) -> tuple[bool, str]:
    """Sonda mínima del proveedor: un `codex exec` trivial SIN espera por cuota. Devuelve
    `(ok, detalle)`; el detalle trae el error clasificado cuando no está vivo."""
    try:
        client = CodexClient(binary=binary, model=model, timeout_s=timeout_s, wait_max_s=0)
        r = await client.complete([ChatMessage("user", "Respondé solo la palabra OK.")])
    except CodexError as e:
        reason = (e.body or "").strip().splitlines()[-1:] or [str(e)]
        return False, f"{type(e).__name__}: {reason[0][:300]}"
    alive = "OK" in r.content.upper()
    return alive, f"{r.model} · {r.latency_ms} ms · {r.content[:40]!r}"
