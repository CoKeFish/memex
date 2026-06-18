"""CLI `memex-notifications` — housekeeping de la cola de notificaciones.

Subcomando:
  purge — borra físicamente los avisos vencidos (`expires_at <= now`). La lectura ya los oculta;
          esto solo recupera espacio.

Server-side: usa la DB de memex del .env (`doppler run -- memex-notifications purge`).
Exit 0 si OK; 2 si argumentos inválidos.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from dotenv import load_dotenv

from memex.db import connection
from memex.logging import get_logger, setup_logging
from memex.notifications import store


def _say(msg: str, *, err: bool = False) -> None:
    enc = sys.stdout.encoding or "utf-8"
    safe = msg.encode(enc, errors="replace").decode(enc, errors="replace")
    print(safe, file=sys.stderr if err else sys.stdout)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-notifications")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("purge", help="Borra los avisos vencidos (housekeeping).")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.notifications.cli")
    args = _build_parser().parse_args(argv)
    if args.cmd == "purge":
        with connection() as conn:
            deleted = store.purge_expired(conn)
        log.info("notifications.cli.purged", deleted=deleted)
        _say(f"Purgados {deleted} avisos vencidos.")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
