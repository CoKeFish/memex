"""CLI `memex-graph` — grafo de relaciones: armado determinista + cúmulos (detección).

Subcomandos:
  build     — paso determinista (`build_relations`): pistas de co-ocurrencia + aristas reales.
  cluster   — detecta los cúmulos (Louvain) y los reconcilia contra lo persistido (sin LLM).
  validate  — valida con el LLM los cúmulos pendientes (confirma/nombra/describe/poda).
  cycle     — build → cluster → validate, de corrido.
  resolve   — veredicto PAR-POR-PAR del long-tail de pistas (prefiltro determinista + LLM gris).
  list      — lista los cúmulos del user (opcional `--status`).
  help      — resumen de los comandos.

Todo on-demand; los jobs del scheduler NO se encienden por esto. `validate`/`cycle`/`resolve` usan
el LLM (DEEPSEEK_API_KEY vía doppler; `resolve --dry-run/--no-llm` no). Server-side: habla con la
DB vía `connection()` (igual que `memex-identidades`). Exit 0 si OK; 1 si error fatal.
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
  resolve             veredicto par-por-par del long-tail de pistas (recibo/bulk + LLM gris)
  list [--status S]   lista los cúmulos del user
  help                muestra esta ayuda

Flags de cada comando: memex-graph <comando> -h"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-graph")
    sub = parser.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="Paso determinista del grafo (build_relations).")
    b.add_argument("--user", type=int, default=1, help="User id (default 1).")
    b.add_argument(
        "--cooccurrence-cap",
        type=int,
        default=None,
        help="Tope de vértices por mensaje para la co-ocurrencia "
        "(default: settings.cooccurrence_cap).",
    )

    c = sub.add_parser("cluster", help="Detecta y reconcilia los cúmulos (sin LLM).")
    c.add_argument("--user", type=int, default=1, help="User id (default 1).")

    v = sub.add_parser("validate", help="Valida con el LLM los cúmulos pendientes.")
    v.add_argument("--user", type=int, default=1, help="User id (default 1).")
    v.add_argument("--limit", type=int, default=None, help="Máximo de cúmulos a validar.")

    cy = sub.add_parser("cycle", help="build → cluster → validate, de corrido.")
    cy.add_argument("--user", type=int, default=1, help="User id (default 1).")
    cy.add_argument(
        "--cooccurrence-cap",
        type=int,
        default=None,
        help="Tope de vértices por mensaje para la co-ocurrencia "
        "(default: settings.cooccurrence_cap).",
    )

    rs = sub.add_parser("resolve", help="Veredicto par-por-par del long-tail de pistas.")
    rs.add_argument("--user", type=int, default=1, help="User id (default 1).")
    rs.add_argument(
        "--cluster", type=int, default=None, help="Solo las pistas internas a este cúmulo."
    )
    rs.add_argument(
        "--vertex",
        default=None,
        help="Solo la componente de pistas que contiene a este vértice (slug:id).",
    )
    rs.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Máximo de grupos (componentes) por corrida en modo auto "
        "(default: settings.resolve_group_limit).",
    )
    rs.add_argument(
        "--max-llm-calls",
        type=int,
        default=None,
        help="Presupuesto de llamadas LLM (1 llamada = 1 mensaje; "
        "default: settings.resolve_max_llm_calls).",
    )
    rs.add_argument(
        "--dry-run",
        action="store_true",
        help="Clasifica sin escribir NADA y estima las llamadas LLM.",
    )
    rs.add_argument(
        "--no-llm",
        action="store_true",
        help="Aplica solo el prefiltro determinista; la zona gris queda pendiente.",
    )

    li = sub.add_parser("list", help="Lista los cúmulos del user.")
    li.add_argument("--user", type=int, default=1, help="User id (default 1).")
    li.add_argument(
        "--status",
        help="Filtra por estado (candidate/confirmed/stale/rejected/dissolved).",
    )

    sub.add_parser("help", help="Resumen de los comandos.")
    return parser


def _cooccurrence_cap(args: argparse.Namespace) -> int:
    """El cap del flag o, sin él, el de settings — la MISMA fuente que el API y el scheduler."""
    if args.cooccurrence_cap is not None:
        return int(args.cooccurrence_cap)
    from memex.config import settings  # import local: estilo del módulo

    return settings.cooccurrence_cap


def _cmd_build(args: argparse.Namespace) -> int:
    with connection() as conn:
        stats = build_relations(conn, args.user, cooccurrence_cap=_cooccurrence_cap(args))
    _say(
        f"\ngraph build: pistas={stats.cooccurrence_pistas} afiliacion={stats.afiliacion_reales} "
        f"pertenencia={stats.pertenencia_reales} contraparte={stats.contraparte_reales} "
        f"mismo_evento={stats.same_event_reales} cumple={stats.cumple_reales} "
        f"participa={stats.participa_reales} canales={stats.canales} "
        f"remitentes_chat={stats.chat_senders} "
        f"saltados={stats.high_fanout_skipped} huerfanas_podadas={stats.orphans_pruned} "
        f"redundantes_confirmadas={stats.redundant_resolved}\n"
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
    with connection() as conn:
        b = build_relations(conn, args.user, cooccurrence_cap=_cooccurrence_cap(args))
        r = detect_and_reconcile(conn, args.user)
    v = asyncio.run(run_cluster_partition(args.user))
    _say(
        f"\ngraph cycle: [build] pistas={b.cooccurrence_pistas} cluster_edges={b.cluster_edges} | "
        f"[detect] detectados={r.detected} nuevos={r.new_candidates} disueltos={r.dissolved} | "
        f"[partition] contextos={v.groups} (nuevos={v.created} sync={v.synced}) "
        f"promovidas={v.promoted} ruido={v.rejected} errores={v.errors}\n"
    )
    return 1 if v.errors else 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    from memex.relations.resolve import _parse_vertex, run_resolve

    vertex = _parse_vertex(args.vertex) if args.vertex else None
    stats = asyncio.run(
        run_resolve(
            args.user,
            cluster_id=args.cluster,
            vertex=vertex,
            limit=args.limit,
            max_llm_calls=args.max_llm_calls,
            dry_run=args.dry_run,
            no_llm=args.no_llm,
        )
    )
    mode = " (dry-run: proyección, nada se escribió)" if args.dry_run else ""
    _say(
        f"\ngraph resolve{mode}: grupos={stats.groups} pares={stats.pairs} "
        f"saltados_memo={stats.skipped_dejar} | regla: recibo={stats.confirmed_recibo} "
        f"bulk={stats.rejected_bulk} sin_evidencia={stats.sin_evidencia} | "
        f"gris: pares={stats.gray_pairs} mensajes={stats.gray_messages} "
        + (
            f"llamadas_estimadas={stats.estimated_calls}"
            if args.dry_run
            else f"confirmadas={stats.llm_confirmed} rechazadas={stats.llm_rejected} "
            f"dejar={stats.llm_dejar} sin_cita={stats.ungrounded} "
            f"presupuesto_agotado={stats.budget_exhausted} "
            f"llm_calls={stats.cost.calls} costo_usd={stats.cost.cost_usd}"
        )
        + f" | errores={stats.errors}\n"
    )
    if stats.stale_recibo_conflicts:
        _say(
            f"AVISO: {stats.stale_recibo_conflicts} pista(s) RECHAZADAS ganaron evidencia de "
            "RECIBO después del veredicto (ver logs relation.resolve.stale_recibo_conflict); "
            "la monotonía no las reabre — revisalas a mano.",
            err=True,
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
        if args.cmd == "build":
            return _cmd_build(args)
        if args.cmd == "cluster":
            return _cmd_cluster(args)
        if args.cmd == "validate":
            return _cmd_validate(args)
        if args.cmd == "cycle":
            return _cmd_cycle(args)
        if args.cmd == "resolve":
            return _cmd_resolve(args)
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
