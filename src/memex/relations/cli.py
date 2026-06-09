"""CLI `memex-graph` — grafo de relaciones: armado determinista + cúmulos (detección).

Subcomandos:
  build     — paso determinista (`build_relations`): pistas de co-ocurrencia + aristas reales.
  cluster   — detecta los cúmulos (Louvain) y los reconcilia contra lo persistido (sin LLM).
  validate  — valida con el LLM los cúmulos pendientes (confirma/nombra/describe/poda).
  cycle     — build → cluster → validate, de corrido.
  list      — lista los cúmulos del user (opcional `--status`).
  help      — resumen de los comandos.

Todo on-demand; el job `graph` del scheduler NO se enciende por esto. `validate`/`cycle` usan el LLM
(DEEPSEEK_API_KEY vía doppler). Server-side: habla con la DB vía `connection()` (igual que
`memex-identidades`). Exit 0 si OK; 1 si error fatal.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import text

from memex.db import connection
from memex.llm.client import LLMError
from memex.logging import get_logger, setup_logging
from memex.relations.clusters_llm import run_cluster_validation
from memex.relations.deterministic import build_relations
from memex.relations.reconcile import detect_and_reconcile


def _safe(text_: str) -> str:
    enc = sys.stdout.encoding or "utf-8"
    return text_.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


_HELP = """memex-graph — grafo de relaciones (armado determinista + cúmulos).

  build               corre el paso determinista (pistas de co-ocurrencia + aristas reales)
  cluster             detecta cúmulos (Louvain) y reconcilia contra lo persistido (sin LLM)
  validate            valida con el LLM los cúmulos pendientes (confirma/nombra/poda)
  cycle               build → cluster → validate, de corrido
  list [--status S]   lista los cúmulos del user
  help                muestra esta ayuda

Flags de cada comando: memex-graph <comando> -h"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-graph")
    sub = parser.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="Paso determinista del grafo (build_relations).")
    b.add_argument("--user", type=int, default=1, help="User id (default 1).")

    c = sub.add_parser("cluster", help="Detecta y reconcilia los cúmulos (sin LLM).")
    c.add_argument("--user", type=int, default=1, help="User id (default 1).")

    v = sub.add_parser("validate", help="Valida con el LLM los cúmulos pendientes.")
    v.add_argument("--user", type=int, default=1, help="User id (default 1).")
    v.add_argument("--limit", type=int, default=None, help="Máximo de cúmulos a validar.")

    cy = sub.add_parser("cycle", help="build → cluster → validate, de corrido.")
    cy.add_argument("--user", type=int, default=1, help="User id (default 1).")

    li = sub.add_parser("list", help="Lista los cúmulos del user.")
    li.add_argument("--user", type=int, default=1, help="User id (default 1).")
    li.add_argument(
        "--status",
        help="Filtra por estado (candidate/confirmed/stale/rejected/dissolved).",
    )

    sub.add_parser("help", help="Resumen de los comandos.")
    return parser


def _cmd_build(args: argparse.Namespace) -> int:
    with connection() as conn:
        stats = build_relations(conn, args.user)
    _say(
        f"\ngraph build: pistas={stats.cooccurrence_pistas} afiliacion={stats.afiliacion_reales} "
        f"pertenencia={stats.pertenencia_reales} contraparte={stats.contraparte_reales} "
        f"mismo_evento={stats.same_event_reales} cumple={stats.cumple_reales} "
        f"saltados={stats.high_fanout_skipped} huerfanas_podadas={stats.orphans_pruned}\n"
    )
    return 0


def _cmd_cluster(args: argparse.Namespace) -> int:
    with connection() as conn:
        stats = detect_and_reconcile(conn, args.user)
    _say(
        f"\ngraph cluster: detectados={stats.detected} match_igual={stats.matched_same} "
        f"match_deriva={stats.matched_drift} nuevos={stats.new_candidates} "
        f"memo_saltados={stats.memo_skipped} borrados={stats.deleted} disueltos={stats.dissolved}\n"
    )
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    stats = asyncio.run(run_cluster_validation(args.user, limit=args.limit))
    _say(
        f"\ngraph validate: cumulos={stats.clusters} confirmados={stats.confirmed} "
        f"rechazados={stats.rejected} podados={stats.pruned_members} saltados={stats.skipped} "
        f"errores={stats.errors} llm_calls={stats.cost.calls} costo_usd={stats.cost.cost_usd}\n"
    )
    return 1 if stats.errors else 0


def _cmd_cycle(args: argparse.Namespace) -> int:
    with connection() as conn:
        b = build_relations(conn, args.user)
        r = detect_and_reconcile(conn, args.user)
    v = asyncio.run(run_cluster_validation(args.user))
    _say(
        f"\ngraph cycle: [build] pistas={b.cooccurrence_pistas} cluster_edges={b.cluster_edges} | "
        f"[detect] detectados={r.detected} nuevos={r.new_candidates} disueltos={r.dissolved} | "
        f"[validate] confirmados={v.confirmed} rechazados={v.rejected} podados={v.pruned_members} "
        f"errores={v.errors}\n"
    )
    return 1 if v.errors else 0


def _cmd_list(args: argparse.Namespace) -> int:
    sql = (
        "SELECT id, status, name, member_count, confidence FROM relation_clusters "
        "WHERE user_id = :u"
    )
    params: dict[str, Any] = {"u": args.user}
    if args.status:
        sql += " AND status = :st"
        params["st"] = args.status
    sql += " ORDER BY status, id"
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    if not rows:
        _say(f"\nSin cúmulos para el user {args.user}.\n")
        return 0
    _say(f"\nCúmulos (user {args.user}):")
    for r in rows:
        conf = f"{float(r['confidence']):.2f}" if r["confidence"] is not None else "-"
        name = r["name"] or "(sin nombre)"
        _say(f"  [{r['id']}] {r['status']:<9} n={r['member_count']:<3} conf={conf}  {name}")
    _say("")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.relations.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "help":
        _say(_HELP)
        return 0
    log.info("graph.cli.start", cmd=args.cmd)

    try:
        if args.cmd == "build":
            return _cmd_build(args)
        if args.cmd == "cluster":
            return _cmd_cluster(args)
        if args.cmd == "validate":
            return _cmd_validate(args)
        if args.cmd == "cycle":
            return _cmd_cycle(args)
        if args.cmd == "list":
            return _cmd_list(args)
        log.error("graph.cli.unknown_command", cmd=args.cmd)
        return 1
    except LLMError as e:
        log.error("graph.cli.llm_error", status_code=e.status_code, msg=str(e))
        _say(
            "\nERROR del LLM. ¿Configuraste DEEPSEEK_API_KEY y corriste con "
            "`doppler run -- ...`?\n",
            err=True,
        )
        return 1
    except Exception as e:
        log.exception("graph.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
