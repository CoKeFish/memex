"""CLI `memex-quality` — sistema de calidad/relevancia contra la DB de memex (sin HTTP).

Subcomandos:
  detect          — corre la detección de candidatos (procedimientos deterministas, sin LLM).
  candidates      — lista los candidatos (filtro opcional por estado).

Conecta directo a Postgres (`memex.db.connection`); pensado para el server (mismo host que la DB).

Ejemplos:
  memex-quality detect --user-id 1
  memex-quality candidates --user-id 1 --status open
"""

from __future__ import annotations

import argparse

from memex.db import connection
from memex.logging import setup_logging
from memex.quality.candidates import list_candidates, run_relevance_detection


def cmd_detect(args: argparse.Namespace) -> int:
    stats = run_relevance_detection(args.user_id)
    print(f"detección: {stats.procedures} procedimientos, {stats.candidates} candidatos")
    return 0


def cmd_candidates(args: argparse.Namespace) -> int:
    status = None if args.status == "all" else args.status
    with connection() as conn:
        rows = list_candidates(conn, user_id=args.user_id, status=status)
    if not rows:
        print("(sin candidatos)")
        return 0
    for r in rows:
        pct = r["relevance_pct"]
        pct_s = f"{pct}%" if pct is not None else "—"
        print(
            f"[{r['status']}] {r['sender_label']} <{r['email'] or '—'}> "
            f"msgs={r['messages']} rel={pct_s} inertes={r['inert']} score={r['score']}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memex-quality")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_detect = sub.add_parser("detect", help="corre la detección de candidatos (sin LLM)")
    p_detect.add_argument("--user-id", type=int, default=1)
    p_detect.set_defaults(func=cmd_detect)

    p_cand = sub.add_parser("candidates", help="lista los candidatos a filtrar")
    p_cand.add_argument("--user-id", type=int, default=1)
    p_cand.add_argument(
        "--status", default="open", choices=["open", "confirmed", "dismissed", "all"]
    )
    p_cand.set_defaults(func=cmd_candidates)

    return p


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
