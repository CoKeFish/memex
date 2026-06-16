"""CLI `memex-relevance` — gate de relevancia por intereses personales (correos).

Subcomandos:
  run        — corre el gate sobre los correos pendientes (LLM Anthropic/Opus; PAGA).
  mine       — minería de reglas sobre los no-relevantes (LLM; dry run + auto-activación).
  settings   — show/set de los settings del gate (enabled, mode, model).
  interests  — CRUD de intereses personales.
  rules      — listar/activar/desactivar reglas; alta manual (corre dry run).
  review     — cola de revisión manual (insufficient) + resolver.
  detect     — corre los procedimientos que arman candidatos a (re)evaluar (sin LLM).
  candidates — lista los candidatos detectados (filtro por estado).

Conecta directo a Postgres (`memex.db.connection`). El gate está APAGADO por default:
`memex-relevance settings set --enabled true` lo enciende.

Ejemplos:
  memex-relevance settings set --enabled true --mode per_window
  memex-relevance interests add "descuentos de Steam"
  memex-relevance run --user-id 1 --limit 100
  memex-relevance review list
  memex-relevance review resolve 123 --relevant --reason "es del banco"
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.exc import IntegrityError

from memex.db import connection
from memex.llm import LLMClient, LLMError, LLMQuotaError, build_provider_client
from memex.logging import setup_logging
from memex.relevance.candidates import list_candidates, run_relevance_detection
from memex.relevance.gate import run_relevance_gate
from memex.relevance.interests import (
    create_interest,
    delete_interest,
    list_interests,
    update_interest,
)
from memex.relevance.rules import (
    RULE_KINDS,
    create_rule,
    dry_run_rule,
    list_rules,
    set_rule_status,
)
from memex.relevance.settings import (
    GATE_MODES,
    GATE_PROVIDERS,
    GateSettings,
    get_settings,
    upsert_settings,
)
from memex.relevance.verdicts import list_review_queue, resolve_insufficient


def _quota_msg(e: LLMQuotaError) -> str:
    return f"saldo del proveedor LLM agotado ({e}); recargar antes de reintentar"


def _build_client(args: argparse.Namespace) -> LLMClient | None:
    """None = lo decide `settings.provider` en el worker. El flag --provider es un OVERRIDE
    por corrida (anthropic/codex/deepseek). codex: sin metricas de tokens (llm_calls a costo 0)
    y solo host-side; deepseek: barato, necesita DEEPSEEK_API_KEY."""
    if args.provider is None:
        return None
    if args.provider == "codex":
        print("(proveedor codex: costo no medido en llm_calls, consume tu suscripcion)")
    return build_provider_client(args.provider, codex_model=args.codex_model)


def cmd_run(args: argparse.Namespace) -> int:
    try:
        stats = asyncio.run(
            run_relevance_gate(
                args.user_id,
                source_id=args.source_id,
                limit=args.limit,
                inbox_ids=args.inbox_ids,
                force=args.force,
                client=_build_client(args),
            )
        )
    except LLMQuotaError as e:
        print(_quota_msg(e), file=sys.stderr)
        return 1
    except LLMError as e:
        print(f"error LLM: {e}", file=sys.stderr)
        return 1
    print(
        f"gate: {stats.messages} correos juzgados en {stats.windows} ventanas — "
        f"relevantes={stats.relevant} no-relevantes={stats.not_relevant} "
        f"insuficientes={stats.insufficient} (por regla={stats.by_rule}) "
        f"errores={stats.errors} costo=${stats.cost.total.cost_usd}"
    )
    if stats.windows == 0:
        print("(nada pendiente o gate apagado — ver `memex-relevance settings show`)")
    return 0


def cmd_mine(args: argparse.Namespace) -> int:
    from memex.relevance.mining import run_rule_mining

    try:
        stats = asyncio.run(
            run_rule_mining(
                args.user_id,
                limit=args.limit,
                min_messages=args.min_count,
                client=_build_client(args),
            )
        )
    except LLMQuotaError as e:
        print(_quota_msg(e), file=sys.stderr)
        return 1
    except LLMError as e:
        print(f"error LLM: {e}", file=sys.stderr)
        return 1
    print(
        f"minería: {stats.proposed} propuestas — activadas={stats.activated} "
        f"rechazadas={stats.rejected} duplicadas={stats.skipped} "
        f"costo=${stats.cost.total.cost_usd}"
    )
    if stats.senders == 0:
        print("(ningún remitente llegó al umbral de acumulación — sin llamada LLM)")
    return 0


def _print_settings(s: GateSettings) -> None:
    estado = "ENCENDIDO" if s.enabled else "apagado"
    if s.provider == "anthropic":
        modelo = s.model
    elif s.provider == "codex":
        modelo = f"codex/{s.codex_model or 'default'}"
    else:
        modelo = f"{s.provider}/default"
    print(
        f"gate: {estado} — proveedor={s.provider} modo={s.mode} modelo={modelo} "
        f"umbral-minería={s.mining_min_messages}"
    )


def cmd_settings_show(args: argparse.Namespace) -> int:
    with connection() as conn:
        s = get_settings(conn, args.user_id)
    _print_settings(s)
    return 0


def cmd_settings_set(args: argparse.Namespace) -> int:
    enabled = None if args.enabled is None else args.enabled == "true"
    with connection() as conn:
        s = upsert_settings(
            conn,
            args.user_id,
            enabled=enabled,
            mode=args.mode,
            model=args.model,
            mining_min_messages=args.mining_min,
            provider=args.provider,
            codex_model=args.codex_model,
        )
    _print_settings(s)
    return 0


def cmd_interests(args: argparse.Namespace) -> int:
    with connection() as conn:
        if args.action == "add":
            try:
                row = create_interest(conn, args.user_id, args.text)
            except IntegrityError:
                print("ya existe un interés con ese texto", file=sys.stderr)
                return 1
            print(f"#{row['id']} {row['text']}")
            return 0
        if args.action == "list":
            rows = list_interests(conn, args.user_id)
            if not rows:
                print("(sin intereses)")
                return 0
            for r in rows:
                estado = "on " if r["enabled"] else "off"
                print(f"#{r['id']} [{estado}] {r['text']}")
            return 0
        if args.action in ("enable", "disable"):
            row2 = update_interest(
                conn, args.interest_id, args.user_id, enabled=args.action == "enable"
            )
            if row2 is None:
                print("interés inexistente", file=sys.stderr)
                return 1
            print(f"#{row2['id']} enabled={row2['enabled']}")
            return 0
        # remove
        if not delete_interest(conn, args.interest_id, args.user_id):
            print("interés inexistente", file=sys.stderr)
            return 1
        print("borrado")
        return 0


def _print_rule(r: dict[str, object]) -> None:
    report = r.get("dry_run_report") or {}
    matched = report.get("matched", "?") if isinstance(report, dict) else "?"
    print(
        f"#{r['id']} [{r['status']}] {r['kind']}={r['pattern']!r} "
        f"(propuso={r['proposed_by']}, dry-run matched={matched}) {r['rationale'] or ''}".rstrip()
    )


def cmd_rules(args: argparse.Namespace) -> int:
    with connection() as conn:
        if args.action == "list":
            status = None if args.status == "all" else args.status
            rows = list_rules(conn, args.user_id, status=status)
            if not rows:
                print("(sin reglas)")
                return 0
            for r in rows:
                _print_rule(r)
            return 0
        if args.action in ("enable", "disable"):
            new_status = "active" if args.action == "enable" else "disabled"
            row = set_rule_status(conn, args.rule_id, args.user_id, new_status)
            if row is None:
                print("regla inexistente o rechazada (no activable)", file=sys.stderr)
                return 1
            _print_rule(row)
            return 0
        # add (manual): corre el dry run; si no pasa NO se persiste (se muestra el reporte)
        report = dry_run_rule(conn, args.user_id, args.kind, args.pattern)
        if not report.passes:
            print(
                f"la regla atraparía {report.matched_relevant} correo(s) RELEVANTE(s) "
                f"(ej. inbox_ids={list(report.relevant_sample_ids)}) — no se crea",
                file=sys.stderr,
            )
            return 1
        row = create_rule(
            conn,
            args.user_id,
            kind=args.kind,
            pattern=args.pattern,
            proposed_by="manual",
            report=report,
            rationale=args.rationale or "",
        )
        if row is None:
            print("ya existe una regla con ese kind+pattern", file=sys.stderr)
            return 1
        _print_rule(row)
        return 0


def cmd_review(args: argparse.Namespace) -> int:
    with connection() as conn:
        if args.action == "list":
            rows = list_review_queue(conn, args.user_id, limit=args.limit)
            if not rows:
                print("(cola de revisión vacía)")
                return 0
            for r in rows:
                print(
                    f"inbox={r['inbox_id']} [{r['occurred_at']:%Y-%m-%d}] "
                    f"<{r['from_email'] or '—'}> {r['subject'] or '(sin asunto)'}\n"
                    f"  motivo: {r['reason'] or '—'}\n  {r['snippet']}"
                )
            return 0
        # resolve
        is_relevant = bool(args.relevant)
        ok = resolve_insufficient(
            conn,
            user_id=args.user_id,
            inbox_id=args.inbox_id,
            is_relevant=is_relevant,
            reason=args.reason,
        )
        if not ok:
            print("ese mensaje no tiene un veredicto 'insufficient' pendiente", file=sys.stderr)
            return 1
        print(f"inbox={args.inbox_id} resuelto: {'relevante' if is_relevant else 'no relevante'}")
        return 0


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
            f"[{r['status']}] {r['procedure']} {r['sender_label']} <{r['email'] or '—'}> "
            f"msgs={r['messages']} rel={pct_s} inertes={r['inert']} score={r['score']}"
        )
    return 0


def _add_provider_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--provider",
        choices=["anthropic", "codex", "deepseek"],
        default=None,
        help="override por corrida (default: el provider de los settings del gate)",
    )
    p.add_argument(
        "--codex-model",
        default=None,
        help="modelo para --provider codex (default: el del CLI de codex)",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memex-relevance")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="corre el gate sobre los correos pendientes (LLM, paga)")
    p_run.add_argument("--user-id", type=int, default=1)
    p_run.add_argument("--source-id", type=int, default=None)
    p_run.add_argument("--limit", type=int, default=200)
    p_run.add_argument("--inbox-ids", type=int, nargs="*", default=None)
    p_run.add_argument(
        "--force", action="store_true", help="re-juzga (borra veredictos no manuales)"
    )
    _add_provider_flags(p_run)
    p_run.set_defaults(func=cmd_run)

    p_mine = sub.add_parser("mine", help="minería de reglas sobre los no-relevantes (LLM, paga)")
    p_mine.add_argument("--user-id", type=int, default=1)
    p_mine.add_argument("--limit", type=int, default=500)
    p_mine.add_argument(
        "--min-count",
        type=int,
        default=None,
        help="umbral de acumulación por remitente (default: el setting del gate)",
    )
    _add_provider_flags(p_mine)
    p_mine.set_defaults(func=cmd_mine)

    p_set = sub.add_parser("settings", help="settings del gate")
    set_sub = p_set.add_subparsers(dest="action", required=True)
    p_show = set_sub.add_parser("show")
    p_show.add_argument("--user-id", type=int, default=1)
    p_show.set_defaults(func=cmd_settings_show)
    p_setset = set_sub.add_parser("set")
    p_setset.add_argument("--user-id", type=int, default=1)
    p_setset.add_argument("--enabled", choices=["true", "false"], default=None)
    p_setset.add_argument("--mode", choices=list(GATE_MODES), default=None)
    p_setset.add_argument("--model", default=None)
    p_setset.add_argument(
        "--mining-min",
        type=int,
        default=None,
        help="umbral de acumulación de la minería (correos no-relevantes por remitente)",
    )
    p_setset.add_argument(
        "--provider",
        choices=list(GATE_PROVIDERS),
        default=None,
        help="proveedor del gate (anthropic/codex/deepseek; codex: solo host-side, sin métricas; "
        "deepseek: barato, necesita DEEPSEEK_API_KEY)",
    )
    p_setset.add_argument(
        "--codex-model",
        default=None,
        help="modelo de codex ('' = volver al default del CLI)",
    )
    p_setset.set_defaults(func=cmd_settings_set)

    p_int = sub.add_parser("interests", help="CRUD de intereses personales")
    int_sub = p_int.add_subparsers(dest="action", required=True)
    p_add = int_sub.add_parser("add")
    p_add.add_argument("text")
    p_add.add_argument("--user-id", type=int, default=1)
    p_list = int_sub.add_parser("list")
    p_list.add_argument("--user-id", type=int, default=1)
    for action in ("enable", "disable", "remove"):
        p_act = int_sub.add_parser(action)
        p_act.add_argument("interest_id", type=int)
        p_act.add_argument("--user-id", type=int, default=1)
    for sp in int_sub.choices.values():
        sp.set_defaults(func=cmd_interests)

    p_rules = sub.add_parser("rules", help="reglas deterministas del gate")
    rules_sub = p_rules.add_subparsers(dest="action", required=True)
    p_rlist = rules_sub.add_parser("list")
    p_rlist.add_argument("--user-id", type=int, default=1)
    p_rlist.add_argument(
        "--status", default="all", choices=["active", "disabled", "rejected", "all"]
    )
    for action in ("enable", "disable"):
        p_ract = rules_sub.add_parser(action)
        p_ract.add_argument("rule_id", type=int)
        p_ract.add_argument("--user-id", type=int, default=1)
    p_radd = rules_sub.add_parser("add", help="alta manual (corre dry run primero)")
    p_radd.add_argument("--user-id", type=int, default=1)
    p_radd.add_argument("--kind", required=True, choices=list(RULE_KINDS))
    p_radd.add_argument("--pattern", required=True)
    p_radd.add_argument("--rationale", default=None)
    for sp in rules_sub.choices.values():
        sp.set_defaults(func=cmd_rules)

    p_rev = sub.add_parser("review", help="cola de revisión manual (insufficient)")
    rev_sub = p_rev.add_subparsers(dest="action", required=True)
    p_rvlist = rev_sub.add_parser("list")
    p_rvlist.add_argument("--user-id", type=int, default=1)
    p_rvlist.add_argument("--limit", type=int, default=50)
    p_resolve = rev_sub.add_parser("resolve")
    p_resolve.add_argument("inbox_id", type=int)
    group = p_resolve.add_mutually_exclusive_group(required=True)
    group.add_argument("--relevant", action="store_true")
    group.add_argument("--not-relevant", dest="relevant", action="store_false")
    p_resolve.add_argument("--reason", default=None)
    p_resolve.add_argument("--user-id", type=int, default=1)
    for sp in rev_sub.choices.values():
        sp.set_defaults(func=cmd_review)

    p_detect = sub.add_parser("detect", help="corre los procedimientos de candidatos (sin LLM)")
    p_detect.add_argument("--user-id", type=int, default=1)
    p_detect.set_defaults(func=cmd_detect)

    p_cand = sub.add_parser("candidates", help="lista los candidatos a (re)evaluar")
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
