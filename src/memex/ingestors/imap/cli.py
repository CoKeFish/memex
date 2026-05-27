"""Entry point: `python -m memex.ingestors.imap.cli run [--source-id N]`.

Subcommands:
- `run`: lists enabled IMAP sources from memex and drives each through the
  generic runner. Default for ongoing ingestion.
- `authorize`: one-time OAuth2 setup for a source with auth='oauth2'. Opens
  the browser, captures consent, persists tokens to disk.

Reads MEMEX_BASE_URL and MEMEX_API_TOKEN from the environment (with .env
auto-loaded).

Exit code 0 if all sources ran without fatal errors; 1 otherwise. Per-record
errors are counted in stats, not fatal.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from dotenv import load_dotenv

from memex.ingestors.http_client import MemexAPIError, MemexClient
from memex.ingestors.imap.config import ImapConfig, ImapConfigError
from memex.ingestors.imap.oauth import OAuthError, authorize_interactive
from memex.ingestors.imap.source import ImapSource
from memex.ingestors.runner import run_ingestor
from memex.logging import get_logger, setup_logging


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex.ingestors.imap")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run a single ingestion cycle.")
    run_p.add_argument(
        "--source-id",
        type=int,
        default=None,
        help="Only run for this source id; default = all enabled IMAP sources.",
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

    auth_p = sub.add_parser(
        "authorize",
        help="Run interactive OAuth2 flow for a source (one-time setup).",
    )
    auth_p.add_argument(
        "--source-id",
        type=int,
        required=True,
        help="Source id to authorize (must be configured with auth='oauth2').",
    )

    return parser


def _select_sources(client: MemexClient, source_id: int | None) -> list[dict[str, Any]]:
    all_imap = client.get_sources_by_type("imap")
    if source_id is None:
        return all_imap
    return [s for s in all_imap if int(s.get("id", 0)) == source_id]


def _cmd_run(args: argparse.Namespace, client: MemexClient, log: Any) -> int:
    sources = _select_sources(client, args.source_id)
    if not sources:
        if args.source_id is not None:
            log.error("source_not_found_or_not_imap", source_id=args.source_id)
            return 1
        log.info("no_imap_sources_found")
        return 0

    had_fatal = False
    for src in sources:
        sid = int(src["id"])
        name = str(src.get("name", "unknown"))
        cfg_dict = src.get("config", {}) or {}
        src_log = log.bind(source_id=sid, source_name=name)
        src_log.info("ingestor_run_start")

        try:
            cfg = ImapConfig.from_source_config(cfg_dict)
        except ImapConfigError as e:
            src_log.error("imap_config_invalid", reason=str(e))
            had_fatal = True
            continue

        try:
            source = ImapSource(cfg)
            stats = run_ingestor(
                source,
                source_id=sid,
                client=client,
                chunk_size=args.chunk_size,
                chunk_sleep_ms=args.chunk_sleep_ms,
            )
            src_log.info(
                "ingestor_run_end",
                posted=stats.posted,
                inserted=stats.inserted,
                duplicates=stats.duplicates,
                errors=stats.errors,
                ms_elapsed=stats.ms_elapsed,
            )
        except Exception as e:
            src_log.exception(
                "ingestor_run_fatal",
                exc_type=type(e).__name__,
                exc_msg=str(e),
            )
            had_fatal = True

    return 1 if had_fatal else 0


def _cmd_authorize(args: argparse.Namespace, client: MemexClient, log: Any) -> int:
    sources = _select_sources(client, args.source_id)
    if not sources:
        log.error("source_not_found_or_not_imap", source_id=args.source_id)
        return 1

    src = sources[0]
    cfg_dict = src.get("config", {}) or {}
    if cfg_dict.get("auth") != "oauth2":
        log.error(
            "source_not_configured_for_oauth",
            source_id=args.source_id,
            hint="set 'auth': 'oauth2' in sources.config",
        )
        return 1

    cs_env = cfg_dict.get("oauth_client_secret_path_env")
    token_env = cfg_dict.get("oauth_token_path_env")
    if not cs_env or not token_env:
        log.error(
            "oauth_paths_missing",
            hint=(
                "sources.config must include 'oauth_client_secret_path_env' "
                "and 'oauth_token_path_env'"
            ),
        )
        return 1

    cs_path = os.environ.get(str(cs_env))
    token_path = os.environ.get(str(token_env))
    if not cs_path:
        log.error("env_var_missing", var=cs_env)
        return 1
    if not token_path:
        log.error("env_var_missing", var=token_env)
        return 1

    print("\nIniciando flujo OAuth2 — se abrirá el navegador para consentimiento.")
    print(f"  client_secret: {cs_path}")
    print(f"  destino token: {token_path}\n")

    try:
        authorize_interactive(cs_path, token_path)
    except OAuthError as e:
        log.error("oauth_authorize_failed", reason=str(e))
        return 1

    log.info("oauth_authorized", source_id=args.source_id, token_path=token_path)
    print(f"\nTokens guardados en {token_path}.")
    print("El refresh_token se renueva automáticamente en cada corrida del CLI.")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.ingestors.imap.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)

    base_url = os.environ.get("MEMEX_BASE_URL", "http://localhost:8787")
    api_token = os.environ.get("MEMEX_API_TOKEN", "") or None

    log.info("cli_start", cmd=args.cmd, base_url=base_url)

    try:
        with MemexClient(base_url=base_url, api_token=api_token) as client:
            if args.cmd == "run":
                return _cmd_run(args, client, log)
            if args.cmd == "authorize":
                return _cmd_authorize(args, client, log)
            log.error("unknown_command", cmd=args.cmd)
            return 1
    except MemexAPIError as e:
        log.error(
            "memex_api_unreachable",
            status_code=e.status_code,
            body=e.body,
        )
        return 1
    except Exception as e:
        log.exception("cli_fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
