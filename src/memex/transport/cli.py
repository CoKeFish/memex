"""CLI `memex-transport` — consulta on-demand de "¿llego a tiempo a mi próximo evento?".

Subcomando:
  next-arrival — evalúa el próximo evento con lugar y reporta el veredicto (sin emitir avisos).

Server-side: usa el proveedor de mapas (key por env var, Doppler → `doppler run -- memex-transport
...`) y la DB de memex. Exit 0 si OK; 1 si error del proveedor/config; 2 si argumentos inválidos.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import datetime

from dotenv import load_dotenv

from memex.geo.client import GeoConfigError, GeoError
from memex.geo.providers import build_provider_from_env
from memex.logging import get_logger, setup_logging
from memex.transport.config import TransportConfig
from memex.transport.service import NextArrivalResult, assess_next_arrival


def _say(msg: str, *, err: bool = False) -> None:
    enc = sys.stdout.encoding or "utf-8"
    safe = msg.encode(enc, errors="replace").decode(enc, errors="replace")
    print(safe, file=sys.stderr if err else sys.stdout)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-transport")
    sub = parser.add_subparsers(dest="cmd", required=True)
    na = sub.add_parser("next-arrival", help="¿Llego a mi próximo evento? (consulta, no emite).")
    na.add_argument("--user", type=int, default=1, help="User id (default 1).")
    na.add_argument("--json", action="store_true", help="Salida JSON (última línea).")
    return parser


def _print_result(result: NextArrivalResult | None, *, as_json: bool) -> None:
    if result is None:
        _say(json.dumps({"upcoming": False}) if as_json else "No hay próximo evento con lugar.")
        return
    a = result.assessment
    if as_json:
        _say(
            json.dumps(
                {
                    "upcoming": True,
                    "event_id": result.event_id,
                    "title": result.title,
                    "event_start": result.event_start.isoformat(),
                    "verdict": a.verdict.value,
                    "leave_by": a.leave_by.isoformat() if a.leave_by else None,
                    "travel_seconds": a.travel_seconds,
                    "reason": result.reason,
                }
            )
        )
        return
    suffix = f" ({result.reason})" if result.reason else ""
    _say(f"Próximo: «{result.title}» a las {result.event_start:%Y-%m-%d %H:%M}")
    _say(f"Veredicto: {a.verdict.value}{suffix}")
    if a.leave_by is not None:
        _say(f"Salí a más tardar: {a.leave_by:%H:%M}")
    if a.travel_seconds is not None:
        _say(f"Viaje estimado: {a.travel_seconds // 60} min")


async def _cmd_next_arrival(args: argparse.Namespace) -> int:
    cfg = TransportConfig.from_env()
    now = datetime.now(cfg.tz)
    provider = build_provider_from_env()
    try:
        result = await assess_next_arrival(user_id=args.user, provider=provider, cfg=cfg, now=now)
    finally:
        await provider.aclose()
    _print_result(result, as_json=args.json)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.transport.cli")
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "next-arrival":
            return asyncio.run(_cmd_next_arrival(args))
    except GeoConfigError as e:
        _say(f"Config inválida: {e}. ¿Corriste con `doppler run -- memex-transport ...`?", err=True)
        return 1
    except GeoError as e:
        _say(f"Error del proveedor de mapas: {e}", err=True)
        log.warning("transport.cli.error", error=str(e))
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
