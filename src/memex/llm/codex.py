"""CodexClient — proveedor EXPERIMENTAL vía `codex exec` (suscripción ChatGPT, sin API key).

Existe para PRUEBAS del dueño: comparar veredictos del gate de relevancia usando su
suscripción de Codex en vez de pagar tokens de API. NO es para producción, y las
limitaciones son estructurales (documentadas acá, no arreglables sin API real):

- **Sin contabilidad**: el CLI no reporta tokens → `LLMUsage` queda en cero y `cost_usd=0`.
  Las filas de `llm_calls` de una corrida con codex muestran costo 0 aunque consuma los
  límites de la suscripción. /métricas queda ciego para estas llamadas.
- **Auth de sesión**: requiere `codex login` hecho en ESTA máquina; si el token venció, la
  corrida falla. No funciona dentro del contenedor del API (el binario y la sesión viven en
  el host) → solo CLI host-side (`memex-relevance run --provider codex`).
- **Sin JSON forzado ni retries**: `response_format` se ignora (el JSON se pide por prompt,
  como con Anthropic); un fallo del CLI es un error reintentable de la ventana, sin backoff
  propio.
- **`model` por llamada se IGNORA**: el `settings.model` del gate pertenece al proveedor
  default (Anthropic); acá el modelo se fija al construir el cliente (`--codex-model`) o se
  deja el default del CLI. `result.model` reporta `codex/<modelo|default>` para que los
  veredictos del experimento distingan proveedor.

Mecánica: un subproceso `codex exec` por completion — prompt por stdin (evita el límite de
longitud de argv en Windows), mensaje final capturado con `-o <archivo>` (sin parsear el
ruido de stdout), `--ephemeral` (sin archivos de sesión), sandbox read-only y cwd en un
directorio temporal (el agente no debe mirar ningún repo).
"""

from __future__ import annotations

import asyncio
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
    ) -> None:
        self._model = model
        self._timeout_s = timeout_s
        self._log = get_logger("memex.llm.codex")
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
                "read-only",
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
