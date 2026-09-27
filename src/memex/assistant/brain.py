"""Un turno de razonamiento con codex: el pedido del usuario (texto, transcripción o foto) +
reglas +
`memex help` + historial → codex ejecuta comandos `memex ...` de CONSULTA y devuelve una salida
estructurada: `reply` para el usuario y `proposed_commands` (escrituras que NO ejecutó, a
confirmar). El prompt es el mismo que validó el banco `experiments/agent_cli_bench.py`
(20 pedidos, 0 banderas inventadas, 0 escrituras sin confirmar).

Las fotos van como imágenes del prompt (`codex exec -i`), sin OCR aparte: el mismo turno lee el
recibo y propone el `memex finance register`. Cuota/ocupado → `CodexBusyError` (el gateway avisa
y espera); sesión inválida/modelo → `CodexUnavailableError` (hace falta acción humana).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from memex.llm.codex import CodexBusyError, CodexError, CodexUnavailableError, classify_failure
from memex.logging import get_logger

_log = get_logger("memex.assistant.brain")

#: Verbos de escritura del CLI (auditoría: si el modelo ejecutó uno con exit 0 sin `--help`, se
#: reporta en el chat como escritura NO confirmada).
WRITE_VERBS = (
    "register",
    "add",
    "add-",
    "close",
    "reopen",
    "rename",
    "merge",
    "rm",
    "delete",
    "unify",
    "set-",
    "affiliate",
    "relate",
    "unrelate",
    "resolve",
    "start",
    "end",
    "cancel",
    "update",
    "evidence",
    "annotate",
    "confirm",
    # verbos de asuntos/tareas/finanzas que llegaron después: siguen siendo escrituras
    "prioridad",
    "vence",
    "nota",
    "plan",
    "silence",
    "unsilence",
    "dato",
    "rubro",
    "done",
    "discard",
    "link",
    "fijar",
    "fijar-regla",
    "estado",
    "subs-set",
    "set-place",
)

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {
            "type": "string",
            "description": "Respuesta al usuario en español, corta, con los datos.",
        },
        "proposed_commands": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Comandos de ESCRITURA exactos (memex ...) que NO ejecutaste y que el "
            "usuario debe confirmar. Vacío si no hay.",
        },
    },
    "required": ["reply", "proposed_commands"],
    "additionalProperties": False,
}

_STDERR_PREVIEW_MAX = 600


@dataclass(frozen=True)
class TurnRequest:
    text: str
    images: Sequence[Path] = ()
    #: (rol, texto) con rol `usuario` | `asistente`; lo más viejo primero.
    history: Sequence[tuple[str, str]] = ()
    #: Escrituras propuestas que siguen sin confirmar (para que el modelo sepa el contexto).
    pending: Sequence[str] = ()
    #: Escrituras `<dominio> <verbo>` que el sistema ejecuta de inmediato al recibirlas (sin
    #: confirmación); el modelo las propone igual, pero redacta la respuesta como hecho.
    auto_execute: Sequence[str] = ()
    #: Streaming: recibe el texto ACUMULADO de la respuesta cada vez que crece (el cerebro por
    #: API lo llama por cada pedazo; codex no hace streaming y lo ignora).
    on_text: Callable[[str], Awaitable[None]] | None = None


@dataclass
class TurnResult:
    reply: str
    proposed_commands: list[str] = field(default_factory=list)
    #: Comandos que codex ejecutó durante el turno, con su exit code.
    commands: list[tuple[str, int | None]] = field(default_factory=list)
    #: Errores del runtime de codex (items `error` del JSONL): p. ej. «Code Mode is unavailable»
    #: cuando falta `codex-code-mode-host` y el modelo NO puede ejecutar ningún comando.
    errors: list[str] = field(default_factory=list)
    tokens: int = 0
    elapsed_s: float = 0.0
    raw_last_message: str = ""

    @property
    def failed_commands(self) -> list[str]:
        return [c for c, code in self.commands if code not in (0, None)]

    @property
    def unconfirmed_writes(self) -> list[str]:
        return [c for c, code in self.commands if code == 0 and looks_like_write(c)]


class Brain(Protocol):
    """Un motor de turnos: codex, API directa con herramientas, o el enrutador entre ambos."""

    async def run(self, req: TurnRequest) -> TurnResult: ...


def looks_like_write(command: str) -> bool:
    """Un comando `memex <dominio> <verbo>` de escritura ejecutado de verdad (no su `--help`)."""
    if "--help" in command or command.rstrip("'\" ").endswith(" -h"):
        return False
    tokens = command.replace("'", " ").replace('"', " ").split()
    if "memex" not in tokens:
        return False
    after = tokens[tokens.index("memex") + 1 :]
    return any(t in WRITE_VERBS or t.startswith(("set-", "add-")) for t in after[:4])


def build_prompt(req: TurnRequest, *, help_text: str, memex_invocation: str) -> str:
    history = ""
    if req.history:
        lines = [f"[{role}] {text}" for role, text in req.history]
        history = (
            "Historial reciente del chat (lo más viejo primero):\n" + "\n".join(lines) + "\n\n"
        )
    pending = ""
    if req.pending:
        pending = (
            "Hay escrituras propuestas que el usuario aún NO confirmó (si su mensaje es una "
            "confirmación, respondé que las confirme con el botón; no las ejecutes):\n"
            + "\n".join(f"  {c}" for c in req.pending)
            + "\n\n"
        )
    auto = ""
    if req.auto_execute:
        auto = (
            "Estas escrituras el sistema las ejecuta DE INMEDIATO al recibirlas en "
            "`proposed_commands`, sin pedir confirmación: "
            + ", ".join(f"`memex {p}`" for p in req.auto_execute)
            + ". Proponelas igual (exactas), pero redactá `reply` como hecho consumado "
            "(«Registré…»), no como propuesta ni pidiendo confirmación.\n\n"
        )
    images = ""
    if req.images:
        images = (
            f"El usuario adjuntó {len(req.images)} imagen(es) (van en el prompt): interpretalas "
            "en el contexto de memex (recibo, pantallazo de un pago, invitación, etc.) y extraé "
            "montos, fechas, contrapartes y lugares antes de proponer comandos.\n\n"
        )
    return (
        "Sos el asistente personal de memex, el sistema de memoria del usuario. Tu ÚNICA "
        "herramienta es el CLI `memex`, que se invoca desde este directorio como:\n"
        f"    {memex_invocation} <dominio> <comando> [opciones]\n"
        "Documentación del CLI (salida de `memex help`):\n\n"
        f"{help_text}\n\n"
        "Cada subcomando acepta --help para ver sus opciones. Reglas:\n"
        "1. CONSULTAS: ejecutá el comando adecuado (usá --json cuando exista) y respondé al "
        "usuario con los datos, en español y corto. Podés encadenar comandos si hace falta.\n"
        "2. ESCRITURAS (register, add, close, rename, merge, rm, delete, unify, set-*, relate, "
        "start/end, update, evidence, annotate): NUNCA las ejecutes. Ponelas en "
        "`proposed_commands` como comandos EXACTOS que empiecen por `memex ` (sin el prefijo "
        "del intérprete) y en `reply` explicá qué harían; el sistema le pide confirmación al "
        "usuario y las ejecuta por vos.\n"
        "3. Si no existe un comando para lo pedido, decilo claramente y sugerí la alternativa "
        "más cercana. No inventes datos ni comandos.\n"
        "4. Nunca ejecutes nada que no sea el CLI memex.\n"
        "5. Fechas relativas («hoy», «el viernes que viene») se resuelven con la fecha actual "
        f"del sistema ({time.strftime('%Y-%m-%d %H:%M')}, zona del usuario: America/Bogota).\n"
        "6. Tu mensaje final es SOLO el JSON del esquema de salida (reply, proposed_commands). "
        "`reply` en texto plano: sin markdown (nada de **negritas** ni #), el chat lo muestra "
        "literal.\n\n"
        f"{history}{pending}{auto}{images}"
        f"Pedido del usuario: {req.text}"
    )


def parse_events(stdout: str) -> tuple[list[tuple[str, int | None]], str, int, list[str]]:
    """Del JSONL de `codex exec --json`: comandos ejecutados (con exit), último mensaje del
    agente, tokens del ÚLTIMO `turn.completed` (es acumulativo, no se suma) y los errores del
    runtime (items `error`: codex sigue y contesta «de memoria»; sin esto el fallo es silencioso).
    """
    commands: list[tuple[str, int | None]] = []
    last_message = ""
    tokens = 0
    errors: list[str] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
            usage = ev["usage"]
            tokens = int(usage.get("input_tokens", 0) or 0) + int(
                usage.get("output_tokens", 0) or 0
            )
        item = ev.get("item")
        if not isinstance(item, dict) or ev.get("type") != "item.completed":
            continue
        if item.get("type") == "command_execution":
            code = item.get("exit_code")
            commands.append((str(item.get("command", "")), int(code) if code is not None else None))
        elif item.get("type") == "agent_message":
            last_message = str(item.get("text", ""))
        elif item.get("type") == "error":
            errors.append(str(item.get("message", "")))
    return commands, last_message, tokens, errors


def parse_final(text: str) -> tuple[str, list[str]]:
    """El mensaje final con el esquema (reply + proposed_commands). Si el modelo no cumplió el
    esquema, el texto crudo es la respuesta y no hay propuestas."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.startswith("json"):
            candidate = candidate[4:]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return text.strip(), []
    if not isinstance(data, dict) or "reply" not in data:
        return text.strip(), []
    proposed_raw = data.get("proposed_commands") or []
    proposed = (
        [str(c).strip() for c in proposed_raw if str(c).strip()]
        if isinstance(proposed_raw, list)
        else []
    )
    return str(data.get("reply", "")).strip(), proposed


