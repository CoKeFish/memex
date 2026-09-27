"""La lógica del asistente, independiente del transporte (Telegram hoy; cualquier canal que entregue
`Incoming` y acepte `Outgoing` mañana). Flujo por mensaje:

1. allowlist (solo los ids configurados);
2. un turno a la vez por chat (lock de la sesión);
3. si hay escrituras pendientes y el mensaje es «sí»/«no» (o el botón), ejecutar/cancelar;
4. si es audio, transcribir (seam `stt`); si es foto, va como imagen del prompt;
5. turno del cerebro (API con herramientas y streaming por default; codex para fotos y como
   fallback); ante cuota/ocupado de codex avisa y ESPERA reintentando (regla del proyecto);
6. la respuesta al usuario (pintada en vivo si el canal dio un `Streamer`) + las escrituras
   propuestas con botones de confirmación.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from memex.assistant.brain import (
    Brain,
    CodexBusyError,
    CodexError,
    CodexUnavailableError,
    TurnRequest,
    TurnResult,
)
from memex.assistant.executor import (
    CommandExecutor,
    CommandRejected,
    for_humans,
    matches_prefix,
)
from memex.assistant.humanize import describe, plain_detail
from memex.assistant.session import ChatSession, SessionStore, parse_confirmation
from memex.assistant.stt import SttError, Transcriber
from memex.llm.client import LLMError, LLMQuotaError
from memex.logging import get_logger

_log = get_logger("memex.assistant.gateway")

CONFIRM = "memex:confirm"
CANCEL = "memex:cancel"


def _token(data: str, prefix: str) -> str | None:
    """`memex:confirm:ab12cd` → `ab12cd`; `memex:confirm` (botón viejo) → None = la más reciente."""
    rest = data[len(prefix) :].lstrip(":").strip()
    return rest or None


Kind = Literal["text", "voice", "photo", "callback"]


@dataclass(frozen=True)
class Incoming:
    chat_id: int
    user_id: int
    kind: Kind
    text: str = ""
    audio: Path | None = None
    images: Sequence[Path] = ()
    callback_data: str | None = None


@dataclass
class Outgoing:
    text: str
    #: (etiqueta, callback_data) → botones inline; vacío = sin botones.
    buttons: list[tuple[str, str]] = field(default_factory=list)


Sender = Callable[[Outgoing], Awaitable[None]]


class Streamer(Protocol):
    """Salida PROGRESIVA de un turno (opcional, la da el canal): `update` recibe el texto
    acumulado mientras el cerebro lo genera; `finish` recibe la respuesta final y devuelve True
    si la pintó (entonces el gateway no la vuelve a mandar con `send`)."""

    async def update(self, text: str) -> None: ...

    async def finish(self, out: Outgoing) -> bool: ...


class Gateway:
    def __init__(
        self,
        *,
        brain: Brain,
        transcriber: Transcriber,
        executor: CommandExecutor,
        sessions: SessionStore,
        allowed_ids: frozenset[int],
        codex_wait_s: float = 300.0,
        codex_wait_max_s: float = 6 * 3600.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        auto_execute: Sequence[str] = (),
    ) -> None:
        self._brain = brain
        self._stt = transcriber
        self._executor = executor
        self._sessions = sessions
        self._allowed = allowed_ids
        self._wait_s = codex_wait_s
        self._wait_max_s = codex_wait_max_s
        self._sleep = sleep
        #: Escrituras `<dominio> <verbo>` que se ejecutan apenas el cerebro las propone, sin
        #: confirmación (`AssistantSettings.auto_execute`); el resto sigue pidiendo el botón.
        self._auto_execute = tuple(auto_execute)

    async def handle(self, inc: Incoming, send: Sender, streamer: Streamer | None = None) -> None:
        if inc.user_id not in self._allowed:
            _log.warning("assistant.gateway.unauthorized", user_id=inc.user_id, chat_id=inc.chat_id)
            await send(
                Outgoing(
                    f"No estás en la lista de usuarios autorizados. Tu id de Telegram es "
                    f"{inc.user_id}: agregalo a MEMEX_ASSISTANT_ALLOWED_USER_IDS para habilitarte."
                )
            )
            return
        session = self._sessions.get(inc.chat_id)
        async with session.lock:
            if inc.kind == "callback":
                await self._handle_callback(session, inc.callback_data or "", send)
                return
            if inc.kind == "text" and session.pending_sets:
                # «sí» / «no» escritos van a la propuesta MÁS RECIENTE; cada botón, a la suya.
                decision = parse_confirmation(inc.text)
                if decision == "yes":
                    await self._execute_pending(session, send, None)
                    return
                if decision == "no":
                    await self._cancel_pending(session, send, None)
                    return
            await self._turn(session, inc, send, streamer)

    # ------------------------------------------------------------------ confirmación
    async def _handle_callback(self, session: ChatSession, data: str, send: Sender) -> None:
        """`memex:confirm:<token>` / `memex:cancel:<token>`: cada botón actúa sobre SU propuesta.
        Sin token (botones viejos, /cancel) actúa sobre la más reciente."""
        if data.startswith(CONFIRM):
            await self._execute_pending(session, send, _token(data, CONFIRM))
        elif data.startswith(CANCEL):
            await self._cancel_pending(session, send, _token(data, CANCEL))
        else:
            await send(Outgoing("Botón desconocido."))

    async def _cancel_pending(self, session: ChatSession, send: Sender, token: str | None) -> None:
        commands = session.take_pending(token)
        n = len(commands or [])
        session.add("asistente", "(el usuario canceló las escrituras propuestas)")
        await send(
            Outgoing(f"Cancelado: {n} comando(s) descartado(s)." if n else "Nada que cancelar.")
        )

    async def _execute_pending(self, session: ChatSession, send: Sender, token: str | None) -> None:
        commands = session.take_pending(token)
        if commands is None:
            await send(Outgoing("No hay nada pendiente de confirmar."))
            return
        blocks = await self._execute(session, commands)
        await send(Outgoing("\n\n".join(blocks) if blocks else "Nada que ejecutar."))

    async def _execute(self, session: ChatSession, commands: Sequence[str]) -> list[str]:
        """Corre los comandos por el executor y devuelve un bloque de texto por comando, para
        una PERSONA: `✓` + qué se hizo en palabras (`describe`) + la salida del CLI sin códigos
        internos (`plain_detail`; se le quita `--json`, que el modelo pide por costumbre). El
        comando exacto queda en el log y en el historial del chat, no en la pantalla."""
        blocks: list[str] = []
        for cmd in commands:
            # En el chat va lo que se hizo, en palabras; el comando exacto queda en el log.
            que = describe(cmd)
            try:
                res = await self._executor.run(for_humans(cmd))
            except CommandRejected as e:
                blocks.append(f"✗ {que}\n  no se ejecutó: {e}")
                session.add("asistente", f"(rechacé ejecutar `{cmd}`: {e})")
                _log.warning("assistant.gateway.rejected", command=cmd[:300], error=str(e))
                continue
            mark = "✓" if res.ok else "✗"
            detail = plain_detail(res.output) if res.ok else res.output.strip()[:300]
            blocks.append(f"{mark} {que}" + (f"\n  {detail}" if detail else ""))
            session.add(
                "asistente",
                f"(ejecuté `{cmd}` → exit {res.exit_code}: {res.output[:300]})",
            )
            _log.info(
                "assistant.gateway.executed",
                command=cmd[:300],
                exit_code=res.exit_code,
                output=res.output[:500],
            )
        return blocks

    # ------------------------------------------------------------------ turno
    async def _turn(
        self, session: ChatSession, inc: Incoming, send: Sender, streamer: Streamer | None
    ) -> None:
        # Un pedido nuevo NO descarta lo pendiente: cada propuesta sigue viva con sus botones
        # (hasta MAX_PENDING_SETS); el cerebro las ve como pendientes y no las vuelve a proponer.
        prefix = ""
        text = inc.text.strip()
        if inc.kind == "voice":
            if inc.audio is None:
                await send(Outgoing("Recibí un audio vacío."))
                return
            try:
                transcript = await self._stt.transcribe(inc.audio)
            except SttError as e:
                await send(Outgoing(f"No pude transcribir el audio: {e}"))
                return
            if not transcript:
                await send(Outgoing("No entendí nada en el audio (transcripción vacía)."))
                return
            prefix = f"🎙️ «{transcript}»\n\n"
            text = f"{transcript}\n{text}".strip() if text else transcript
        if inc.kind == "photo" and not text:
            text = (
                "Foto adjunta sin texto: interpretala (recibo, pantallazo, invitación) y "
                "proponé lo que corresponda."
            )
        if not text:
            await send(Outgoing("Mandame texto, una nota de voz o una foto."))
            return
        shown = text if inc.kind != "photo" else f"[foto] {text}"
        on_text: Callable[[str], Awaitable[None]] | None = None
        if streamer is not None:
            live = streamer

            async def _on_text(partial: str) -> None:
                await live.update(prefix + partial)

            on_text = _on_text
        req = TurnRequest(
            text=text,
            images=list(inc.images),
            history=session.snapshot(),
            pending=tuple(session.pending),
            auto_execute=self._auto_execute,
            on_text=on_text,
        )
        result = await self._run_with_wait(req, send)
        if result is None:
            return
        session.add("usuario", shown)
        session.add("asistente", result.reply)
        auto = [c for c in result.proposed_commands if matches_prefix(c, self._auto_execute)]
        to_confirm = [c for c in result.proposed_commands if c not in auto]
        _log.info(
            "assistant.gateway.turn",
            chat_id=session.chat_id,
            kind=inc.kind,
            text=text[:2000],
            reply=result.reply[:4000],
            commands=[c for c, _ in result.commands][:8],
            proposed=to_confirm[:4],
            auto=auto[:4],
        )
        out = prefix + (result.reply or "(sin respuesta)")
        if auto:
            # Escrituras de la lista `auto_execute`: se corren ya y se muestra el resultado.
            out += "\n\n" + "\n\n".join(await self._execute(session, auto))
        buttons: list[tuple[str, str]] = []
        if to_confirm:
            token = session.add_pending(to_confirm)
            # Lo que se va a hacer, en palabras; los comandos exactos ya están en el log de arriba.
            out += "\n\nSi confirmás, hago esto:\n" + "\n".join(
                f"  • {describe(c)}" for c in to_confirm
            )
            out += "\n\n¿Confirmás? (sí / no)"
            buttons = [("✅ Confirmar", f"{CONFIRM}:{token}"), ("❌ Cancelar", f"{CANCEL}:{token}")]
        if result.unconfirmed_writes:
            out += "\n\n⚠️ El agente ejecutó escrituras sin pedir confirmación:\n" + "\n".join(
                f"  {c}" for c in result.unconfirmed_writes
            )
        if result.failed_commands:
            out += f"\n\n(nota: {len(result.failed_commands)} comando(s) del agente fallaron)"
        final = Outgoing(out, buttons)
        if streamer is not None and await streamer.finish(final):
            return
        await send(final)

    async def _run_with_wait(self, req: TurnRequest, send: Sender) -> TurnResult | None:
        waited = 0.0
        attempts = 0
        while True:
            try:
                return await self._brain.run(req)
            except CodexBusyError as e:
                if waited >= self._wait_max_s:
                    await send(
                        Outgoing(
                            "codex sigue sin cuota después de "
                            f"{waited / 3600:.1f} h; volvé a mandar el mensaje más tarde."
                        )
                    )
                    _log.error("assistant.gateway.wait_exhausted", waited_s=int(waited))
                    return None
                if attempts == 0:
                    await send(
                        Outgoing(
                            "codex está en cuota u ocupado. Sigo intentando cada "
                            f"{self._wait_s / 60:.0f} min (hasta {self._wait_max_s / 3600:.0f} h) "
                            "y te respondo cuando salga."
                        )
                    )
                _log.warning(
                    "assistant.gateway.waiting",
                    reason=(e.body or str(e))[:200],
                    waited_s=int(waited),
                )
                attempts += 1
                await self._sleep(self._wait_s)
                waited += self._wait_s
            except CodexUnavailableError as e:
                await send(
                    Outgoing(
                        "codex no está disponible (sesión/modelo): hace falta intervenir.\n"
                        f"{(e.body or str(e))[:300]}"
                    )
                )
                return None
            except CodexError as e:
                await send(Outgoing(f"El turno falló: {e}"))
                return None
            except LLMQuotaError as e:
                await send(
                    Outgoing(
                        "El proveedor del cerebro no tiene saldo o cuota: hace falta "
                        f"intervenir.\n{(e.body or str(e))[:300]}"
                    )
                )
                return None
            except LLMError as e:
                await send(Outgoing(f"El turno falló: {e}"))
                return None
