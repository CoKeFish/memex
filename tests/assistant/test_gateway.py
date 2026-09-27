"""Gateway con cerebro, transcriptor y ejecutor falsos: allowlist, confirmación (botón y «sí»/«no»),
audio → transcripción → turno, foto como imagen, espera ante cuota, auditoría de escrituras."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from memex.assistant.brain import CodexBusyError, CodexUnavailableError, TurnRequest, TurnResult
from memex.assistant.executor import CommandRejected, ExecResult
from memex.assistant.gateway import CANCEL, CONFIRM, Gateway, Incoming, Outgoing
from memex.assistant.session import SessionStore
from memex.assistant.stt import SttError

ALLOWED = frozenset({7})


class FakeBrain:
    def __init__(self, results: list[TurnResult | Exception]) -> None:
        self.results = list(results)
        self.requests: list[TurnRequest] = []

    async def run(self, req: TurnRequest) -> TurnResult:
        self.requests.append(req)
        nxt = self.results.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FakeStt:
    name = "fake"

    def __init__(self, text: str | None = "pagué diez mil de uber") -> None:
        self.text = text

    async def transcribe(self, path: Path) -> str:
        if self.text is None:
            raise SttError("sin motor")
        return self.text


class FakeExecutor:
    def __init__(self) -> None:
        self.ran: list[str] = []

    async def run(self, command: str) -> ExecResult:
        if "rechazame" in command:
            raise CommandRejected("no es memex")
        self.ran.append(command)
        return ExecResult(command, 0, "registrado #9")


def _gateway(
    brain: Any,
    *,
    stt: Any = None,
    executor: Any = None,
    sleeps: list[float] | None = None,
    auto_execute: tuple[str, ...] = (),
) -> tuple[Gateway, Any]:
    ex: Any = executor or FakeExecutor()

    async def sleep(s: float) -> None:
        if sleeps is not None:
            sleeps.append(s)

    gw = Gateway(
        brain=brain,
        transcriber=stt or FakeStt(),
        executor=ex,
        sessions=SessionStore(max_turns=3),
        allowed_ids=ALLOWED,
        codex_wait_s=1.0,
        codex_wait_max_s=2.5,
        sleep=sleep,
        auto_execute=auto_execute,
    )
    return gw, ex


def _collector() -> tuple[list[Outgoing], Any]:
    outs: list[Outgoing] = []

    async def send(o: Outgoing) -> None:
        outs.append(o)

    return outs, send


@pytest.mark.asyncio
async def test_unauthorized_user_gets_refused_without_running_brain() -> None:
    brain = FakeBrain([TurnResult(reply="no debería")])
    gw, _ = _gateway(brain)
    outs, send = _collector()
    await gw.handle(Incoming(1, 99, "text", text="hola"), send)
    assert brain.requests == [] and "autorizados" in outs[0].text
    assert "99" in outs[0].text  # le dice su id para que el dueño lo habilite


@pytest.mark.asyncio
async def test_query_turn_uses_history_and_reports_failed_commands() -> None:
    brain = FakeBrain(
        [
            TurnResult(reply="Gastaste 905.797 COP", commands=[("memex finance summary", 0)]),
            TurnResult(reply="Sí", commands=[("memex x", 1)]),
        ]
    )
    gw, _ = _gateway(brain)
    outs, send = _collector()
    await gw.handle(Incoming(1, 7, "text", text="¿cuánto gasté en Uber?"), send)
    await gw.handle(Incoming(1, 7, "text", text="¿y este mes?"), send)
    assert outs[0].text == "Gastaste 905.797 COP" and outs[0].buttons == []
    assert brain.requests[1].history == [
        ("usuario", "¿cuánto gasté en Uber?"),
        ("asistente", "Gastaste 905.797 COP"),
    ]
    assert "1 comando(s) del agente fallaron" in outs[1].text


@pytest.mark.asyncio
async def test_proposed_writes_need_confirmation_then_execute() -> None:
    brain = FakeBrain(
        [
            TurnResult(
                reply="Registro 25.000 de Uber",
                proposed_commands=["memex finance register --amount 25000"],
            )
        ]
    )
    gw, ex = _gateway(brain)
    outs, send = _collector()
    await gw.handle(Incoming(1, 7, "text", text="registrá 25.000 de uber"), send)
    (confirmar, cancelar) = outs[0].buttons
    assert confirmar[0] == "✅ Confirmar" and confirmar[1].startswith(f"{CONFIRM}:")
    assert cancelar[0] == "❌ Cancelar" and cancelar[1].startswith(f"{CANCEL}:")
    # en el chat va en palabras; el comando exacto no se muestra (queda en el log)
    assert "Registrar gasto: 25.000" in outs[0].text
    assert "memex finance register" not in outs[0].text
    assert ex.ran == []  # nada ejecutado todavía
    await gw.handle(Incoming(1, 7, "callback", callback_data=confirmar[1]), send)
    assert ex.ran == ["memex finance register --amount 25000"]
    # la salida del sistema va sin códigos internos («#9»)
    assert outs[1].text == "✓ Registrar gasto: 25.000\n  registrado"
    # ya no hay pendiente: un segundo confirm (mismo botón o uno viejo sin token) no ejecuta nada
    await gw.handle(Incoming(1, 7, "callback", callback_data=confirmar[1]), send)
    await gw.handle(Incoming(1, 7, "callback", callback_data=CONFIRM), send)
    assert ex.ran == ["memex finance register --amount 25000"]
    assert "nada pendiente" in outs[2].text.lower() and "nada pendiente" in outs[3].text.lower()


@pytest.mark.asyncio
async def test_cada_propuesta_tiene_sus_botones() -> None:
    """Dos fotos seguidas (dos tarjetas de contacto): el «Confirmar» de la primera ejecuta la
    PRIMERA y el de la segunda, la segunda. Antes la segunda pisaba a la primera (2026-09-17)."""
    brain = FakeBrain(
        [
            TurnResult(
                reply="Guardo a Krunoslav", proposed_commands=["memex identidad add --name K"]
            ),
            TurnResult(
                reply="Guardo a Eduardo", proposed_commands=["memex identidad add --name E"]
            ),
        ]
    )
    gw, ex = _gateway(brain)
    outs, send = _collector()
    await gw.handle(Incoming(1, 7, "photo", images=[Path("k.jpg")]), send)
    await gw.handle(Incoming(1, 7, "photo", images=[Path("e.jpg")]), send)
    primero, segundo = outs[0].buttons[0][1], outs[1].buttons[0][1]
    assert primero != segundo
    # el cerebro del segundo turno supo que la primera seguía pendiente
    assert brain.requests[1].pending == ("memex identidad add --name K",)
    await gw.handle(Incoming(1, 7, "callback", callback_data=primero), send)
    assert ex.ran == ["memex identidad add --name K"]
    await gw.handle(Incoming(1, 7, "callback", callback_data=segundo), send)
    assert ex.ran == ["memex identidad add --name K", "memex identidad add --name E"]
    # cancelar por botón también es por propuesta
    brain.results.append(TurnResult(reply="c", proposed_commands=["memex x"]))
    await gw.handle(Incoming(1, 7, "text", text="otra"), send)
    await gw.handle(Incoming(1, 7, "callback", callback_data=outs[-1].buttons[1][1]), send)
    assert "Cancelado: 1" in outs[-1].text and ex.ran[-1] == "memex identidad add --name E"


@pytest.mark.asyncio
async def test_auto_execute_runs_receipt_without_confirmation() -> None:
    # Una factura/recibo se registra «y ya» (pedido del dueño, 2026-09-15): las escrituras de la
    # lista auto_execute se corren apenas el cerebro las propone; las demás siguen con botón.
    brain = FakeBrain(
        [
            TurnResult(
                reply="Registré 1.045.970 de PASH SAS",
                proposed_commands=[
                    "memex finance register --amount 1045970 --currency COP --json",
                    "memex calendario add --title 'Cambio de prenda'",
                ],
            )
        ]
    )
    gw, ex = _gateway(brain, auto_execute=("finance register",))
    outs, send = _collector()
    await gw.handle(Incoming(1, 7, "photo", images=[Path("factura.jpg")]), send)
    assert brain.requests[0].auto_execute == ("finance register",)
    # se ejecuta SIN --json: el chat muestra la línea humana del CLI, no el JSON ni el comando
    assert ex.ran == ["memex finance register --amount 1045970 --currency COP"]
    text = outs[0].text
    # en palabras y sin códigos internos («#9»): qué se hizo + la línea humana del CLI
    assert text.startswith(
        "Registré 1.045.970 de PASH SAS\n\n✓ Registrar gasto: 1.045.970 COP\n  registrado"
    )
    assert "--json" not in text and "memex " not in text
    # el calendario NO se ejecutó: queda a confirmación con sus botones (token propio)
    (confirmar, cancelar) = outs[0].buttons
    assert confirmar[1].startswith(f"{CONFIRM}:") and cancelar[1].startswith(f"{CANCEL}:")
    assert "Si confirmás, hago esto:\n  • Agendar: «Cambio de prenda»" in text
    await gw.handle(Incoming(1, 7, "callback", callback_data=confirmar[1]), send)
    assert ex.ran[-1] == "memex calendario add --title 'Cambio de prenda'"


@pytest.mark.asyncio
async def test_auto_execute_only_when_all_proposals_match_nothing_else_changes() -> None:
    brain = FakeBrain([TurnResult(reply="Listo", proposed_commands=["memex finance rm 9"])])
    gw, ex = _gateway(brain, auto_execute=("finance register",))
    outs, send = _collector()
    await gw.handle(Incoming(1, 7, "text", text="borrá el 9"), send)
    assert ex.ran == [] and outs[0].buttons  # un rm no está en la lista: pide confirmación


@pytest.mark.asyncio
async def test_text_yes_no_confirmation_and_rejected_commands() -> None:
    brain = FakeBrain(
        [
            TurnResult(reply="a", proposed_commands=["memex finance register --amount 1"]),
            TurnResult(
                reply="b", proposed_commands=["rechazame esto", "memex asuntos close --id 1"]
            ),
            TurnResult(reply="c", proposed_commands=["memex x"]),
            TurnResult(reply="d"),
        ]
    )
    gw, ex = _gateway(brain)
    outs, send = _collector()
    await gw.handle(Incoming(1, 7, "text", text="uno"), send)
    await gw.handle(Incoming(1, 7, "text", text="no"), send)
    assert ex.ran == [] and "Cancelado: 1" in outs[1].text
    await gw.handle(Incoming(1, 7, "text", text="dos"), send)
    await gw.handle(Incoming(1, 7, "text", text="sí"), send)
    assert ex.ran == ["memex asuntos close --id 1"]
    assert "no se ejecutó" in outs[3].text and "✓ Cerrar el asunto #1" in outs[3].text
    assert "memex asuntos close" not in outs[3].text
    # un pedido nuevo (no «sí»/«no») abre otro turno y NO descarta lo pendiente: sigue con sus
    # botones, y el cerebro lo ve como pendiente
    await gw.handle(Incoming(1, 7, "text", text="tres"), send)
    await gw.handle(Incoming(1, 7, "text", text="otra cosa distinta, gracias"), send)
    assert len(brain.requests) == 4 and brain.results == []  # el 4.o mensaje fue un turno nuevo
    assert brain.requests[3].pending == ("memex x",)
    assert ex.ran == ["memex asuntos close --id 1"]
    assert outs[-1].text == "d" and outs[-1].buttons == []
    await gw.handle(Incoming(1, 7, "text", text="sí"), send)  # «sí» va a la más reciente viva
    assert ex.ran == ["memex asuntos close --id 1", "memex x"]


@pytest.mark.asyncio
async def test_voice_is_transcribed_and_echoed() -> None:
    brain = FakeBrain(
        [
            TurnResult(
                reply="Propongo registrar",
                proposed_commands=["memex finance register --amount 10000"],
            )
        ]
    )
    gw, _ = _gateway(brain)
    outs, send = _collector()
    await gw.handle(Incoming(1, 7, "voice", audio=Path("nota.ogg")), send)
    assert brain.requests[0].text == "pagué diez mil de uber"
    assert outs[0].text.startswith("🎙️ «pagué diez mil de uber»")
    gw2, _ = _gateway(FakeBrain([]), stt=FakeStt(None))
    outs2, send2 = _collector()
    await gw2.handle(Incoming(1, 7, "voice", audio=Path("nota.ogg")), send2)
    assert "No pude transcribir" in outs2[0].text


@pytest.mark.asyncio
async def test_photo_goes_as_image_with_default_text_and_write_audit() -> None:
    brain = FakeBrain(
        [
            TurnResult(
                reply="Es un recibo", commands=[("uv run memex finance register --amount 5", 0)]
            )
        ]
    )
    gw, _ = _gateway(brain)
    outs, send = _collector()
    await gw.handle(Incoming(1, 7, "photo", images=[Path("recibo.jpg")]), send)
    req = brain.requests[0]
    assert list(req.images) == [Path("recibo.jpg")] and "Foto adjunta" in req.text
    assert "ejecutó escrituras sin pedir confirmación" in outs[0].text


@pytest.mark.asyncio
async def test_busy_waits_then_answers_and_unavailable_reports() -> None:
    sleeps: list[float] = []
    brain = FakeBrain(
        [
            CodexBusyError(0, "busy", body="usage limit"),
            CodexBusyError(0, "busy", body="usage limit"),
            TurnResult(reply="ahora sí"),
            CodexUnavailableError(0, "gone", body="please log in"),
        ]
    )
    gw, _ = _gateway(brain, sleeps=sleeps)
    outs, send = _collector()
    await gw.handle(Incoming(1, 7, "text", text="hola"), send)
    assert sleeps == [1.0, 1.0]
    assert "en cuota" in outs[0].text and outs[-1].text == "ahora sí"
    await gw.handle(Incoming(1, 7, "text", text="hola"), send)
    assert "hace falta intervenir" in outs[-1].text
