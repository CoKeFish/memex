"""CodexWebContextProvider — contexto web vía `codex exec` con web search built-in.

Implementa el Protocol `WebContextProvider`. Una búsqueda = un `codex exec` (vía `run_codex`) con
`-c web_search_mode=live` (búsqueda web hosted, auto-selecciona fuentes) y `--output-schema` (fuerza
el JSON conforme a `EntityProfile`). Costo $0 (suscripción del dueño); los tokens igual se reportan
(`--json`). Si la salida no valida, reintenta `format_retries` veces antes de propagar.

Sandbox: misma env `MEMEX_CODEX_SANDBOX` que `memex.llm.codex` (la constante se REPLICA acá para no
acoplar a la capa LLM). El contenedor la fija en `danger-full-access`; el host usa
`read-only` (el web search es server-side, no necesita red local).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from memex.logging import get_logger
from memex.webcontext._codex_subprocess import run_codex
from memex.webcontext.client import (
    EntityKind,
    ProfileResult,
    WebContextConfigError,
    WebContextFormatError,
)
from memex.webcontext.config import WebContextConfig
from memex.webcontext.schema import entity_profile_schema, validate_profile

_SANDBOX_ENV = "MEMEX_CODEX_SANDBOX"
_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
_DEFAULT_SANDBOX = "read-only"
_RAW_MAX = 2000

#: `kind` → sintagma natural para el prompt (mejor que "la producto").
_KIND_WORD: dict[EntityKind, str] = {
    "organizacion": "la organización",
    "producto": "el producto",
}


def _build_prompt(name: str, kind: EntityKind) -> str:
    """Prompt: buscar en la web y devolver SOLO el JSON del esquema, con fuentes reales."""
    return (
        "Busca en la web y responde SOLO con el JSON que cumpla el esquema provisto "
        "(sin prosa ni fences). "
        f"Perfil corto y verificable de {_KIND_WORD[kind]} «{name}». "
        "En el campo 'sources' poné las URLs reales que consultaste. "
        "Solo hechos verificables; dejá vacío lo que no encuentres."
    )


class CodexWebContextProvider:
    """Proveedor de contexto web vía `codex exec`. `binary` inyectable para tests (stub)."""

    name: ClassVar[str] = "codex"

    def __init__(self, config: WebContextConfig, *, binary: Sequence[str] | None = None) -> None:
        self._config = config
        self._log = get_logger("memex.webcontext.codex")
        resolved_sandbox = os.environ.get(_SANDBOX_ENV, _DEFAULT_SANDBOX)
        if resolved_sandbox not in _SANDBOX_MODES:
            raise WebContextConfigError(
                f"sandbox inválido: {resolved_sandbox!r}; válidos: {_SANDBOX_MODES}"
            )
        self._sandbox = resolved_sandbox
        if binary is not None:
            self._binary = list(binary)
        else:
            resolved = shutil.which("codex")
            if resolved is None:
                raise WebContextConfigError(
                    "binario `codex` no encontrado en PATH (¿npm i -g @openai/codex?)"
                )
            self._binary = [resolved]

    async def search(self, name: str, kind: EntityKind) -> ProfileResult:
        prompt = _build_prompt(name, kind)
        attempts = max(1, self._config.format_retries + 1)
        last_err: WebContextFormatError | None = None
        with tempfile.TemporaryDirectory(prefix="memex-webctx-codex-") as tmp:
            schema_file = Path(tmp) / "schema.json"
            schema_file.write_text(json.dumps(entity_profile_schema()), encoding="utf-8")
            extra = ["-c", "web_search_mode=live", "--output-schema", str(schema_file)]
            for attempt in range(attempts):
                run = await run_codex(
                    self._binary,
                    prompt,
                    extra_args=extra,
                    sandbox=self._sandbox,
                    timeout_s=self._config.timeout_s,
                )
                try:
                    profile = validate_profile(run.last_message, expected_kind=kind)
                except WebContextFormatError as e:
                    last_err = e
                    self._log.warning(
                        "webcontext.codex.format_retry", attempt=attempt + 1, error=str(e)[:200]
                    )
                    continue
                self._log.info(
                    "webcontext.codex.search",
                    entity=name,
                    kind=kind,
                    latency_ms=run.latency_ms,
                    input_tokens=run.usage.input_tokens,
                    output_tokens=run.usage.output_tokens,
                )
                return ProfileResult(
                    profile=profile,
                    provider=self.name,
                    latency_ms=run.latency_ms,
                    tokens=run.usage,
                    raw=run.last_message[:_RAW_MAX],
                )
        assert last_err is not None  # attempts >= 1 y no hubo return → hubo un FormatError
        raise last_err

    async def aclose(self) -> None:
        return None
