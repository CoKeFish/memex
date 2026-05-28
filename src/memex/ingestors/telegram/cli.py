"""CLI `memex-telegram` — auth, run, discover, listen.

Subcomandos:

  auth      — interactiva (SMS). Crea/renueva el session file de Telethon.
              Bloquea pidiendo el código por stdin. Run-once por VPS.
  run       — ejecuta UNA pasada de polling para los sources tipo `telegram`
              registrados en memex (filtrable por --source-id). Mismo patrón
              que `python -m memex.ingestors.imap.cli run`.
  discover  — lista los dialogs (chats) accesibles desde la session
              actual, imprimiendo `chat_id` + nombre + tipo. Útil para
              poblar `allowed_chats` en `sources.config`. NO persiste nada.
  listen    — dev/debug: escucha los chats `streaming=True` en vivo e imprime
              cada mensaje a stdout. NO persiste — sirve para verificar
              allowlist/topics sin tocar la DB. En producción el listener
              corre dentro del lifespan de FastAPI (`StreamingRunner`), NO acá.

Reads `MEMEX_BASE_URL` y `MEMEX_API_TOKEN` del entorno (con `.env` auto-load).

Exit code 0 si todo OK; 1 si hubo errores fatales (auth perdida, sources
sin config, API memex unreachable). Errores por record se cuentan en stats,
no son fatales.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from dotenv import load_dotenv
from telethon import TelegramClient

from memex.core.cursors import TelegramCursor
from memex.core.observability import ingestion_run
from memex.core.source import SourceConfigError, SourceRecord
from memex.ingestors.memex_server_client import MemexAPIError, MemexServerClient
from memex.ingestors.runner import run_ingestor
from memex.ingestors.telegram.client import TelegramClientWrapper
from memex.ingestors.telegram.config import TelegramConfig, TelegramConfigError
from memex.ingestors.telegram.source import make_source
from memex.ingestors.telegram.streaming import TelegramStreamingSource
from memex.logging import get_logger, setup_logging


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-telegram")
    sub = parser.add_subparsers(dest="cmd", required=True)

    auth_p = sub.add_parser(
        "auth",
        help="Interactive Telethon auth (SMS) — creates the session file. Run-once per VPS.",
    )
    auth_p.add_argument(
        "--source-id",
        type=int,
        required=True,
        help="Source id whose config tells us api_id/api_hash/phone (via env vars).",
    )

    run_p = sub.add_parser("run", help="Run a single polling pass.")
    run_p.add_argument(
        "--source-id",
        type=int,
        default=None,
        help="Only run for this source id; default = all enabled telegram sources.",
    )
    run_p.add_argument(
        "--chunk-size",
        type=int,
        default=20,
        help="Records per /ingest/batch chunk (default 20).",
    )
    run_p.add_argument(
        "--chunk-sleep-ms",
        type=int,
        default=100,
        help="Sleep between chunks in ms (default 100).",
    )

    disc_p = sub.add_parser(
        "discover",
        help="List dialogs (chats) accessible from the current session — read-only.",
    )
    disc_p.add_argument(
        "--source-id",
        type=int,
        required=True,
        help="Source id whose session we open to enumerate dialogs.",
    )
    disc_p.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Max dialogs to list (default 200).",
    )

    listen_p = sub.add_parser(
        "listen",
        help="Dev/debug: stream streaming=True chats live to stdout (no persistence).",
    )
    listen_p.add_argument(
        "--source-id",
        type=int,
        required=True,
        help="Source id whose streaming chats we listen to.",
    )

    return parser


def _select_sources(client: MemexServerClient, source_id: int | None) -> list[dict[str, Any]]:
    all_tg = client.get_sources_by_type("telegram")
    if source_id is None:
        return all_tg
    return [s for s in all_tg if int(s.get("id", 0)) == source_id]


def _resolve_config(src: dict[str, Any], log: Any) -> TelegramConfig | None:
    """Returns the resolved TelegramConfig, or None if config is invalid (already logged)."""
    cfg_dict = src.get("config", {}) or {}
    try:
        return TelegramConfig.from_source_config(cfg_dict)
    except TelegramConfigError as e:
        log.error(
            "telegram.cli.config_invalid",
            source_id=src.get("id"),
            reason=str(e),
        )
        return None
    except SourceConfigError as e:
        log.error(
            "telegram.cli.config_invalid",
            source_id=src.get("id"),
            reason=str(e),
        )
        return None


def _cmd_run(args: argparse.Namespace, client: MemexServerClient, log: Any) -> int:
    sources = _select_sources(client, args.source_id)
    if not sources:
        if args.source_id is not None:
            log.error("telegram.cli.source.not_found", source_id=args.source_id)
            return 1
        log.info("telegram.cli.no_sources_found")
        return 0

    had_fatal = False
    for src in sources:
        sid = int(src["id"])
        uid = int(src["user_id"])
        name = str(src.get("name", "unknown"))
        cfg_dict = src.get("config", {}) or {}

        with ingestion_run(user_id=uid, source_id=sid, trigger="cli") as run:
            try:
                source = make_source(cfg_dict)
            except SourceConfigError as e:
                log.error(
                    "telegram.cli.source.config_invalid",
                    source_name=name,
                    reason=str(e),
                )
                run.fail(e)
                had_fatal = True
                continue

            try:
                stats = run_ingestor(
                    source,
                    source_id=sid,
                    sink=client,
                    chunk_size=args.chunk_size,
                    chunk_sleep_ms=args.chunk_sleep_ms,
                )
                run.finalize(stats)
            except Exception as e:
                run.fail(e)
                had_fatal = True

    return 1 if had_fatal else 0


def _cmd_auth(args: argparse.Namespace, client: MemexServerClient, log: Any) -> int:
    """Interactive Telethon auth — blocks for SMS code on stdin."""
    sources = _select_sources(client, args.source_id)
    if not sources:
        log.error("telegram.cli.source.not_found", source_id=args.source_id)
        return 1
    cfg = _resolve_config(sources[0], log)
    if cfg is None:
        return 1

    cfg.session_path.mkdir(parents=True, exist_ok=True)

    print(f"\nIniciando flujo de autorización Telethon — se enviará SMS a {cfg.phone_masked}.")
    # Solo mostramos el nombre del file, no el path absoluto (que puede leak
    # estructura del filesystem en logs de container/cron).
    print(f"  session file: {cfg.session_file.name}.session\n")

    async def _do_auth() -> int:
        tg = TelegramClient(str(cfg.session_file), cfg.api_id, cfg.api_hash)
        try:
            await tg.start(phone=lambda: cfg.phone)
            me = await tg.get_me()
            log.info(
                "telegram.cli.auth.ok",
                source_id=args.source_id,
                user_id=getattr(me, "id", None),
            )
            print(f"\nAutorización OK. Session guardada como {cfg.session_file.name}.session")
            return 0
        except Exception as e:
            log.error(
                "telegram.cli.auth.failed",
                source_id=args.source_id,
                exc_type=type(e).__name__,
                exc_msg=str(e),
            )
            print(
                "\nAuth falló. Revisá los logs estructurados para el detalle.",
                file=sys.stderr,
            )
            return 1
        finally:
            await tg.disconnect()

    return asyncio.run(_do_auth())


def _cmd_discover(args: argparse.Namespace, client: MemexServerClient, log: Any) -> int:
    """List accessible dialogs — read-only, no persistence."""
    sources = _select_sources(client, args.source_id)
    if not sources:
        log.error("telegram.cli.source.not_found", source_id=args.source_id)
        return 1
    cfg = _resolve_config(sources[0], log)
    if cfg is None:
        return 1

    async def _do_discover() -> int:
        try:
            async with TelegramClientWrapper(cfg) as tc:
                count = 0
                print(f"\n{'chat_id':>20s}  {'kind':<10s}  name")
                print("-" * 80)
                async for dialog in tc.iter_dialogs():
                    if count >= args.limit:
                        break
                    kind = (
                        "channel"
                        if getattr(dialog, "is_channel", False)
                        else "group"
                        if getattr(dialog, "is_group", False)
                        else "user"
                    )
                    dialog_id = getattr(dialog, "id", "?")
                    name = getattr(dialog, "name", None) or "(unnamed)"
                    print(f"{dialog_id!s:>20s}  {kind:<10s}  {name}")
                    count += 1
                print(f"\nlisted {count} dialog(s)")
            return 0
        except Exception as e:
            log.error(
                "telegram.cli.discover.failed",
                source_id=args.source_id,
                exc_type=type(e).__name__,
                exc_msg=str(e),
            )
            return 1

    return asyncio.run(_do_discover())


def _cmd_listen(args: argparse.Namespace, client: MemexServerClient, log: Any) -> int:
    """Dev/debug: escucha chats streaming en vivo e imprime a stdout.

    NO persiste — solo imprime. Para depurar allowlist/topics. El cursor
    inicial se lee de memex vía HTTP para que el catchup arranque del punto
    guardado (sin re-imprimir todo el historial).
    """
    sources = _select_sources(client, args.source_id)
    if not sources:
        log.error("telegram.cli.source.not_found", source_id=args.source_id)
        return 1
    cfg = _resolve_config(sources[0], log)
    if cfg is None:
        return 1

    source = TelegramStreamingSource(cfg)
    raw_cursor = client.get_checkpoint(args.source_id) or {}
    cursor = TelegramCursor.model_validate(raw_cursor)

    async def _print(record: SourceRecord) -> None:
        text = str(record.payload.get("text", ""))[:100]
        print(f"[{record.external_id}] {text}")

    async def _run() -> None:
        print("\nCatchup desde el cursor guardado...\n")
        async for rec in source.catchup(cursor):
            await _print(rec)
        print("\nEscuchando en vivo (Ctrl+C para salir)...\n")
        await source.listen(_print)

    try:
        asyncio.run(_run())
        return 0
    except KeyboardInterrupt:
        print("\nDetenido.")
        return 0
    except Exception as e:
        log.error(
            "telegram.cli.listen.failed",
            source_id=args.source_id,
            exc_type=type(e).__name__,
            exc_msg=str(e),
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.ingestors.telegram.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)

    base_url = os.environ.get("MEMEX_BASE_URL", "http://localhost:8787")
    api_token = os.environ.get("MEMEX_API_TOKEN", "") or None

    log.info("telegram.cli.start", cmd=args.cmd, base_url=base_url)

    try:
        with MemexServerClient(base_url=base_url, api_token=api_token) as client:
            if args.cmd == "run":
                return _cmd_run(args, client, log)
            if args.cmd == "auth":
                return _cmd_auth(args, client, log)
            if args.cmd == "discover":
                return _cmd_discover(args, client, log)
            if args.cmd == "listen":
                return _cmd_listen(args, client, log)
            log.error("telegram.cli.unknown_command", cmd=args.cmd)
            return 1
    except MemexAPIError as e:
        log.error(
            "telegram.cli.memex_api_unreachable",
            status_code=e.status_code,
            body=e.body,
        )
        return 1
    except Exception as e:
        log.exception(
            "telegram.cli.fatal",
            exc_type=type(e).__name__,
            exc_msg=str(e),
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
