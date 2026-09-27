"""CLI `memex-social` — run, health, discover (Instagram / Facebook / X vía Apify).

Subcomandos:

  run       — ejecuta UNA pasada de polling para los sources sociales registrados
              en memex (filtrable por --source-id y --type). Mismo patrón que
              `memex-telegram run`.
  health    — valida el token de Apify de un source (GET /v2/users/me) sin
              scrapear ni gastar. Imprime healthy/unhealthy.
  discover  — dry-run: corre el actor con un resultsLimit chico e imprime los
              posts parseados a stdout. NO persiste nada. Loguea el costo Apify
              para calibrar antes de habilitar el run real.

Reads `MEMEX_BASE_URL` y `MEMEX_API_TOKEN` del entorno (con `.env` auto-load), y
`MEMEX_APIFY_TOKEN` (lo resuelve la config del source).

Exit code 0 si todo OK; 1 si hubo errores fatales (config inválida, API memex
unreachable). Errores por record/cuenta se cuentan en stats, no son fatales.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from dotenv import load_dotenv

from memex.core.cursors import SocialCursor
from memex.core.observability import ingestion_run, record_apify_runs
from memex.core.sink import MemexSink
from memex.core.source import ActorRunReporting, Source, SourceConfigError
from memex.ingestors.memex_server_client import MemexAPIError, MemexServerClient
from memex.ingestors.runner import run_ingestor
from memex.logging import get_logger, setup_logging
from memex.sources import resolve

SOCIAL_TYPES = ("instagram", "facebook", "x")


def _safe(text: str) -> str:
    """Sanea un string para el encoding de la consola actual (cp1252 en Windows)."""
    enc = sys.stdout.encoding or "utf-8"
    return text.encode(enc, errors="replace").decode(enc, errors="replace")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-social")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run a single polling pass for social sources.")
    run_p.add_argument(
        "--source-id",
        type=int,
        default=None,
        help="Only run for this source id; default = all enabled social sources.",
    )
    run_p.add_argument(
        "--type",
        choices=SOCIAL_TYPES,
        default=None,
        help="Restrict to one platform (instagram/facebook/x).",
    )
    run_p.add_argument("--chunk-size", type=int, default=20, help="Records per chunk (default 20).")
    run_p.add_argument(
        "--chunk-sleep-ms", type=int, default=100, help="Sleep between chunks in ms (default 100)."
    )
    # Ventana (simetría con POST /sources/{id}/fetch): range/last = backfill, NO avanzan el cursor.
    run_p.add_argument(
        "--mode",
        choices=["incremental", "range", "last"],
        default="incremental",
        help="incremental (default) | range (since..until, backfill) | last (últimos N).",
    )
    run_p.add_argument("--since", default=None, help="range: YYYY-MM-DD inclusivo.")
    run_p.add_argument("--until", default=None, help="range: YYYY-MM-DD exclusivo.")
    run_p.add_argument("--limit", type=int, default=None, help="last/range: tope de posts/cuenta.")

    health_p = sub.add_parser("health", help="Validate the Apify token — no scrape, no spend.")
    health_p.add_argument("--source-id", type=int, required=True, help="Source id to health-check.")

    disc_p = sub.add_parser(
        "discover",
        help="Dry-run: scrape a few posts and print them — no persistence.",
    )
    disc_p.add_argument("--source-id", type=int, required=True, help="Source id to preview.")
    disc_p.add_argument(
        "--limit", type=int, default=3, help="Posts to scrape per account (default 3)."
    )

    acc_p = sub.add_parser(
        "accounts",
        help="Manage the followed-accounts allowlist of a social source (list/add/remove).",
    )
    acc_p.add_argument("op", choices=["list", "add", "remove"], help="Operation.")
    acc_p.add_argument("--source-id", type=int, required=True, help="Social source id.")
    acc_p.add_argument("--handle", default=None, help="Handle/URL (required for add/remove).")
    acc_p.add_argument("--priority", action="store_true", help="Mark as priority (add only).")

    return parser


def _select_sources(
    client: MemexServerClient,
    source_id: int | None,
    type_filter: str | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stype in SOCIAL_TYPES:
        if type_filter is not None and stype != type_filter:
            continue
        rows.extend(client.get_sources_by_type(stype))
    if source_id is not None:
        rows = [s for s in rows if int(s.get("id", 0)) == source_id]
    return rows


def _build_source(src: dict[str, Any], cfg_override: dict[str, Any] | None = None) -> Source[Any]:
    """Resuelve la factory por `source.type` (vía registry) y construye la source.

    No importa las clases concretas — usa `memex.sources.resolve` (path disciplinado).
    """
    cfg_dict = dict(src.get("config") or {})
    if cfg_override:
        cfg_dict.update(cfg_override)
    factory = resolve(str(src["type"]))
    return factory(cfg_dict)


def _window_override(args: argparse.Namespace) -> dict[str, Any]:
    """Claves transitorias de ventana para el config (mismas que inyecta el fetch a demanda)."""
    if args.mode == "incremental":
        return {}
    override: dict[str, Any] = {"fetch_mode": args.mode}
    if args.since:
        override["fetch_since"] = args.since
    if args.until:
        override["fetch_until"] = args.until
    if args.limit is not None:
        override["fetch_limit"] = args.limit
    return override


class _NoCheckpointSink:
    """Sink que delega todo MENOS la persistencia del checkpoint (para range/last).

    El sink del CLI (`MemexServerClient`) siempre persiste; sin este wrapper, un backfill de
    posts viejos RETROCEDERÍA el cursor por-cuenta (advance_checkpoint setea last_posted_at
    del record) y el próximo incremental re-pagaría todo el camino desde ahí. Espejo del
    `persist_checkpoint=False` del camino API (InProcessSink).
    """

    def __init__(self, inner: MemexServerClient) -> None:
        self._inner = inner

    def get_sources_by_type(self, source_type: str) -> list[dict[str, Any]]:
        return self._inner.get_sources_by_type(source_type)

    def get_checkpoint(self, source_id: int) -> dict[str, Any] | None:
        return self._inner.get_checkpoint(source_id)

    def put_checkpoint(self, source_id: int, cursor: dict[str, Any]) -> None:
        return None

    def post_ingest_batch(self, records: list[dict[str, Any]]) -> dict[str, int]:
        return self._inner.post_ingest_batch(records)


def _drain_reports(
    source: Source[Any], *, uid: int, sid: int, run_id: str | None, log: Any
) -> None:
    """Persiste el costo de los runs de actor (apify_runs) aunque la corrida haya fallado.

    La plata ya se gastó cuando el fetch corrió — esto va en un `finally`. Nunca tumba
    el CLI: si la DB no está accesible (p. ej. `discover` sin entorno de DB), se loggea
    y se sigue.
    """
    if not isinstance(source, ActorRunReporting):
        return
    reports = source.pop_run_reports()
    if not reports:
        return
    try:
        record_apify_runs(user_id=uid, source_id=sid, ingestion_run_id=run_id, reports=reports)
    except Exception as e:
        log.error(
            "social.cli.apify_runs.persist_failed",
            source_id=sid,
            exc_type=type(e).__name__,
            exc_msg=str(e),
        )


def _cmd_run(args: argparse.Namespace, client: MemexServerClient, log: Any) -> int:
    if args.mode == "range" and not args.since:
        log.error("social.cli.range_requires_since")
        return 1
    sources = _select_sources(client, args.source_id, args.type)
    if not sources:
        if args.source_id is not None:
            log.error("social.cli.source.not_found", source_id=args.source_id)
            return 1
        log.info("social.cli.no_sources_found")
        return 0

    override = _window_override(args)
    # range/last = backfill: NO debe persistir el cursor (ver _NoCheckpointSink).
    sink: MemexSink = client if args.mode == "incremental" else _NoCheckpointSink(client)

    had_fatal = False
    for src in sources:
        sid = int(src["id"])
        uid = int(src["user_id"])
        name = str(src.get("name", "unknown"))

        with ingestion_run(user_id=uid, source_id=sid, trigger="cli") as run:
            try:
                source = _build_source(src, cfg_override=override or None)
            except SourceConfigError as e:
                log.error("social.cli.source.config_invalid", source_name=name, reason=str(e))
                run.fail(e)
                had_fatal = True
                continue

            try:
                stats = run_ingestor(
                    source,
                    source_id=sid,
                    sink=sink,
                    chunk_size=args.chunk_size,
                    chunk_sleep_ms=args.chunk_sleep_ms,
                )
                run.finalize(stats)
            except Exception as e:
                run.fail(e)
                had_fatal = True
            finally:
                _drain_reports(source, uid=uid, sid=sid, run_id=run.id, log=log)

    return 1 if had_fatal else 0


def _cmd_health(args: argparse.Namespace, client: MemexServerClient, log: Any) -> int:
    sources = _select_sources(client, args.source_id, None)
    if not sources:
        log.error("social.cli.source.not_found", source_id=args.source_id)
        return 1
    try:
        source = _build_source(sources[0])
    except SourceConfigError as e:
        log.error("social.cli.source.config_invalid", source_id=args.source_id, reason=str(e))
        return 1

    result = asyncio.run(source.health_check())
    print(f"\n{result.status.upper()}: {_safe(result.detail)}\n")
    return 0 if result.status != "unhealthy" else 1


def _cmd_discover(args: argparse.Namespace, client: MemexServerClient, log: Any) -> int:
    sources = _select_sources(client, args.source_id, None)
    if not sources:
        log.error("social.cli.source.not_found", source_id=args.source_id)
        return 1
    try:
        source = _build_source(sources[0], cfg_override={"results_limit": args.limit})
    except SourceConfigError as e:
        log.error("social.cli.source.config_invalid", source_id=args.source_id, reason=str(e))
        return 1

    try:
        count = 0
        print(f"\n{'posted_at':<26s}  external_id / text")
        print("-" * 80)
        for rec in source.fetch(SocialCursor()):
            posted_at = str(rec.payload.get("posted_at", "?"))
            text = str(rec.payload.get("text", ""))[:80]
            print(f"{posted_at:<26s}  [{rec.external_id}] {_safe(text)}")
            count += 1
        print(f"\n{count} post(s) — dry-run, nada posteado a memex.")
        return 0
    except Exception as e:
        log.error(
            "social.cli.discover.failed",
            source_id=args.source_id,
            exc_type=type(e).__name__,
            exc_msg=str(e),
        )
        return 1
    finally:
        # discover gasta Apify real aunque no persista posts: dejar rastro del costo
        # (ingestion_run_id=None — no hay corrida de ingesta).
        _drain_reports(
            source,
            uid=int(sources[0]["user_id"]),
            sid=args.source_id,
            run_id=None,
            log=log,
        )


def _print_accounts(row: dict[str, Any]) -> None:
    accounts = (row.get("config") or {}).get("accounts") or []
    handles = [str(a.get("account", "?")) for a in accounts if isinstance(a, dict)]
    label = ", ".join(_safe(h) for h in handles) if handles else "(vacío)"
    print(f"\n{len(handles)} cuenta(s) seguida(s): {label}\n")


def _cmd_accounts(args: argparse.Namespace, client: MemexServerClient, log: Any) -> int:
    if args.op == "list":
        sources = _select_sources(client, args.source_id, None)
        if not sources:
            log.error("social.cli.source.not_found", source_id=args.source_id)
            return 1
        _print_accounts(sources[0])
        return 0

    if not args.handle:
        log.error("social.cli.accounts.handle_required", op=args.op)
        return 1
    if args.op == "add":
        row = client.add_social_account(args.source_id, args.handle, priority=args.priority)
        log.info("social.cli.accounts.added", source_id=args.source_id, handle=args.handle)
    else:  # remove
        row = client.remove_social_account(args.source_id, args.handle)
        log.info("social.cli.accounts.removed", source_id=args.source_id, handle=args.handle)
    _print_accounts(row)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.ingestors.social.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)

    base_url = os.environ.get("MEMEX_BASE_URL", "http://localhost:8787")
    api_token = os.environ.get("MEMEX_API_TOKEN", "") or None

    log.info("social.cli.start", cmd=args.cmd, base_url=base_url)

    try:
        with MemexServerClient(base_url=base_url, api_token=api_token) as client:
            if args.cmd == "run":
                return _cmd_run(args, client, log)
            if args.cmd == "health":
                return _cmd_health(args, client, log)
            if args.cmd == "discover":
                return _cmd_discover(args, client, log)
            if args.cmd == "accounts":
                return _cmd_accounts(args, client, log)
            log.error("social.cli.unknown_command", cmd=args.cmd)
            return 1
    except MemexAPIError as e:
        log.error("social.cli.memex_api_unreachable", status_code=e.status_code, body=e.body)
        return 1
    except Exception as e:
        log.exception("social.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
