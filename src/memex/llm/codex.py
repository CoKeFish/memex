"""CodexClient — proveedor vía `codex exec` (suscripción ChatGPT del dueño, sin API key).

Proveedor de primera clase del gate de relevancia (`settings.provider='codex'`). Comparación
real contra Opus (mismos 30 correos): acuerdo 30/30; latencia ~8x mayor (codex es un agente,
no una completion). Sus limitaciones son estructurales (documentadas acá, no arreglables sin
API real):

- **Sin contabilidad**: el CLI no reporta tokens → `LLMUsage` queda en cero y `cost_usd=0`.
  Las filas de `llm_calls` de una corrida con codex muestran costo 0 aunque consuma los
  límites de la suscripción. /métricas queda ciego para estas llamadas.
- **Auth de sesión**: requiere una sesión de `codex login` accesible. En el host: el login
  propio. En el contenedor: el binario viene en la imagen (Dockerfile) y `CODEX_HOME` apunta
  a `/secrets/codex` (compose) — copiar ahí el `auth.json` del host; si la sesión muere, las
  corridas fallan con `CodexError` (re-copiar/re-login).
- **Sin JSON forzado ni retries**: `response_format` se ignora (el JSON se pide por prompt,
  como con Anthropic); un fallo del CLI es un error reintentable de la ventana, sin backoff
  propio.
- **`model` por llamada se IGNORA**: el `settings.model` del gate pertenece al proveedor
  Anthropic; acá el modelo se fija al construir el cliente (`codex_model` de settings o
  `--codex-model`) o se deja el default del CLI. `result.model` reporta `codex/<m|default>`
  para que los veredictos distingan proveedor.

Mecánica: un subproceso `codex exec` por completion — prompt por stdin (evita el límite de
longitud de argv en Windows), mensaje final capturado con `-o <archivo>` (sin parsear el
ruido de stdout), `--ephemeral` (sin archivos de sesión), sandbox configurable y cwd en un
directorio temporal (el agente no debe mirar ningún repo).

Sandbox (`MEMEX_CODEX_SANDBOX`, default `read-only`): el sandbox propio de codex (landlock)
NO funciona dentro de docker → el compose lo fija en `danger-full-access` para el contenedor
(que ya ES el sandbox). En el host queda el default conservador.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from memex.llm.client import ChatMessage, LLMError, LLMResult, LLMUsage, ResponseFormat
from memex.logging import get_logger

_STDERR_PREVIEW_MAX = 500
#: codex puede tardar (es un agente, no una completion): budget generoso por llamada.
_DEFAULT_TIMEOUT_S = 300.0
#: Sandbox del CLI (ver docstring del módulo). Env para que el compose lo fije sin código.
_SANDBOX_ENV = "MEMEX_CODEX_SANDBOX"
_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
_DEFAULT_SANDBOX = "read-only"


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
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as e:
                raise CodexError(0, f"no se pudo lanzar codex: {e}") from e
            try:
                _, stderr = await asyncio.wait_for(
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

        self._log.info(
            "llm.codex.complete",
            model=model_name,
            response_format=response_format,
            latency_ms=latency_ms,
            content_chars=len(content),
        )
        # Sin métricas del CLI: usage en cero y costo 0 (la suscripción no factura por token).
        return LLMResult(
            content=content,
            model=model_name,
            usage=LLMUsage(0, 0, 0),
            cost_usd=Decimal(0),
            latency_ms=latency_ms,
            finish_reason="stop",
        )
