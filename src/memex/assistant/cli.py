"""memex-assistant — el asistente conversacional (Telegram + cerebro LLM + CLI memex).

Comandos:
  run      arranca el bot de Telegram (bloquea; polling o webhook según config)
  turn     un turno SIN Telegram, desde la terminal (texto, --image, --audio): para probar el
           cerebro, la transcripción y la confirmación de escrituras igual que en el chat
  doctor   verifica token, allowlist, cerebro (API y codex), transcripción y el CLI memex
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from memex.agent_cli import help_text
from memex.assistant.api_brain import ApiBrain, RoutedBrain
from memex.assistant.brain import Brain, CodexBrain, CodexUnavailableError
from memex.assistant.config import AssistantSettings
from memex.assistant.executor import CommandExecutor
from memex.assistant.gateway import CANCEL, CONFIRM, Gateway, Incoming, Outgoing
from memex.assistant.session import SessionStore
from memex.assistant.stt import SttError, build_transcriber
from memex.db import connection
from memex.llm.client import LLMError
from memex.llm.config import LLMConfigError
from memex.llm.registry import build_provider_client
from memex.llm.settings import get_consumer_settings
from memex.llm.tools import ToolStreamClient
from memex.logging import get_logger, setup_logging

_log = get_logger("memex.assistant.cli")


def _say(msg: str) -> None:
    enc = sys.stdout.encoding or "utf-8"
    print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))


def _codex_brain(settings: AssistantSettings) -> CodexBrain | None:
    try:
        return CodexBrain(
            help_text=help_text(),
            memex_invocation=" ".join(settings.memex_argv),
            workdir=settings.workdir_path,
            model=settings.codex_model or None,
            timeout_s=settings.codex_timeout_s,
        )
    except CodexUnavailableError as e:
        _log.warning("assistant.cli.codex_unavailable", error=str(e)[:200])
        return None


def _brain_provider(settings: AssistantSettings) -> tuple[str, str | None]:
    """Proveedor+modelo del cerebro `api`: override por env, si no la fila `assistant_brain` de
    `llm_consumer_settings` (o la `default`), si no DeepSeek con su modelo default."""
    if settings.brain_provider.strip():
        return settings.brain_provider.strip(), settings.brain_model.strip() or None
    try:
        with connection() as conn:
            cs = get_consumer_settings(conn, 1, "assistant_brain")
    except Exception as e:  # sin DB a mano (p. ej. doctor fuera de red): default razonable
        _log.warning("assistant.cli.consumer_settings_unavailable", error=str(e)[:200])
        return "deepseek", settings.brain_model.strip() or None
    return cs.provider, settings.brain_model.strip() or cs.model


def _build_brain(settings: AssistantSettings) -> Brain:
    codex = _codex_brain(settings)
    if settings.brain == "codex":
        if codex is None:
            raise SystemExit("MEMEX_ASSISTANT_BRAIN=codex pero el binario `codex` no está en PATH.")
        return codex
    provider, model = _brain_provider(settings)
    client = build_provider_client(provider, model=model)
    if not isinstance(client, ToolStreamClient):
        raise SystemExit(
            f"el proveedor {provider!r} no soporta herramientas+streaming; usá deepseek u openai "
            "(MEMEX_ASSISTANT_BRAIN_PROVIDER) o MEMEX_ASSISTANT_BRAIN=codex."
        )
    api = ApiBrain(
        client=client,
        help_text=help_text(),
        executor=CommandExecutor(
            settings.memex_argv, workdir=settings.workdir_path, output_limit=12000
        ),
        model=model,
        max_tool_rounds=settings.brain_max_tool_rounds,
    )
    _log.info(
        "assistant.cli.brain",
        engine="api",
        provider=provider,
        model=model or "",
        codex_fallback=codex is not None,
    )
    return RoutedBrain(primary=api, fallback=codex)


def _build_gateway(
    settings: AssistantSettings, *, allowed: frozenset[int] | None = None
) -> Gateway:
    brain = _build_brain(settings)
    return Gateway(
        brain=brain,
        transcriber=build_transcriber(settings),
        executor=CommandExecutor(settings.memex_argv, workdir=settings.workdir_path),
        sessions=SessionStore(max_turns=settings.history_turns),
        allowed_ids=allowed if allowed is not None else settings.allowed_ids,
        codex_wait_s=settings.codex_wait_s,
        codex_wait_max_s=settings.codex_wait_max_s,
        auto_execute=settings.auto_execute_prefixes,
    )


def _cmd_run(settings: AssistantSettings) -> int:
    if not settings.telegram_bot_token:
        _say("Falta MEMEX_ASSISTANT_TELEGRAM_BOT_TOKEN (token de BotFather).")
        return 2
    if not settings.allowed_ids:
        # Modo onboarding: sin allowlist el bot solo le dice a cada quien su id (para que el
        # dueño lo ponga en MEMEX_ASSISTANT_ALLOWED_USER_IDS); no razona ni escribe nada.
        _say(
            "MEMEX_ASSISTANT_ALLOWED_USER_IDS vacío: el bot solo responderá con el id de quien "
            "le escriba (onboarding). Agregá tu id y reiniciá."
        )
    from memex.assistant.telegram import TelegramBot

    TelegramBot(settings, _build_gateway(settings)).run()
    return 0


class _ConsoleStreamer:
    """Streaming en la terminal: imprime cada pedazo nuevo; al final completa lo que faltaba
    (comandos propuestos, botones)."""

    def __init__(self) -> None:
        self._shown = ""

    async def update(self, text: str) -> None:
        if text.startswith(self._shown):
            sys.stdout.write(text[len(self._shown) :])
        else:
            sys.stdout.write("\n" + text)
        sys.stdout.flush()
        self._shown = text

    async def finish(self, out: Outgoing) -> bool:
        if not self._shown:
            return False
        rest = out.text[len(self._shown) :] if out.text.startswith(self._shown) else out.text
        _say(rest)
        if out.buttons:
            _say("[botones: " + " | ".join(label for label, _ in out.buttons) + "]")
        return True


async def _turn(settings: AssistantSettings, args: argparse.Namespace) -> int:
    gateway = _build_gateway(settings, allowed=frozenset({0}))

    async def send(out: Outgoing) -> None:
        _say(out.text)
        if out.buttons:
            _say("[botones: " + " | ".join(label for label, _ in out.buttons) + "]")

    chat = 0
    images = [Path(p) for p in args.image or []]
    if args.audio:
        inc = Incoming(chat, 0, "voice", text=args.text or "", audio=Path(args.audio))
    elif images:
        inc = Incoming(chat, 0, "photo", text=args.text or "", images=images)
    else:
        if not args.text:
            _say("Falta el texto (o --image / --audio).")
            return 2
        inc = Incoming(chat, 0, "text", text=args.text)
    await gateway.handle(inc, send, _ConsoleStreamer())
    if args.confirm:
        await gateway.handle(Incoming(chat, 0, "callback", callback_data=CONFIRM), send)
    elif args.cancel:
        await gateway.handle(Incoming(chat, 0, "callback", callback_data=CANCEL), send)
    return 0


def _cmd_doctor(settings: AssistantSettings) -> int:
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        _say(f"  [{'ok' if good else 'XX'}] {label}" + (f" — {detail}" if detail else ""))

    _say("memex-assistant doctor")
    check(
        "token de Telegram", bool(settings.telegram_bot_token), "MEMEX_ASSISTANT_TELEGRAM_BOT_TOKEN"
    )
    check(
        "allowlist",
        bool(settings.allowed_ids),
        ", ".join(str(i) for i in sorted(settings.allowed_ids))
        or "MEMEX_ASSISTANT_ALLOWED_USER_IDS vacío",
    )
    check(
        "transporte",
        True,
        settings.transport
        + (f" → {settings.webhook_url}" if settings.transport == "webhook" else ""),
    )
    codex = shutil.which("codex")
    version = ""
    if codex:
        try:
            version = subprocess.run(
                [codex, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            version = ""
    check(
        "codex en PATH" + (" (fotos + fallback)" if settings.brain == "api" else ""),
        bool(codex and version),
        version or "no encontrado",
    )
    # Sin este binario (viene aparte en el release) codex NO ejecuta ningún comando: sale con
    # exit 0 y el modelo contesta «no pude acceder al CLI» (visto el 2026-09-15 con una factura).
    code_mode_host = shutil.which("codex-code-mode-host")
    check(
        "codex-code-mode-host en PATH (sin él codex no corre comandos)",
        bool(code_mode_host),
        code_mode_host or "no encontrado",
    )
    if settings.brain == "api":
        provider, model = _brain_provider(settings)
        try:
            client = build_provider_client(provider, model=model)
            check(
                "cerebro api (herramientas + streaming)",
                isinstance(client, ToolStreamClient),
                f"{provider} · {model or 'modelo default'}",
            )
        except (LLMConfigError, LLMError) as e:
            check("cerebro api", False, str(e)[:160])
    else:
        check("cerebro", True, "codex (MEMEX_ASSISTANT_BRAIN=codex)")
    try:
        stt = build_transcriber(settings)
        engine = {
            "openai": settings.stt_openai_model,
            "local": settings.stt_local_model,
            "off": "deshabilitada",
        }[settings.stt]
        check("transcripción", True, f"{stt.name} ({engine})")
    except SttError as e:
        check("transcripción", False, str(e))
    try:
        out = subprocess.run(
            [*settings.memex_argv, "help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            cwd=str(settings.workdir_path),
        )
        check(
            "CLI memex",
            out.returncode == 0 and "memex" in (out.stdout or ""),
            " ".join(settings.memex_argv),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        check("CLI memex", False, str(e))
    check("workdir", settings.workdir_path.exists(), str(settings.workdir_path))
    check(
        "escrituras sin confirmación (MEMEX_ASSISTANT_AUTO_EXECUTE)",
        True,
        ", ".join(f"memex {p}" for p in settings.auto_execute_prefixes) or "ninguna",
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    # httpx/httpcore logean cada request a INFO con la URL completa, que incluye el token del bot:
    # solo warnings para no filtrar el secreto a los logs.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(
        prog="memex-assistant",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="Arranca el bot de Telegram.")
    turn = sub.add_parser("turn", help="Un turno desde la terminal, sin Telegram.")
    turn.add_argument("text", nargs="?", default="", help="El pedido en lenguaje natural.")
    turn.add_argument("--image", action="append", help="Imagen adjunta (repetible).")
    turn.add_argument("--audio", help="Nota de voz (ogg/mp3/wav) a transcribir.")
    turn.add_argument("--confirm", action="store_true", help="Confirma las escrituras propuestas.")
    turn.add_argument("--cancel", action="store_true", help="Cancela las escrituras propuestas.")
    sub.add_parser("doctor", help="Verifica la configuración.")
    args = parser.parse_args(argv)
    settings = AssistantSettings()
    if args.cmd == "run":
        return _cmd_run(settings)
    if args.cmd == "turn":
        return asyncio.run(_turn(settings, args))
    if args.cmd == "doctor":
        return _cmd_doctor(settings)
    return 2


if __name__ == "__main__":
    sys.exit(main())