class CodexBrain:
    def __init__(
        self,
        *,
        help_text: str,
        memex_invocation: str,
        workdir: Path,
        binary: Sequence[str] | None = None,
        model: str | None = None,
        timeout_s: float = 300.0,
        sandbox: str = "danger-full-access",
    ) -> None:
        self._help_text = help_text
        self._memex_invocation = memex_invocation
        self._workdir = workdir
        self._model = model or None
        self._timeout_s = timeout_s
        self._sandbox = sandbox
        if binary is not None:
            self._binary = list(binary)
        else:
            resolved = shutil.which("codex")
            if resolved is None:
                raise CodexUnavailableError(0, "binario `codex` no encontrado en PATH")
            self._binary = [resolved]

    async def run(self, req: TurnRequest) -> TurnResult:
        prompt = build_prompt(
            req, help_text=self._help_text, memex_invocation=self._memex_invocation
        )
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="memex-assistant-") as tmp:
            schema_file = Path(tmp) / "schema.json"
            schema_file.write_text(json.dumps(_OUTPUT_SCHEMA), encoding="utf-8")
            out_file = Path(tmp) / "last_message.txt"
            args = [
                *self._binary,
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "-s",
                self._sandbox,
                "-C",
                str(self._workdir),
                "-o",
                str(out_file),
                "--output-schema",
                str(schema_file),
            ]
            if self._model:
                args += ["-m", self._model]
            for img in req.images:
                args += ["-i", str(img)]
            args.append("-")
            env = dict(os.environ)
            env.setdefault("PYTHONIOENCODING", "utf-8")
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self._workdir),
                    env=env,
                )
            except OSError as e:
                raise CodexUnavailableError(0, f"no pude lanzar codex: {e}") from e
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(prompt.encode("utf-8")), timeout=self._timeout_s
                )
            except TimeoutError as e:
                proc.kill()
                raise CodexError(0, f"codex exec superó el timeout ({self._timeout_s:.0f}s)") from e
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                preview = stderr[-_STDERR_PREVIEW_MAX:]
                cls = classify_failure(preview)
                _log.warning(
                    "assistant.brain.failed",
                    exit_code=proc.returncode,
                    kind=cls.__name__,
                    stderr=preview[:200],
                )
                raise cls(0, f"codex exec falló (exit {proc.returncode})", body=preview or None)
            commands, last_message, tokens, errors = parse_events(stdout)
            if errors and not commands:
                # El runtime de codex no pudo ejecutar NADA (2026-09-15: faltaba
                # `codex-code-mode-host` en la imagen y el modelo respondió «no pude acceder al
                # CLI» como si fuera un dato). Hace falta intervenir, no un turno más.
                _log.error("assistant.brain.runtime_error", errors=[e[:200] for e in errors])
                raise CodexUnavailableError(
                    0, "codex no pudo ejecutar comandos", body="; ".join(errors)[:600]
                )
            if errors:
                _log.warning("assistant.brain.runtime_warning", errors=[e[:200] for e in errors])
            final_text = last_message
            if out_file.exists():
                final_text = out_file.read_text(encoding="utf-8", errors="replace") or last_message
        reply, proposed = parse_final(final_text)
        result = TurnResult(
            reply=reply,
            proposed_commands=proposed,
            commands=commands,
            errors=errors,
            tokens=tokens,
            elapsed_s=time.monotonic() - started,
            raw_last_message=final_text,
        )
        _log.info(
            "assistant.brain.turn",
            commands=len(commands),
            failed=len(result.failed_commands),
            proposed=len(proposed),
            unconfirmed_writes=len(result.unconfirmed_writes),
            tokens=tokens,
            elapsed_s=round(result.elapsed_s, 1),
            images=len(req.images),
        )
        return result


__all__ = [
    "WRITE_VERBS",
    "Brain",
    "CodexBrain",
    "CodexBusyError",
    "CodexError",
    "CodexUnavailableError",
    "TurnRequest",
    "TurnResult",
    "build_prompt",
    "looks_like_write",
    "parse_events",
    "parse_final",
]
