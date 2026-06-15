"""CLI `memex-graph` — grafo de relaciones: co-ocurrencia + cúmulos + mantenimiento.

Subcomandos:
  confirm   — co-ocurrencia POR-MENSAJE (metodología B): GENERA las pistas y las juzga (recibo a
              priori + LLM con compuerta alias-aware) + resumen. Reemplaza al viejo `build`.
  cluster   — detecta los cúmulos (Louvain) y los reconcilia contra lo persistido (sin LLM).
  validate  — valida con el LLM los cúmulos pendientes (confirma/nombra/describe/poda).
  reconcile — mantenimiento del grafo (sin LLM): poda huérfanas + reconcilia las reales stale.
  cycle     — confirm → cluster → validate → reconcile, de corrido.
  list      — lista los cúmulos del user (opcional `--status`).
  help      — resumen de los comandos.

Las aristas REALES NO se arman acá: las tejen los módulos al escribir (paso 5). Todo on-demand; los
jobs del scheduler NO se encienden por esto. `confirm`/`validate`/`cycle` usan el LLM
(DEEPSEEK_API_KEY vía doppler; `confirm --dry-run/--no-llm` y `reconcile` no). Server-side: habla
con la DB vía `connection()` (igual que `memex-identidades`). Exit 0 si OK; 1 si error fatal.
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
from memex.relations.clusters_llm import run_cluster_partition
from memex.relations.maintenance import reconcile_graph
from memex.relations.reconcile import detect_and_reconcile


def _safe(text_: str) -> str:
    enc = sys.stdout.encoding or "utf-8"
    return text_.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


_HELP = """memex-graph — grafo de relaciones (co-ocurrencia + cúmulos + mantenimiento).

  confirm             GENERA y confirma co-ocurrencias por-mensaje (recibo a priori + LLM B)
  cluster             detecta cúmulos (Louvain) y reconcilia contra lo persistido (sin LLM)
  validate            valida con el LLM los cúmulos pendientes (confirma/nombra/poda)
  reconcile           mantenimiento: poda huérfanas + reconcilia reales stale (sin LLM)
  cycle               confirm → cluster → validate → reconcile, de corrido
  list [--status S]   lista los cúmulos del user
  help                muestra esta ayuda

Flags de cada comando: memex-graph <comando> -h"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-graph")
    sub = parser.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("cluster", help="Detecta y reconcilia los cúmulos (sin LLM).")
    c.add_argument("--user", type=int, default=1, help="User id (default 1).")

    v = sub.add_parser("validate", help="Valida con el LLM los cúmulos pendientes.")
    v.add_argument("--user", type=int, default=1, help="User id (default 1).")
    v.add_argument("--limit", type=int, default=None, help="Máximo de cúmulos a validar.")

    rc = sub.add_parser("reconcile", help="Mantenimiento: poda huérfanas + reconcilia reales.")
    rc.add_argument("--user", type=int, default=1, help="User id (default 1).")

    cy = sub.add_parser("cycle", help="confirm → cluster → validate → reconcile, de corrido.")
    cy.add_argument("--user", type=int, default=1, help="User id (default 1).")

    cf = sub.add_parser("confirm", help="Confirmación por-mensaje de co-ocurrencias ambiguas.")
    cf.add_argument("--user", type=int, default=1, help="User id (default 1).")
    cf.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Máximo de aristas ambiguas a considerar (default: sin tope; el universo entero).",
    )
    cf.add_argument(
        "--budget",
        "--max-llm-calls",
        type=int,
        default=None,
        dest="budget",
        help="Presupuesto de llamadas LLM (1 llamada = 1 mensaje; "
        "default: settings.per_message_max_llm_calls).",
    )
    cf.add_argument(
        "--dry-run",
        action="store_true",
        help="Clasifica sin escribir NADA y estima las llamadas LLM.",
    )
    cf.add_argument(
        "--no-llm",
        action="store_true",
        help="Aplica solo el a-priori del recibo; las demás quedan ambiguas.",
    )

    li = sub.add_parser("list", help="Lista los cúmulos del user.")
    li.add_argument("--user", type=int, default=1, help="User id (default 1).")
    li.add_argument(
        "--status",
        help="Filtra por estado (candidate/confirmed/stale/rejected/dissolved).",
    )

    sub.add_parser("help", help="Resumen de los comandos.")
    return parser


