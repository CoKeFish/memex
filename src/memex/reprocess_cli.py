"""CLI `memex-reprocess` — re-aplica etapas del pipeline a inbox YA ingeridos.

Etapas (`--stage`, separadas por coma, o `all`): media (re-baja adjuntos por IMAP) · ocr ·
classify · summarize · extract. Se corren en orden de dependencia (`STAGE_ORDER`).

Objetivo: `--inbox` (repetible) para mensajes puntuales, o un filtro de lote
(`--source` / `--since` / `--until` / `--only` / `--limit`). `--force` reprocesa lo ya hecho
(re-OCR, re-clasificar, re-resumir/extraer). `--dry-run` solo lista los objetivos.

Server-side + async. Las etapas LLM/OCR necesitan las llaves del proveedor (DEEPSEEK_API_KEY /
OPENAI_API_KEY) y `media` necesita las credenciales IMAP de la fuente — todo inyectado por
`doppler run`. Exit 0 si OK; 1 si error fatal de argumentos/config.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

from dotenv import load_dotenv

from memex.logging import get_logger, setup_logging
from memex.reprocess import ONLY_FILTERS, STAGE_ORDER, reprocess, select_targets


def _parse_stages(raw: str) -> list[str]:
    if raw.strip() == "all":
        return list(STAGE_ORDER)
    stages = [s.strip() for s in raw.split(",") if s.strip()]
    invalid = [s for s in stages if s not in STAGE_ORDER]
    if invalid:
        raise argparse.ArgumentTypeError(
            f"stage(s) inválida(s): {invalid}; válidas: {list(STAGE_ORDER)} (o 'all')"
        )
    return stages


def _parse_date(raw: str) -> datetime:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"fecha inválida {raw!r}, usá YYYY-MM-DD") from e


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memex-reprocess")
    p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    p.add_argument(
        "--stage",
        type=_parse_stages,
        required=True,
        help=f"Etapas separadas por coma, o 'all'. Válidas: {','.join(STAGE_ORDER)}.",
    )
    p.add_argument("--inbox", type=int, action="append", default=None, help="inbox id (repetible).")
    p.add_argument("--source", type=int, default=None, help="Limitar a este source id.")
    p.add_argument("--since", type=_parse_date, default=None, help="Desde (YYYY-MM-DD, inclusive).")
    p.add_argument("--until", type=_parse_date, default=None, help="Hasta (YYYY-MM-DD, exclusivo).")
    p.add_argument(
        "--only", choices=sorted(ONLY_FILTERS), default=None, help="Filtro de selección por lote."
    )
    p.add_argument("--limit", type=int, default=None, help="Tope de mensajes seleccionados.")
    p.add_argument("--force", action="store_true", help="Reprocesar lo ya hecho (invalida cursor).")
    p.add_argument("--dry-run", action="store_true", help="Solo listar los objetivos, sin correr.")
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.reprocess.cli")
    args = _build_parser().parse_args(argv)

    targets = (
        list(args.inbox)
        if args.inbox
        else select_targets(
            args.user,
            source_id=args.source,
            since=args.since,
            until=args.until,
            limit=args.limit,
            only=args.only,
        )
    )
    log.info("reprocess.cli.start", user=args.user, stages=args.stage, targets=len(targets))

    if args.dry_run:
        print(f"\ndry-run: {len(targets)} objetivo(s), etapas={args.stage}")
        print(f"inbox_ids: {targets}\n")
        return 0
    if not targets:
        print("\nsin objetivos para el filtro dado.\n")
        return 0

    try:
        results = asyncio.run(
            reprocess(args.user, stages=args.stage, targets=targets, force=args.force)
        )
    except Exception as e:  # error de args/validación; las etapas internas son best-effort
        log.exception("reprocess.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        print(f"\nERROR: {e}\n", file=sys.stderr)
        return 1

    print("\n" + json.dumps(results, indent=2, ensure_ascii=False, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
