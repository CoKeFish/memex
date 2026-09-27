"""CLI de asuntos (`memex-asuntos`; el agente la usa como `memex asuntos <cmd>`).

Operaciones frecuentes: listar/mostrar storylines, correr el asignador (ventana, pendientes o
reproceso), el curador y el mantenimiento, y las correcciones puntuales (cerrar, renombrar, fundir,
relacionar, borrar). Toda escritura pasa por `store`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import text

from memex.db import connection
from memex.logging import get_logger, setup_logging
from memex.modules.asuntos import store
from memex.modules.asuntos.settings import get_settings, upsert_settings
from memex.relevance.verdicts import EMAIL_TYPES


def _safe(s: str) -> str:
    enc = sys.stdout.encoding or "utf-8"
    return s.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


def _emit_json(obj: Any) -> None:
    print(_safe(json.dumps(obj, default=str, ensure_ascii=False)))


def _fmt(a: store.Asunto) -> str:
    last = a.last_signal_at.date().isoformat() if a.last_signal_at else "—"
    vence = f" vence={a.due_on.isoformat()}" if a.due_on else ""
    return (
        f"A{a.id:<5} {a.status:9s} {a.priority:6s} {a.kind:9s} n={a.evidence_count:<3} "
        f"última={last}{vence}  {a.name}" + (f"  | {a.state_text[:80]}" if a.state_text else "")
    )


def _cmd_list(args: argparse.Namespace) -> int:
    statuses = args.status.split(",") if args.status else None
    kinds = args.kind.split(",") if args.kind else None
    priorities = args.priority.split(",") if getattr(args, "priority", None) else None
    with connection() as conn:
        rows = store.list_asuntos(
            conn,
            args.user,
            statuses=statuses,
            kinds=kinds,
            priorities=priorities,
            q=args.q,
            limit=args.limit,
        )
    if args.json:
        _emit_json([a.__dict__ for a in rows])
        return 0
    for a in rows:
        _say(_fmt(a))
    _say(f"({len(rows)} asuntos)")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    with connection() as conn:
        a = store.get_asunto(conn, args.user, args.id)
        if a is None:
            _say("asunto no encontrado", err=True)
            return 2
        ev = store.evidence_of(conn, args.user, a.id)
        states = store.states_of(conn, args.user, a.id)
        rels = store.relations_of(conn, args.user, a.id)
    if args.json:
        _emit_json(
            {
                "asunto": a.__dict__,
                "evidence": [e.__dict__ for e in ev],
                "states": [s.__dict__ for s in states],
                "relations": [r.__dict__ for r in rels],
            }
        )
        return 0
    _say(_fmt(a))
    if a.aliases:
        _say(f"  alias: {', '.join(a.aliases)}")
    _say(f"  {a.description}")
    if a.entities:
        _say(f"  entidades: {', '.join(a.entities)}")
    if rels:
        _say("  relaciones:")
        for r in rels:
            arrow = "→" if r.direction == "out" else "←"
            _say(f"    {r.relation_type} {arrow} A{r.other_id} {r.other_name} [{r.other_status}]")
    _say("  hitos:")
    for e in ev:
        if e.hito_text:
            _say(f"    {e.hito_date or '?'}  {e.hito_text}  (#{e.inbox_id})")
    _say("  evidencia:")
    for e in ev:
        when = e.occurred_at.date().isoformat() if e.occurred_at else "?"
        _say(f"    #{e.inbox_id:<6} {when} {e.method:7s} {e.sender[:28]:28s} {e.subject[:60]}")
    if states:
        _say("  estados:")
        for s in states:
            flag = " [CERRADO]" if s.closed else ""
            _say(f"    {s.created_at:%Y-%m-%d} ({s.source}) {s.state_text}{flag}")
    return 0


def _cmd_assign(args: argparse.Namespace) -> int:
    from memex.modules.asuntos.assign import run_assign, run_pending

    if args.pending:
        res = asyncio.run(run_pending(args.user, limit=args.limit, dry_run=args.dry_run))
    else:
        ids: list[int] = []
        if args.inbox_ids:
            ids = [int(x) for x in args.inbox_ids.split(",") if x.strip()]
        elif args.since or args.until:
            with connection() as conn:
                sql = (
                    "SELECT i.id FROM inbox i JOIN sources s ON s.id = i.source_id "
                    "WHERE i.user_id = :u AND (s.type <> ALL(:email_types) OR EXISTS ("
                    "  SELECT 1 FROM relevance_verdicts v WHERE v.inbox_id = i.id "
                    "  AND v.verdict = 'relevant'))"
                )
                p: dict[str, Any] = {"u": args.user, "lim": args.limit, "email_types": EMAIL_TYPES}
                if args.since:
                    sql += " AND i.occurred_at >= :since"
                    p["since"] = args.since
                if args.until:
                    sql += " AND i.occurred_at < :until"
                    p["until"] = args.until
                sql += " ORDER BY i.occurred_at, i.id LIMIT :lim"
                ids = [int(x) for x in conn.execute(text(sql), p).scalars()]
        else:
            _say("indica --pending, --inbox-ids o --since/--until", err=True)
            return 2
        res = asyncio.run(run_assign(args.user, ids, force=args.force, dry_run=args.dry_run))
    d = res.as_dict()
    if args.json:
        _emit_json({**d, "proposals": res.proposals} if args.dry_run else d)
    elif args.dry_run:
        by_target: dict[str, list[dict[str, Any]]] = {}
        for p in res.proposals:
            by_target.setdefault(p["asunto"] or "(sin asunto)", []).append(p)
        _say(
            f"dry-run: mensajes={len(res.proposals)} lotes_llm={d['llm_batches']} "
            f"costo=${d['cost_usd']:.4f}"
        )
        for target, items in sorted(by_target.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            _say(f"  {target}: {len(items)} msg")
            for p in items[:6]:
                hito = f" · hito: {p['hito']}" if p.get("hito") else ""
                _say(f"      #{p['inbox_id']} conf={p['confianza']:.2f}{hito}")
    else:
        _say(
            f"asignador: considerados={d['considered']} relevantes={d['relevant']} "
            f"ya_vistos={d['skipped_seen']} adheridos={d['adhered']} lotes_llm={d['llm_batches']} "
            f"asignados={d['assigned']} sin_asunto={d['none']} nuevos={d['created']} "
            f"promovidos={d['promoted']} hitos={d['hitos']} estados={d['updates']} "
            f"cierres={d['closed']} relaciones={d['relations']} baja_conf={d['low_confidence']} "
            f"descartes={d['dropped']} errores={d['errors']} aristas={d['woven']} "
            f"costo=${d['cost_usd']:.4f}"
        )
    return 0 if d["errors"] == 0 else 1


def _cmd_curate(args: argparse.Namespace) -> int:
    from memex.modules.asuntos.curator import run_curator

    st = asyncio.run(
        run_curator(
            args.user,
            scope=args.scope,
            limit=args.limit,
            batch=args.batch,
            dry_run=args.dry_run,
            web_search=not args.no_web,
            timeout_s=args.timeout,
        )
    )
    if args.json:
        _emit_json(st.__dict__)
        return 0
    for a in st.applied:
        _say(f"  {a['item']:16s} {a['action']:13s} → {a['result']:28s} {a['rationale'][:90]}")
    _say(
        f"curador[{args.scope}]: items={st.items} llamadas={st.calls} fundidos={st.merged} "
        f"renombrados={st.renamed} cerrados={st.closed} reabiertos={st.reopened} "
        f"borrados={st.deleted} relaciones={st.related} rel_rechazadas={st.relations_rejected} "
        f"retipados={st.retyped} ev_rechazadas={st.evidence_rejected} "
        f"skip={st.skipped} descartes={st.dropped} errores={st.errors}"
        + (" (dry-run)" if args.dry_run else "")
    )
    return 0


def _cmd_maintain(args: argparse.Namespace) -> int:
    from memex.modules.asuntos.curator import run_maintenance

    st = asyncio.run(run_maintenance(args.user, web_search=not args.no_web, dry_run=args.dry_run))
    _emit_json(st.__dict__) if args.json else _say(f"maintain: {st.__dict__}")
    return 0


def _cmd_rules(args: argparse.Namespace) -> int:
    """Solo las reglas deterministas (sin LLM, sin curador): cierres por fecha, inactividad,
    promociones, plazos. Lo mismo que corre al final de cada corrida del asignador."""
    from memex.modules.asuntos.curator import run_system_rules

    with connection() as conn:
        st = run_system_rules(conn, args.user)
    _emit_json(st.__dict__) if args.json else _say(f"rules: {st.__dict__}")
    return 0


def _cmd_close(args: argparse.Namespace) -> int:
    with connection() as conn:
        if store.get_asunto(conn, args.user, args.id) is None:
            _say("asunto no encontrado", err=True)
            return 2
        try:
            n = store.resolve_pending_tareas(conn, args.user, args.id, args.tareas)
        except ValueError as e:
            _say(str(e), err=True)
            return 2
        store.record_state(
            conn, args.user, args.id, args.state or "", closed=True, source=args.source
        )
    extra = f" ({n} pendiente(s) {args.tareas.rstrip('r')}da(s))" if n else ""
    _say(f"A{args.id} cerrado{extra}")
    return 0


def _cmd_reopen(args: argparse.Namespace) -> int:
    with connection() as conn:
        ok = store.set_status(conn, args.user, args.id, "abierto", source=args.source)
    _say(f"A{args.id} {'reabierto' if ok else 'sin cambios'}")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    """Declara un asunto a mano. Nace `abierto` y no `candidato`: lo pidió el dueño, no hay nada
    que confirmar con más evidencia."""
    with connection() as conn:
        aid = store.create_asunto(
            conn,
            args.user,
            name=args.name,
            kind=args.kind,
            description=args.description or "",
            seed="manual",
            status="abierto",
            priority=args.priority or "normal",
            priority_source="user" if args.priority else None,
        )
    _say(f"A{aid} creado: {args.name}")
    if args.json:
        _say(json.dumps({"id": aid, "name": args.name, "kind": args.kind}, ensure_ascii=False))
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    """Arma el plan del asunto con codex + búsqueda web en fuentes oficiales."""
    from memex.modules.asuntos.planner import plan_asunto

    res = asyncio.run(
        plan_asunto(
            args.user,
            args.id,
            contexto=args.context or "",
            ciudad=args.city,
            dry_run=args.dry_run,
        )
    )
    plan = res.plan
    if args.json:
        _say(
            json.dumps(
                {
                    "asunto_id": res.asunto_id,
                    "resumen": plan.resumen,
                    "pasos": [
                        {
                            "titulo": p.titulo,
                            "que_hacer": p.que_hacer,
                            "donde": p.donde,
                            "llevar": list(p.llevar),
                            "costo": p.costo,
                            "fecha": p.fecha.isoformat() if p.fecha else None,
                            "fuente": p.fuente,
                            "por_que": p.por_que,
                        }
                        for p in plan.pasos
                    ],
                    "fuentes": list(plan.fuentes),
                    "sin_confirmar": list(plan.sin_confirmar),
                    "creadas": res.creadas,
                    "repetidas": res.repetidas,
                    "dry_run": res.dry_run,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if plan.resumen:
        _say(plan.resumen)
    for i, p in enumerate(plan.pasos, 1):
        fecha = f" · para el {p.fecha.isoformat()}" if p.fecha else ""
        _say(f"{i}. {p.titulo}{fecha}")
        for linea in describe_lines(p):
            _say(f"   {linea}")
    if plan.sin_confirmar:
        _say("")
        _say("Sin confirmar en fuente oficial:")
        for s_ in plan.sin_confirmar:
            _say(f"- {s_}")
    if not res.dry_run:
        _say("")
        _say(f"{res.creadas} paso(s) nuevo(s), {res.repetidas} que ya estaban.")
    return 0


def describe_lines(paso: object) -> list[str]:
    from memex.modules.asuntos.planner import describe

    return [ln for ln in describe(paso).split("\n") if ln]  # type: ignore[arg-type]


def _cmd_silence(args: argparse.Namespace) -> int:
    with connection() as conn:
        ok, canceladas = store.silence(conn, args.user, args.id)
    if not ok:
        _say("asunto no encontrado", err=True)
        return 2
    extra = f" ({canceladas} tarea(s) cancelada(s))" if canceladas else ""
    _say(f"A{args.id} silenciado: no se reabre solo{extra}")
    return 0


def _cmd_unsilence(args: argparse.Namespace) -> int:
    with connection() as conn:
        ok = store.unsilence(conn, args.user, args.id)
    _say(f"A{args.id} {'ya no está silenciado' if ok else 'no estaba silenciado'}")
    return 0


def _cmd_nota(args: argparse.Namespace) -> int:
    """Contarle algo al asunto: queda como hito y, si trae fecha o algo hecho, se aplica."""
    from memex.modules.asuntos.notas import anotar

    res = asyncio.run(anotar(args.user, args.id, args.text, interpretar=not args.solo_anotar))
    if args.json:
        _emit_json(
            {
                "nota_id": res.nota_id,
                "asunto_id": res.asunto_id,
                "entendido": res.entendido,
                "cambios": res.cambios,
                "interpretada": res.interpretada,
            }
        )
        return 0
    _say(f"Anotado en A{res.asunto_id}: «{res.texto}»")
    if not res.interpretada:
        _say("  (guardada sin interpretar: no hubo intérprete disponible)")
        return 0
    if res.entendido:
        _say(f"  Entendí: {res.entendido}")
    for c in res.cambios:
        _say(f"  · {c}")
    if not res.cambios:
        _say("  Sin cambios que aplicar: queda como hito.")
    return 0


def _cmd_vence(args: argparse.Namespace) -> int:
    from datetime import date as _date

    raw = args.date.strip().lower()
    due = None if raw in ("none", "null", "-", "") else _date.fromisoformat(raw)
    with connection() as conn:
        ok = store.set_due(conn, args.user, args.id, due, source="user")
    shown = due.isoformat() if due else "(sin plazo)"
    _say(f"A{args.id} vence {shown}{'' if ok else ' (sin cambios)'}")
    return 0


def _cmd_prioridad(args: argparse.Namespace) -> int:
    with connection() as conn:
        ok = store.set_priority(
            conn, args.user, args.id, args.level, reason=args.reason or "", source="user"
        )
    _say(f"A{args.id} prioridad {args.level}{'' if ok else ' (sin cambios)'}")
    return 0


def _cmd_rename(args: argparse.Namespace) -> int:
    with connection() as conn:
        store.rename(conn, args.user, args.id, args.name)
    _say(f"A{args.id} renombrado (el anterior queda como alias)")
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    with connection() as conn:
        ok = store.merge_asuntos(conn, args.user, survivor_id=args.into, absorbed_id=args.absorb)
    _say(f"A{args.absorb} → A{args.into}: {'fundido' if ok else 'sin cambios'}")
    return 0 if ok else 1


def _cmd_relate(args: argparse.Namespace) -> int:
    with connection() as conn:
        ok = store.add_relation(
            conn, args.user, args.src, args.dst, args.type, confidence=1.0, source=args.source
        )
    _say(f"A{args.src} {args.type} A{args.dst}: {'creada' if ok else 'ya existía / inválida'}")
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    with connection() as conn:
        ok = store.delete_asunto(conn, args.user, args.id)
    _say(f"A{args.id} {'borrado' if ok else 'no encontrado'}")
    return 0 if ok else 2


def _cmd_evidence(args: argparse.Namespace) -> int:
    with connection() as conn:
        if args.reject:
            aid = store.reject_evidence(conn, args.user, args.reject)
            _say(f"evidencia {args.reject} rechazada (asunto A{aid})" if aid else "no encontrada")
            return 0 if aid else 2
        if args.add:
            store.add_evidence(
                conn,
                args.user,
                args.add,
                args.inbox,
                method="agent" if args.source == "agent" else "manual",
                hito_date=date.fromisoformat(args.hito_date) if args.hito_date else None,
                hito_text=args.hito,
                reactivate=True,
            )
            _say(f"mensaje #{args.inbox} agregado a A{args.add}")
            return 0
    _say("indica --reject ID o --add ASUNTO --inbox INBOX", err=True)
    return 2


def _cmd_settings(args: argparse.Namespace) -> int:
    changes = {
        k: getattr(args, k)
        for k in (
            "enabled",
            "adhesion_enabled",
            "batch_size",
            "body_chars",
            "closed_window_days",
            "inactive_days",
            "min_evidence_open",
            "min_confidence",
        )
        if getattr(args, k) is not None
    }
    with connection() as conn:
        s = (
            upsert_settings(conn, args.user, **changes)
            if changes
            else get_settings(conn, args.user)
        )
    _emit_json(s.__dict__)
    return 0


def _cmd_dato(args: argparse.Namespace) -> int:
    from memex.modules.asuntos.datos import add_dato, datos_of, remove_dato

    with connection() as conn:
        if args.remove:
            ok = remove_dato(conn, args.user, args.id, args.remove)
            _say("dato quitado" if ok else "no había ese dato")
            return 0
        if args.value:
            created = add_dato(
                conn, args.user, args.id, label=args.label, value=args.value, origin="manual"
            )
            _say("dato nuevo" if created else "ya estaba (se sumó la etiqueta)")
        for d in datos_of(conn, args.user, args.id):
            _say(f"  {d.get('label')}: {d.get('value')}  (mensajes {d.get('inbox_ids')})")
    return 0


def _cmd_purge(args: argparse.Namespace) -> int:
    """Borra en lote los asuntos de un estado (con sus tareas). Sin `--apply` solo lista."""
    from memex.modules.tareas.module import delete as delete_tarea
    from memex.modules.tareas.module import list_tareas

    statuses = [s for s in args.status.split(",") if s]
    valid = ("candidato", "abierto", "cerrado", "inactivo")
    bad = [s for s in statuses if s not in valid]
    if bad:
        _say(f"estado inválido: {bad}; válidos: {', '.join(valid)}", err=True)
        return 2
    with connection() as conn:
        rows = store.list_asuntos(conn, args.user, statuses=statuses, limit=10000)
    for a in rows:
        _say(_fmt(a))
    if not args.apply:
        _say(f"({len(rows)} asuntos a borrar; es un ensayo: agregá --apply para borrarlos)")
        return 0
    borradas = 0
    redirecciones = 0
    with connection() as conn:
        for a in rows:
            for t in list_tareas(conn, args.user, status=None, asunto_id=a.id, limit=5000):
                delete_tarea(conn, args.user, int(t["id"]))
                borradas += 1
            # Los asuntos fusionados DENTRO de este son redirecciones: se van con él (si no,
            # al quedar sin destino resucitan como asuntos visibles).
            for m in store.list_asuntos(conn, args.user, include_merged=True, limit=10000):
                if m.merged_into_id == a.id:
                    store.delete_asunto(conn, args.user, m.id)
                    redirecciones += 1
            store.delete_asunto(conn, args.user, a.id)
    _say(
        f"borrados {len(rows)} asuntos y {borradas} tareas"
        + (f" (+{redirecciones} fusionados dentro de ellos)" if redirecciones else "")
    )
    return 0


def _cmd_forget(args: argparse.Namespace) -> int:
    ids = [int(x) for x in args.inbox_ids.split(",") if x.strip()]
    with connection() as conn:
        n = store.forget(conn, args.user, ids)
    _say(f"olvidadas {n} evidencias de {len(ids)} mensajes")
    return 0


def _bool(v: str) -> bool:
    return v.lower() in ("1", "true", "si", "sí", "yes", "on")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memex-asuntos", description="Asuntos (storylines)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--user", type=int, default=1)
        sp.add_argument("--json", action="store_true")
        return sp

    sp = add("list", "lista asuntos (--status abierto,candidato --kind --priority alta,normal --q)")
    sp.add_argument("--status")
    sp.add_argument("--kind")
    sp.add_argument("--priority", help="alta,normal,baja (coma-separado)")
    sp.add_argument("--q")
    sp.add_argument("--limit", type=int, default=200)
    sp = add("show", "storyline de un asunto: hitos, evidencia, estados, relaciones")
    sp.add_argument("--id", type=int, required=True)
    sp = add("assign", "corre el asignador (--pending | --inbox-ids | --since/--until)")
    sp.add_argument("--pending", action="store_true")
    sp.add_argument("--inbox-ids")
    sp.add_argument("--since")
    sp.add_argument("--until")
    sp.add_argument("--limit", type=int, default=400)
    sp.add_argument("--force", action="store_true", help="olvida la decisión previa y re-decide")
    sp.add_argument(
        "--dry-run", action="store_true", dest="dry_run", help="juzga y reporta, no escribe"
    )
    sp = add(
        "curate",
        "curador codex (--scope duplicates|candidates|open|relations|names|evidence|all)",
    )
    sp.add_argument("--scope", default="all")
    sp.add_argument("--limit", type=int, default=200)
    sp.add_argument("--batch", type=int, default=40)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--no-web", action="store_true")
    sp.add_argument("--timeout", type=float, default=600)
    sp = add("maintain", "reglas del sistema + curador all (= job asuntos_curate)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--no-web", action="store_true")
    add("rules", "solo las reglas del sistema, sin LLM (cierres por fecha, inactividad, promoción)")
    sp = add(
        "close", "cierra un asunto (--state «estado final» [--tareas dejar|completar|cancelar])"
    )
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--state")
    sp.add_argument("--source", default="agent", choices=("agent", "manual"))
    sp.add_argument(
        "--tareas",
        default="dejar",
        choices=list(store.TAREAS_AL_CERRAR),
        help=(
            "qué hacer con las pendientes: dejarlas como están, darlas por hechas o cancelarlas "
            "(lo que ya no aplica)"
        ),
    )
    sp = add(
        "prioridad", "fija la prioridad del seguimiento (--id --level alta|normal|baja [--reason])"
    )
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--level", required=True, choices=sorted(store.PRIORITIES))
    sp.add_argument("--reason", default="")
    sp = add("vence", "fija la fecha límite del próximo paso (--id --date YYYY-MM-DD | none)")
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--date", required=True)
    sp = add("add", "declara un asunto a mano (no nace de un mensaje)")
    sp.add_argument("--name", required=True)
    sp.add_argument("--kind", default="tramite", choices=sorted(store.KINDS))
    sp.add_argument("--description", default="")
    sp.add_argument("--priority", default=None, choices=list(store.PRIORITIES))

    sp = add("plan", "arma el plan del asunto buscando en fuentes oficiales (codex + web)")
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--context", default="", help="lo que el dueño quiera agregar")
    sp.add_argument("--city", default="Bogotá, Colombia")
    sp.add_argument("--dry-run", action="store_true", help="muestra el plan sin crear las tareas")
    sp = add(
        "nota",
        "contale algo al asunto (--id --text): queda como hito y, si trae una fecha o algo "
        "hecho, le pone fecha al paso o lo marca hecho",
    )
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--text", required=True, help="la nota, en tus palabras")
    sp.add_argument(
        "--solo-anotar", action="store_true", dest="solo_anotar", help="guardar sin interpretar"
    )

    sp = add("silence", "silencia un asunto: queda cerrado aunque siga llegando señal")
    sp.add_argument("id", type=int)
    sp = add("unsilence", "levanta el silencio (no lo reabre por sí solo)")
    sp.add_argument("id", type=int)

    sp = add("reopen", "reabre un asunto")
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--source", default="agent", choices=("agent", "manual"))
    sp = add("rename", "renombra (el nombre anterior queda como alias)")
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--name", required=True)
    sp = add(
        "merge", "funde --absorb dentro de --into (alias, evidencia, estados y datos se mueven)"
    )
    sp.add_argument("--into", type=int, required=True)
    sp.add_argument("--absorb", type=int, required=True)
    sp = add("dato", "dato clave del asunto (--id --label --value | --id --remove <valor>)")
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--label", default="")
    sp.add_argument("--value", default=None)
    sp.add_argument("--remove", default=None, help="quita el dato con ese valor")
    sp = add("relate", "relación entre asuntos (--src --dst --type parte_de|requisito_de|continua)")
    sp.add_argument("--src", type=int, required=True)
    sp.add_argument("--dst", type=int, required=True)
    sp.add_argument("--type", required=True, choices=sorted(store.RELATION_TYPES))
    sp.add_argument("--source", default="agent", choices=("agent", "manual"))
    sp = add("delete", "borra un asunto (candidatos/ruido)")
    sp.add_argument("--id", type=int, required=True)
    sp = add(
        "purge",
        "borra en lote los asuntos de un estado (inactivo|cerrado|candidato) con sus tareas; "
        "ensayo sin --apply",
    )
    sp.add_argument("--status", required=True, help="estados separados por coma")
    sp.add_argument("--apply", action="store_true")
    sp = add(
        "evidence", "rechaza una evidencia (--reject ID) o agrega un mensaje (--add A --inbox N)"
    )
    sp.add_argument("--reject", type=int)
    sp.add_argument("--add", type=int)
    sp.add_argument("--inbox", type=int)
    sp.add_argument("--hito")
    sp.add_argument("--hito-date")
    sp.add_argument("--source", default="agent", choices=("agent", "manual"))
    sp = add("settings", "muestra o cambia settings (--enabled true --batch-size 40 …)")
    sp.add_argument("--enabled", type=_bool)
    sp.add_argument("--adhesion-enabled", type=_bool, dest="adhesion_enabled")
    sp.add_argument("--batch-size", type=int, dest="batch_size")
    sp.add_argument("--body-chars", type=int, dest="body_chars")
    sp.add_argument("--closed-window-days", type=int, dest="closed_window_days")
    sp.add_argument("--inactive-days", type=int, dest="inactive_days")
    sp.add_argument("--min-evidence-open", type=int, dest="min_evidence_open")
    sp.add_argument("--min-confidence", type=float, dest="min_confidence")
    sp = add("forget", "olvida la decisión de mensajes (--inbox-ids 1,2,3) para re-decidir")
    sp.add_argument("--inbox-ids", required=True)
    return p


_DISPATCH = {
    "list": _cmd_list,
    "show": _cmd_show,
    "assign": _cmd_assign,
    "curate": _cmd_curate,
    "maintain": _cmd_maintain,
    "rules": _cmd_rules,
    "close": _cmd_close,
    "reopen": _cmd_reopen,
    "add": _cmd_add,
    "plan": _cmd_plan,
    "nota": _cmd_nota,
    "silence": _cmd_silence,
    "unsilence": _cmd_unsilence,
    "prioridad": _cmd_prioridad,
    "vence": _cmd_vence,
    "rename": _cmd_rename,
    "merge": _cmd_merge,
    "dato": _cmd_dato,
    "relate": _cmd_relate,
    "delete": _cmd_delete,
    "purge": _cmd_purge,
    "evidence": _cmd_evidence,
    "settings": _cmd_settings,
    "forget": _cmd_forget,
}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.modules.asuntos.cli")
    args = _build_parser().parse_args(argv)
    log.info("asuntos.cli.start", cmd=args.cmd)
    try:
        return _DISPATCH[args.cmd](args)
    except Exception as e:
        log.exception("asuntos.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        _say(f"error: {e}", err=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