def _cmd_reconcile(args: argparse.Namespace) -> int:
    with connection() as conn:
        stats = reconcile_graph(conn, args.user)
    _say(
        f"\ngraph reconcile: stale_afiliacion={stats.stale_afiliacion} "
        f"stale_pertenencia={stats.stale_pertenencia} "
        f"stale_contraparte={stats.stale_contraparte} "
        f"huerfanas_podadas={stats.orphans_pruned}\n"
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
    stats = asyncio.run(run_cluster_partition(args.user, limit=args.limit))
    _say(
        f"\ngraph validate: blobs={stats.blobs} contextos={stats.groups} "
        f"(nuevos={stats.created} sincronizados={stats.synced}) disueltos={stats.dissolved} "
        f"ruido={stats.rejected} pistas_promovidas={stats.promoted} saltados={stats.skipped} "
        f"errores={stats.errors} llm_calls={stats.cost.calls} costo_usd={stats.cost.cost_usd}\n"
    )
    return 1 if stats.errors else 0


def _cmd_cycle(args: argparse.Namespace) -> int:
    from memex.relations.per_message import run_per_message_confirm

    cf = asyncio.run(run_per_message_confirm(args.user))
    with connection() as conn:
        r = detect_and_reconcile(conn, args.user)
    v = asyncio.run(run_cluster_partition(args.user))
    with connection() as conn:
        rc = reconcile_graph(conn, args.user)
    _say(
        f"\ngraph cycle: [confirm] aristas={cf.edges} recibo={cf.confirmed_recibo} "
        f"llm_conf={cf.llm_confirmed} llm_rej={cf.llm_rejected} gated={cf.gated} "
        f"resumenes={cf.summaries} | "
        f"[detect] detectados={r.detected} nuevos={r.new_candidates} disueltos={r.dissolved} | "
        f"[partition] contextos={v.groups} (nuevos={v.created} sync={v.synced}) "
        f"promovidas={v.promoted} ruido={v.rejected} errores={v.errors} | "
        f"[reconcile] stale={rc.stale_afiliacion + rc.stale_pertenencia + rc.stale_contraparte} "
        f"huerfanas={rc.orphans_pruned}\n"
    )
    return 1 if (v.errors or cf.errors) else 0


def _cmd_confirm(args: argparse.Namespace) -> int:
    from memex.relations.per_message import run_per_message_confirm

    stats = asyncio.run(
        run_per_message_confirm(
            args.user,
            limit=args.limit,
            budget=args.budget,
            dry_run=args.dry_run,
            no_llm=args.no_llm,
        )
    )
    mode = " (dry-run: proyección, nada se escribió)" if args.dry_run else ""
    _say(
        f"\ngraph confirm{mode}: aristas={stats.edges} saltadas_memo={stats.skipped_dejar} "
        f"recibo={stats.confirmed_recibo} | mensajes={stats.messages} "
        f"chats_saltados={stats.chat_skipped} "
        + (
            f"llamadas_estimadas={stats.estimated_calls}"
            if args.dry_run
            else f"| llm: confirmadas={stats.llm_confirmed} rechazadas={stats.llm_rejected} "
            f"dejar={stats.llm_dejar} gated={stats.gated} resumenes={stats.summaries} "
            f"presupuesto_agotado={stats.budget_exhausted} "
            f"llm_calls={stats.cost.calls} costo_usd={stats.cost.cost_usd}"
        )
        + f" | errores={stats.errors}\n"
    )
    return 1 if stats.errors else 0


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
        if args.cmd == "cluster":
            return _cmd_cluster(args)
        if args.cmd == "validate":
            return _cmd_validate(args)
        if args.cmd == "reconcile":
            return _cmd_reconcile(args)
        if args.cmd == "cycle":
            return _cmd_cycle(args)
        if args.cmd == "confirm":
            return _cmd_confirm(args)
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
