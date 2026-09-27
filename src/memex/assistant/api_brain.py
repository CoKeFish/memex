"""Cerebro por API directa con herramientas y streaming (el reemplazo rápido de `CodexBrain`).

Medido 2026-09-09: un turno con `codex exec` tardaba p50 31 s (10 a 63 s) porque cada mensaje es una
corrida entera de un agente de código (arranque, leer la ayuda, varios `memex … --json`, razonar).
Acá el mismo contrato (`TurnRequest` → `TurnResult`) se cumple con una llamada de chat con
herramientas a un proveedor OpenAI-compatible (DeepSeek v4-flash por default, cambiable por
`llm_consumer_settings` consumer `assistant_brain`): el modelo pide `memex(command)` para
consultar, `proponer(commands)` para dejar escrituras a confirmar, y el texto de la respuesta
llega en streaming (`TurnRequest.on_text`) para pintarlo en el chat mientras se genera.

Las escrituras siguen bloqueadas en el código, no en el prompt: `memex(...)` rechaza cualquier
verbo de escritura (`looks_like_write`) y le dice al modelo que use `proponer`. El usuario confirma
y el `CommandExecutor` del gateway ejecuta, igual que antes.

`RoutedBrain` decide por turno: fotos → codex (DeepSeek no ve imágenes); el resto → API, y si la
API falla (red, cuota, 4xx) cae a codex para no dejar el mensaje sin respuesta.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from memex.assistant.brain import Brain, TurnRequest, TurnResult, looks_like_write
from memex.assistant.executor import CommandRejected, ExecResult, matches_prefix, normalize
from memex.llm.client import LLMError
from memex.llm.tools import (
    AgentMessage,
    StreamDone,
    TextDelta,
    ToolCall,
    ToolCallsEvent,
    ToolSpec,
    ToolStreamClient,
)
from memex.logging import get_logger

_log = get_logger("memex.assistant.api_brain")

TOOL_MEMEX = "memex"
TOOL_PROPOSE = "proponer"

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name=TOOL_MEMEX,
        description=(
            "Ejecuta un comando de CONSULTA del CLI memex y devuelve su salida. Pasá el comando "
            'SIN el prefijo `memex` (p. ej. "calendario list --since 2026-09-09 --json"). Solo '
            "lecturas: list, show, search, summary, help, --help. Las escrituras se rechazan."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Subcomando y opciones, sin `memex`."}
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name=TOOL_PROPOSE,
        description=(
            "Deja registradas escrituras EXACTAS (`memex <dominio> <verbo> ...`) para que el "
            "usuario las confirme. NO las ejecuta. Usala para register, add, close, rename, "
            "merge, rm, update, estado, fijar, set-*, relate, start/end, annotate."
        ),
        parameters={
            "type": "object",
            "properties": {
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Comandos completos que empiezan por `memex `.",
                }
            },
            "required": ["commands"],
            "additionalProperties": False,
        },
    ),
)


class Executor(Protocol):
    async def run(self, command: str) -> ExecResult: ...


def system_prompt(*, help_text: str, now: str) -> str:
    return (
        "Sos el asistente personal de memex, el sistema de memoria del usuario. Tu única vía al "
        "sistema son dos herramientas:\n"
        f"- `{TOOL_MEMEX}(command)`: ejecuta un comando de CONSULTA del CLI `memex` y te devuelve "
        "la salida (usá --json cuando exista). Podés llamarla varias veces.\n"
        f"- `{TOOL_PROPOSE}(commands)`: registra escrituras exactas para que el usuario las "
        "confirme; el sistema las ejecuta por vos DESPUÉS de la confirmación.\n\n"
        "Documentación del CLI (salida de `memex help`):\n\n"
        f"{help_text}\n\n"
        "Reglas:\n"
        f"1. CONSULTAS: llamá `{TOOL_MEMEX}` con el comando adecuado y respondé con los datos, en "
        "español y corto.\n"
        "2. ESCRITURAS (register, add, close, reopen, rename, merge, rm, delete, unify, set-*, "
        f"relate, start/end, update, estado, fijar, annotate, cancel): NUNCA por `{TOOL_MEMEX}`; "
        f"usá `{TOOL_PROPOSE}` con comandos completos que empiecen por `memex ` y explicá en tu "
        "respuesta qué harían. Si el usuario solo pide algo que es una escritura, proponela sin "
        "pedir permiso de nuevo.\n"
        "3. Si no existe un comando para lo pedido, decilo claramente y sugerí la alternativa más "
        "cercana. No inventes datos, banderas ni comandos; ante la duda pedí `--help`.\n"
        f"4. Fechas relativas («hoy», «el viernes que viene») se resuelven con la fecha actual "
        f"({now}, zona America/Bogota).\n"
        "5. Tu respuesta final es texto plano para un chat: sin markdown (nada de **negritas** "
        "ni #), frases cortas, los datos primero. No repitas la salida cruda de los comandos: "
        "resumila.\n"
        '6. Vos NO tenés internet, pero `memex asuntos plan --id <id> --context "..."` SÍ: busca '
        "en la web (solo fuentes oficiales, tarda varios minutos) y arma los pasos: qué hacer, "
        "dónde, requisitos, fechas, costos. Cuando el usuario pida buscar información, "
        "investigar, averiguar requisitos o «qué tengo que hacer» para algo, proponé `plan` "
        "(con el enlace o lo que dijo, en --context); si el asunto no existe, proponé antes "
        "`memex asuntos add`. Nunca contestes «no tengo acceso a internet» sin proponer `plan`.\n"
        "7. Toda escritura pasa por `proponer`. Un comando `memex …` escrito solo en el texto no "
        "existe para el sistema: el usuario no tiene qué confirmar.\n"
    )


def _history_messages(history: Sequence[tuple[str, str]]) -> list[AgentMessage]:
    out: list[AgentMessage] = []
    for role, text in history:
        out.append(AgentMessage("assistant" if role == "asistente" else "user", text))
    return out


_PROSE_CMD_RE = re.compile(r"^\s*(?:[-*•]\s*)?`?((?:uv run )?memex\s+[^`\n]+?)`?\s*$")


def prose_writes(reply: str) -> list[str]:
    """Comandos de ESCRITURA que el modelo dejó escritos línea por línea en la respuesta (en vez
    de pasarlos por `proponer`): validados con la misma regla que una propuesta (`normalize`) y
    filtrados a los que son escritura (`looks_like_write`). Las consultas se ignoran."""
    out: list[str] = []
    for line in reply.splitlines():
        m = _PROSE_CMD_RE.match(line)
        if m is None:
            continue
        full = f"memex {_strip_memex_prefix(m.group(1))}"
        try:
            normalize(full)
        except CommandRejected:
            continue
        if looks_like_write(full) and full not in out:
            out.append(full)
    return out


def _strip_memex_prefix(command: str) -> str:
    s = command.strip()
    for prefix in ("memex ", "uv run memex "):
        if s.startswith(prefix):
            return s[len(prefix) :].strip()
    return s


@dataclass
class _ToolOutcome:
    content: str
    command: str | None = None
    exit_code: int | None = None
    proposed: list[str] | None = None


class ApiBrain:
    """Un turno = una conversación con herramientas (hasta `max_tool_rounds` idas y vueltas)."""

    def __init__(
        self,
        *,
        client: ToolStreamClient,
        help_text: str,
        executor: Executor,
        model: str | None = None,
        max_tool_rounds: int = 6,
        temperature: float | None = 0.2,
        user_id: int = 1,
        record_cost: bool = True,
    ) -> None:
        self._client = client
        self._help_text = help_text
        self._executor = executor
        self._model = model
        self._max_rounds = max_tool_rounds
        self._temperature = temperature
        self._user_id = user_id
        self._record_cost = record_cost

    async def run(self, req: TurnRequest) -> TurnResult:
        started = time.monotonic()
        messages: list[AgentMessage] = [
            AgentMessage("system", system_prompt(help_text=self._help_text, now=_now())),
            *_history_messages(req.history),
        ]
        if req.pending:
            messages.append(
                AgentMessage(
                    "system",
                    "Hay escrituras propuestas que el usuario aún NO confirmó (si su mensaje es "
                    "una confirmación, decile que use el botón; no las vuelvas a proponer):\n"
                    + "\n".join(f"  {c}" for c in req.pending),
                )
            )
        if req.auto_execute:
            messages.append(
                AgentMessage(
                    "system",
                    "Estas escrituras el sistema las ejecuta DE INMEDIATO al recibirlas por "
                    f"`{TOOL_PROPOSE}`, sin pedir confirmación: "
                    + ", ".join(f"`memex {p}`" for p in req.auto_execute)
                    + ". Proponelas igual (exactas), pero redactá la respuesta como hecho "
                    "consumado («Registré…»), no como propuesta ni pidiendo confirmación.",
                )
            )
        messages.append(AgentMessage("user", req.text))

        commands: list[tuple[str, int | None]] = []
        proposed: list[str] = []
        tokens = 0
        cost = Decimal("0")
        reply = ""
        rounds = 0
        for _round in range(self._max_rounds + 1):
            rounds = _round
            text_parts: list[str] = []
            calls: tuple[ToolCall, ...] = ()
            async for ev in self._client.stream_tools(
                messages, tools=TOOLS, model=self._model, temperature=self._temperature
            ):
                if isinstance(ev, TextDelta):
                    text_parts.append(ev.text)
                    if req.on_text is not None:
                        await req.on_text("".join(text_parts))
                elif isinstance(ev, ToolCallsEvent):
                    calls = ev.calls
                elif isinstance(ev, StreamDone):
                    tokens += ev.usage.total_tokens
                    cost += ev.cost_usd
                    if self._record_cost:
                        await _record(ev, user_id=self._user_id)
            reply = "".join(text_parts).strip()
            if not calls:
                break
            messages.append(AgentMessage("assistant", reply, tool_calls=calls))
            outcomes = await asyncio.gather(*(self._tool(c, req.auto_execute) for c in calls))
            for call, oc in zip(calls, outcomes, strict=True):
                if oc.command is not None:
                    commands.append((oc.command, oc.exit_code))
                if oc.proposed:
                    proposed.extend(c for c in oc.proposed if c not in proposed)
                messages.append(
                    AgentMessage("tool", oc.content, tool_call_id=call.id, name=call.name)
                )
            # El texto parcial que acompañó a las herramientas no es la respuesta final: se
            # reinicia en la próxima vuelta (el streaming del chat lo reemplaza).
            reply = ""
        else:
            reply = reply or (
                "No pude terminar la consulta: agoté las idas y vueltas con el sistema. "
                "Probá con un pedido más acotado."
            )
        # Red de seguridad: si el modelo dejó escrituras en el texto sin pasar por `proponer`,
        # se registran igual (validadas como cualquier propuesta); si no, el usuario ve
        # «propongo esto» y no tiene qué confirmar.
        for cmd in prose_writes(reply):
            if cmd not in proposed:
                proposed.append(cmd)
        reply = plain_text(reply) or "(sin respuesta)"
        result = TurnResult(
            reply=reply,
            proposed_commands=proposed,
            commands=commands,
            tokens=tokens,
            elapsed_s=time.monotonic() - started,
            raw_last_message=reply,
        )
        _log.info(
            "assistant.brain.turn",
            engine="api",
            model=self._model or "",
            rounds=rounds,
            commands=len(commands),
            failed=len(result.failed_commands),
            proposed=len(proposed),
            tokens=tokens,
            cost_usd=str(cost),
            elapsed_s=round(result.elapsed_s, 1),
        )
        return result

    async def _tool(self, call: ToolCall, auto_execute: Sequence[str] = ()) -> _ToolOutcome:
        if call.name == TOOL_PROPOSE:
            raw = call.arguments.get("commands")
            items = [str(c) for c in raw] if isinstance(raw, list) else []
            accepted: list[str] = []
            rejected: list[str] = []
            for c in items:
                full = f"memex {_strip_memex_prefix(c)}"
                try:
                    normalize(full)
                except CommandRejected as e:
                    rejected.append(f"{c} ({e})")
                    continue
                accepted.append(full)
            auto = [c for c in accepted if matches_prefix(c, auto_execute)]
            parts = [
                f"Registrados: {len(accepted)} comando(s); {len(auto)} se ejecutan de inmediato "
                "(sin confirmación) y el resto queda a confirmación del usuario."
                if auto
                else f"Registrados para confirmación del usuario: {len(accepted)} comando(s)."
            ]
            if rejected:
                parts.append("Rechazados (no son comandos memex válidos): " + "; ".join(rejected))
            return _ToolOutcome(" ".join(parts), proposed=accepted)
        if call.name == TOOL_MEMEX:
            raw_cmd = call.arguments.get("command")
            if not isinstance(raw_cmd, str) or not raw_cmd.strip():
                return _ToolOutcome("Falta `command` (string).")
            full = f"memex {_strip_memex_prefix(raw_cmd)}"
            if looks_like_write(full):
                return _ToolOutcome(
                    f"RECHAZADO: `{full}` es una escritura. No la ejecuto: ponela en "
                    f"`{TOOL_PROPOSE}` para que el usuario la confirme.",
                    command=full,
                    exit_code=None,
                )
            try:
                res = await self._executor.run(full)
            except CommandRejected as e:
                return _ToolOutcome(f"RECHAZADO: {e}", command=full, exit_code=None)
            body = res.output or "(sin salida)"
            head = f"exit {res.exit_code}\n" if res.exit_code != 0 else ""
            return _ToolOutcome(f"{head}{body}", command=full, exit_code=res.exit_code)
        return _ToolOutcome(f"Herramienta desconocida: {call.name}.")


def plain_text(text: str) -> str:
    """Quita el markdown que el modelo insiste en usar (**negritas**, `código`, # títulos):
    Telegram y la terminal lo muestran literal."""
    out_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            line = stripped.lstrip("#").strip()
        out_lines.append(line.replace("**", "").replace("`", ""))
    return "\n".join(out_lines).strip()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


async def _record(done: StreamDone, *, user_id: int) -> None:
    """Persiste la llamada en `llm_calls` (purpose `assistant_brain`) sin bloquear el loop."""
    try:
        from memex.core.observability import record_llm_call

        await asyncio.to_thread(
            record_llm_call,
            user_id=user_id,
            purpose="assistant_brain",
            model=done.model,
            prompt_tokens=done.usage.prompt_tokens,
            completion_tokens=done.usage.completion_tokens,
            cost_usd=done.cost_usd,
            latency_ms=done.latency_ms,
            status="ok",
            cache_hit_tokens=done.usage.cache_hit_tokens,
        )
    except Exception as e:  # la contabilidad no puede tumbar el turno
        _log.warning("assistant.brain.record_failed", error=str(e)[:200])


class RoutedBrain:
    """Fotos → `fallback` (codex ve imágenes); lo demás → `primary` (API) y, si la API falla con
    un `LLMError`, `fallback`. Sin fallback configurado, el error de la API propaga."""

    def __init__(self, *, primary: Brain, fallback: Brain | None) -> None:
        self._primary = primary
        self._fallback = fallback

    async def run(self, req: TurnRequest) -> TurnResult:
        if req.images and self._fallback is not None:
            return await self._fallback.run(req)
        try:
            return await self._primary.run(req)
        except LLMError as e:
            if self._fallback is None or not e.fallback_ok:
                raise
            _log.warning(
                "assistant.brain.fallback",
                reason=str(e)[:200],
                status=getattr(e, "status_code", 0),
            )
            return await self._fallback.run(req)


__all__ = [
    "TOOLS",
    "ApiBrain",
    "Executor",
    "RoutedBrain",
    "plain_text",
    "system_prompt",
]
