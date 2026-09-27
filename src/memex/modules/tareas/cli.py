"""CLI `memex-tareas` — pendientes accionables (registrador determinista, sin LLM).

La ENTRADA del módulo: el agente entiende el lenguaje natural y llama a estos subcomandos con
campos estructurados. memex guarda, deduplica (clave natural + resolución difusa) y conecta.

Subcomandos:
  add       — crea una tarea (o devuelve la existente si es un duplicado: `deduped: true`).
  list      — pendientes (default): vencidas primero, luego por fecha límite, sin fecha al final.
  show      — detalle de una tarea.
  done      — la marca completada (sella `done_at`). reopen — la vuelve a pendiente.
  cancel    — la cancela (sale del grafo, no se borra).
  update    — corrige título/fecha/prioridad/asunto/descripción.
  rm        — borra la fila (mantenimiento; el agente usa `cancel`).
  help      — resumen de los comandos.

Todos aceptan `--json` (la respuesta es la ÚLTIMA línea de stdout). Exit 0 si OK, 1 si error.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from typing import Any

from dotenv import load_dotenv
from sqlalchemy.engine import Connection

from memex.core.localtime import LOCAL_TZ
from memex.db import connection
from memex.logging import get_logger, setup_logging
from memex.modules.tareas.module import (
    STATUS_CANCELADA,
    STATUS_COMPLETADA,
    STATUS_PENDIENTE,
    STATUS_VENCIDA,
    AsuntoNotFoundError,
    TareaNotFoundError,
    add,
    delete,
    get,
    list_tareas,
    resolve_asunto,
    set_status,
    update,
)


def _safe(text_: str) -> str:
    enc = sys.stdout.encoding or "utf-8"
    return text_.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


def _emit_json(obj: Any) -> None:
    print(_safe(json.dumps(obj, default=str, ensure_ascii=False)))


def _parse_date(s: str | None) -> date | None:
    if not s or not s.strip():
        return None
    return date.fromisoformat(s.strip())


def _parse_when(s: str | None) -> datetime | None:
    """`--when` de `done`: fecha o fecha-hora; naive = hora LOCAL (Bogotá), como los otros CLIs."""
    if not s or not s.strip():
        return None
    s = s.strip()
    try:
        d = date.fromisoformat(s)
        return datetime.combine(d, datetime.min.time(), tzinfo=LOCAL_TZ)
    except ValueError:
        pass
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=LOCAL_TZ)


_HELP = """memex-tareas — pendientes accionables (registrador determinista, sin LLM).

Comandos:
  add       crea una tarea: --title [--due YYYY-MM-DD] [--priority alta|normal|baja]
            [--asunto <id|texto>] [--description] [--force]
            Dedup: mismo título (normalizado) entre pendientes del mismo asunto (o sueltas) =
            la misma tarea → devuelve la existente con deduped=true; --force la crea igual.
  list      pendientes por default (--status completada|cancelada|vencida|all); --overdue solo
            pendientes con fecha pasada;
            --due-before YYYY-MM-DD; --asunto <id|texto>; --q texto
            Orden: vencidas primero, luego fecha límite, las SIN fecha al final; dentro, prioridad.
  show      detalle (--id)
  done      marca completada (--id [--when ISO]);  reopen — vuelve a pendiente (--id)
  cancel    cancela (--id): sale del grafo, no se borra
  update    corrige (--id) --title/--description/--due/--clear-due/--priority/--asunto/
            --clear-asunto
  rm        borra la fila (--id) — mantenimiento; el agente usa cancel
  help      muestra esta ayuda

Reglas:
  --event <id>  hechos del MISMO mensaje comparten el id → el grafo los conecta
  --json        la respuesta JSON es la ÚLTIMA línea de stdout (las previas son logs)

Flags de cada comando: memex-tareas <comando> -h"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-tareas")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("help", help="Resumen de los comandos.")

    p = sub.add_parser("add", help="Crea una tarea (con dedup).")
    p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    p.add_argument("--title", required=True, help="Qué hay que hacer (corto y concreto).")
    p.add_argument("--description", default="", help="Detalle libre.")
    p.add_argument("--due", default=None, help="Fecha límite YYYY-MM-DD (opcional).")
    p.add_argument("--priority", default="normal", help="alta | normal | baja (default normal).")
    p.add_argument(
        "--categoria",
        default=None,
        help="seguridad | entrega | tramite | pago | inscripcion | dinamica | otro (default otro).",
    )
    p.add_argument("--asunto", default=None, help="Asunto al que pertenece: id o texto del nombre.")
    p.add_argument(
        "--source-text", default=None, help="El mensaje original del agente (procedencia)."
    )
    p.add_argument("--event", default=None, help="Id de correlación del mensaje (hechos hermanos).")
    p.add_argument(
        "--parent", type=int, default=None, help="Es un paso de este proyecto (id de tarea)."
    )
    p.add_argument("--force", action="store_true", help="Crear aunque parezca duplicada.")
    p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    p = sub.add_parser("list", help="Lista tareas (pendientes por default).")
    p.add_argument("--user", type=int, default=1)
    p.add_argument(
        "--status",
        default=STATUS_PENDIENTE,
        choices=(STATUS_PENDIENTE, STATUS_COMPLETADA, STATUS_CANCELADA, STATUS_VENCIDA, "all"),
    )
    p.add_argument("--overdue", action="store_true", help="Solo pendientes vencidas.")
    p.add_argument("--due-before", default=None, help="Con fecha límite hasta YYYY-MM-DD.")
    p.add_argument("--asunto", default=None, help="Filtra por asunto (id o texto).")
    p.add_argument("--q", default=None, help="Texto en título/descripción/asunto.")
    p.add_argument("--categoria", default=None, help="Solo esta categoría (seguridad, entrega…).")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("show", help="Detalle de una tarea.")
    p.add_argument("--user", type=int, default=1)
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--json", action="store_true", dest="as_json")

    for name, help_ in (
        ("done", "Marca la tarea como completada."),
        ("reopen", "Vuelve la tarea a pendiente."),
        ("cancel", "Cancela la tarea (sale del grafo; no se borra)."),
        ("rm", "Borra la fila (mantenimiento)."),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--user", type=int, default=1)
        p.add_argument("--id", type=int, required=True)
        if name == "done":
            p.add_argument("--when", default=None, help="Cuándo se hizo (ISO; default ahora).")
        p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("discard", help="«No es una tarea»: la cancela, bloquea y deja feedback.")
    p.add_argument("--user", type=int, default=1)
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--reason", default="no_es_tarea", help="Razón corta (default no_es_tarea).")
    p.add_argument("--note", default="", help="Nota libre para la revisión del prompt.")
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser(
        "link", help="Suma un enlace (plantilla, video, carpeta) a una tarea o proyecto."
    )
    p.add_argument("--user", type=int, default=1)
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--url", action="append", required=True, help="URL (repetible).")

    p = sub.add_parser(
        "expire",
        help="Cierra como vencidas las tareas de curso o «próxima sesión» cuya fecha ya pasó.",
    )
    p.add_argument("--user", type=int, default=1)
    p.add_argument("--apply", action="store_true", help="sin esto es dry-run")

    p = sub.add_parser(
        "relink",
        help="Recuelga del asunto las tareas extraídas sueltas (y fecha «próxima sesión»).",
    )
    p.add_argument("--user", type=int, default=1)
    p.add_argument("--apply", action="store_true", help="sin esto es dry-run")

    p = sub.add_parser(
        "embed",
        help=(
            "Bloqueo semántico: calcula (o refresca) el vector del título de cada tarea no "
            "cancelada; solo embebe lo que cambió. Correr una vez tras activar el módulo."
        ),
    )
    p.add_argument("--user", type=int, default=1)
    p.add_argument("--json", dest="as_json", action="store_true")

    p = sub.add_parser(
        "ritmo",
        help=(
            "El ritmo de un asunto deducido de sus datos: plazo típico de una consigna y, por "
            "proyecto, días con pasos, cadencia, gracia y cuándo vence."
        ),
    )
    p.add_argument("--user", type=int, default=1)
    p.add_argument("--asunto", type=int, required=True, help="Id del asunto.")
    p.add_argument("--json", dest="as_json", action="store_true")

    p = sub.add_parser(
        "recordatorios",
        help=(
            "Materializa como tarea los recordatorios del calendario (eventos de día completo "
            "cuyo título es una acción: «Pagar arriendo…») que caen en los próximos días."
        ),
    )
    p.add_argument("--user", type=int, default=1)
    p.add_argument("--dias", type=int, default=None, help="Horizonte en días (default 7).")
    p.add_argument("--apply", action="store_true", help="sin esto es dry-run")
    p.add_argument("--json", dest="as_json", action="store_true")

    p = sub.add_parser(
        "dedup-parents",
        help=(
            "Funde los proyectos repetidos de un asunto (mismo nombre o uno contenido en el otro) "
            "y, con --flatten-singles, deshace los «proyectos» de un solo paso sin enlaces."
        ),
    )
    p.add_argument("--user", type=int, default=1)
    p.add_argument("--asunto", type=int, default=None, help="solo ese asunto; sin esto, todos")
    p.add_argument("--apply", action="store_true", help="sin esto es dry-run")
    p.add_argument("--flatten-singles", action="store_true", dest="flatten_singles")

    p = sub.add_parser(
        "merge",
        help="Funde dos tareas que son la misma: --absorb enriquece a --into y queda cancelada.",
    )
    p.add_argument("--user", type=int, default=1)
    p.add_argument("--into", type=int, required=True, help="La que queda.")
    p.add_argument("--absorb", type=int, required=True, help="La que se funde en la otra.")
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser(
        "recheck",
        help="Re-busca duplicados con el juez para tareas colgadas tarde de su asunto "
        "(o --since / --ids).",
    )
    p.add_argument("--user", type=int, default=1)
    p.add_argument("--since", default=None, help="YYYY-MM-DD: las dadas de alta desde ese día.")
    p.add_argument("--ids", default=None, help="ids coma-separados.")
    p.add_argument("--dry-run", action="store_true", help="Solo cuenta; no funde.")
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("update", help="Corrige campos de una tarea.")
    p.add_argument("--user", type=int, default=1)
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--title", default=None)
    p.add_argument("--description", default=None)
    p.add_argument("--due", default=None, help="Nueva fecha límite YYYY-MM-DD.")
    p.add_argument("--clear-due", action="store_true", help="Quita la fecha límite.")
    p.add_argument("--priority", default=None, help="alta | normal | baja.")
    p.add_argument("--categoria", default=None, help="Nueva categoría (seguridad, entrega…).")
    p.add_argument("--asunto", default=None, help="Nuevo asunto (id o texto).")
    p.add_argument("--clear-asunto", action="store_true", help="Desliga la tarea del asunto.")
    p.add_argument("--parent", type=int, default=None, help="Colgarla como paso de este proyecto.")
    p.add_argument("--clear-parent", action="store_true", help="Descolgarla del proyecto.")
    p.add_argument("--json", action="store_true", dest="as_json")
    return parser


def add_from_args(
    conn: Connection, user_id: int, args: argparse.Namespace, *, event_id: str | None = None
) -> dict[str, Any]:
    """Mapea `args` → `tareas.add` sobre un `conn` DADO. Lo reusan `_cmd_add` y el cierre de evento
    del agente (tx compartida). `event_id`, si viene, pisa `args.event`."""
    metadata = {"source_text": args.source_text} if args.source_text else None
    return add(
        conn,
        user_id,
        title=args.title,
        description=args.description or "",
        due_on=_parse_date(args.due),
        priority=args.priority,
        category=getattr(args, "categoria", None),
        asunto=args.asunto,
        metadata=metadata,
        event_id=event_id if event_id is not None else args.event,
        force=bool(args.force),
        parent=getattr(args, "parent", None),
    )


def _fmt(t: dict[str, Any]) -> str:
    due = t["due_on"] or "sin fecha"
    flag = " ¡VENCIDA!" if t.get("overdue") else ""
    asunto = f" · {t['asunto_name']}" if t.get("asunto_name") else ""
    prio = "" if t["priority"] == "normal" else f" [{t['priority']}]"
    cat = f" <{t['category']}>" if t.get("category") and t["category"] != "otro" else ""
    state = "" if t["status"] == STATUS_PENDIENTE else f" ({t['status']})"
    step = f" ↳ paso de #{t['parent_id']}" if t.get("parent_id") else ""
    return f"  #{t['id']} {due}{flag}{prio}{cat} {t['title']}{asunto}{state}{step}"


def _cmd_add(args: argparse.Namespace) -> int:
    with connection() as conn:
        row = add_from_args(conn, args.user, args)
    if args.as_json:
        _emit_json(row)
        return 0
    if row.get("deduped"):
        _say(f"ya existía (dedup {row['matched_by']}): #{row['id']} {row['title']}")
    else:
        _say(f"tarea #{row['id']} creada: {row['title']} ({row['due_on'] or 'sin fecha'})")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    with connection() as conn:
        asunto_id = resolve_asunto(conn, args.user, args.asunto) if args.asunto else None
        rows = list_tareas(
            conn,
            args.user,
            status=args.status,
            overdue_only=args.overdue,
            due_before=_parse_date(args.due_before),
            asunto_id=asunto_id,
            q=args.q,
            limit=args.limit,
            category=getattr(args, "categoria", None),
        )
    if args.as_json:
        _emit_json({"count": len(rows), "items": rows})
        return 0
    if not rows:
        _say("\nSin tareas que cumplan el filtro.\n")
        return 0
    _say(f"\n{len(rows)} tarea(s):")
    for t in rows:
        _say(_fmt(t))
    _say("")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    with connection() as conn:
        t = get(conn, args.user, args.id)
    if args.as_json:
        _emit_json(t)
        return 0
    _say(_fmt(t))
    if t["description"]:
        _say(f"    {t['description']}")
    if t["done_at"]:
        _say(f"    completada: {t['done_at']}")
    return 0


def _cmd_status(args: argparse.Namespace, status: str) -> int:
    when = _parse_when(getattr(args, "when", None)) if status == STATUS_COMPLETADA else None
    with connection() as conn:
        t = set_status(conn, args.user, args.id, status, when=when)
    if args.as_json:
        _emit_json(t)
    else:
        _say(f"tarea #{t['id']} → {t['status']}: {t['title']}")
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    kwargs: dict[str, Any] = {}
    if args.title is not None:
        kwargs["title"] = args.title
    if args.description is not None:
        kwargs["description"] = args.description
    if args.clear_due:
        kwargs["due_on"] = None
    elif args.due is not None:
        kwargs["due_on"] = _parse_date(args.due)
    if args.priority is not None:
        kwargs["priority"] = args.priority
    if getattr(args, "categoria", None) is not None:
        kwargs["category"] = args.categoria
    if args.clear_asunto:
        kwargs["asunto"] = None
    elif args.asunto is not None:
        kwargs["asunto"] = args.asunto
    if args.clear_parent:
        kwargs["parent"] = None
    elif args.parent is not None:
        kwargs["parent"] = args.parent
    with connection() as conn:
        t = update(conn, args.user, args.id, **kwargs)
    if args.as_json:
        _emit_json(t)
    else:
        _say(f"tarea #{t['id']} actualizada: {t['title']} ({t['due_on'] or 'sin fecha'})")
    return 0


def _cmd_link(args: argparse.Namespace) -> int:
    from memex.modules.tareas.module import attach_links

    with connection() as conn:
        n = attach_links(conn, args.user, args.id, list(args.url))
    _say(f"tarea #{args.id}: {n} enlaces nuevos")
    return 0


def _cmd_discard(args: argparse.Namespace) -> int:
    from memex.modules.tareas.module import discard

    with connection() as conn:
        t = discard(conn, args.user, args.id, reason=args.reason, note=args.note)
    if args.as_json:
        _say(json.dumps(t, default=str, ensure_ascii=False))
    else:
        _say(f"tarea #{t['id']} descartada ({args.reason}): {t['title']}")
    return 0


def _cmd_expire(args: argparse.Namespace) -> int:
    from memex.modules.tareas.module import expire_overdue

    with connection() as conn:
        ids = expire_overdue(conn, args.user, apply=args.apply)
    mode = "APLICADO" if args.apply else "dry-run (agregá --apply)"
    _say(f"{mode}: {len(ids)} tareas vencidas" + (f" → ids {ids}" if ids else ""))
    return 0


def _cmd_ritmo(args: argparse.Namespace) -> int:
    from datetime import timedelta

    from sqlalchemy import text

    from memex.modules.tareas.ritmo import cadencia_proyecto, gracia_proyecto, plazo_tipico

    with connection() as conn:
        r = plazo_tipico(conn, args.user, args.asunto)
        rows = conn.execute(
            text(
                """
                SELECT t.id, t.title, t.status,
                       (SELECT max(c.due_on) FROM mod_tareas c
                         WHERE c.parent_id = t.id AND c.status <> 'cancelada') AS entrega,
                       (SELECT count(*) FROM mod_tareas c
                         WHERE c.parent_id = t.id AND c.status <> 'cancelada'
                           AND c.due_on IS NULL) AS sin_fecha
                  FROM mod_tareas t
                 WHERE t.user_id = :u AND t.asunto_id = :a AND t.parent_id IS NULL
                   AND (t.metadata->>'is_parent')::boolean IS TRUE AND t.status <> 'cancelada'
                 ORDER BY t.id
                """
            ),
            {"u": args.user, "a": args.asunto},
        ).all()
        proyectos = []
        for pid, title, status, entrega, sin_fecha in rows:
            cad = cadencia_proyecto(conn, args.user, int(pid))
            gracia = gracia_proyecto(conn, args.user, int(pid), args.asunto)
            vence = (
                (entrega + timedelta(days=gracia)).isoformat()
                if isinstance(entrega, date) and not sin_fecha
                else None
            )
            proyectos.append(
                {
                    "id": int(pid),
                    "title": str(title),
                    "status": str(status),
                    "dias_con_pasos": cad.dias_con_pasos,
                    "cadencia_dias": cad.dias,
                    "gracia_dias": gracia,
                    "entrega": entrega.isoformat() if isinstance(entrega, date) else None,
                    "pasos_sin_fecha": int(sin_fecha or 0),
                    "vence_el": vence,
                }
            )
    result = {
        "asunto_id": args.asunto,
        "plazo_tipico_dias": r.plazo_dias,
        "plazo_muestras": r.muestras,
        "plazo_en_uso": r.plazo_o_default,
        "proyectos": proyectos,
    }
    if args.as_json:
        _emit_json(result)
        return 0
    plazo = (
        f"{r.plazo_dias} días (mediana de {r.muestras} plazos dichos)"
        if r.plazo_dias is not None
        else f"sin historia suficiente ({r.muestras} muestras): se usan {r.plazo_o_default} días"
    )
    _say(f"\nritmo del asunto {args.asunto}: una consigna sin plazo dura {plazo}")
    for p in proyectos:
        cad_txt = f"{p['cadencia_dias']} días" if p["cadencia_dias"] is not None else "—"
        vence_txt = str(
            p["vence_el"] or ("(pasos sin fecha)" if p["pasos_sin_fecha"] else "(sin pasos)")
        )
        _say(
            f"  #{p['id']} [{p['status']}] {p['title']!r}: pasos en {p['dias_con_pasos']} "
            f"día(s), cadencia {cad_txt}, entrega {p['entrega'] or '—'}, gracia "
            f"{p['gracia_dias']} días → vence {vence_txt}"
        )
    _say("")
    return 0


def _cmd_embed(args: argparse.Namespace) -> int:
    from sqlalchemy import text

    from memex.modules.tareas.module import SEMANTIC_SLUG
    from memex.semantic.embedder import get_embedder
    from memex.semantic.store import count_vectors, upsert_vectors

    emb = get_embedder()
    if emb is None:
        _say("bloqueo semántico apagado (MEMEX_SEMANTIC_PROVIDER=off) o sin proveedor.", err=True)
        return 1
    with connection() as conn:
        rows = conn.execute(
            text(
                "SELECT id, title FROM mod_tareas WHERE user_id = :u AND status <> 'cancelada' "
                "ORDER BY id"
            ),
            {"u": args.user},
        ).all()
        done = 0
        for i in range(0, len(rows), 256):
            done += upsert_vectors(
                conn,
                args.user,
                SEMANTIC_SLUG,
                [(int(r[0]), str(r[1])) for r in rows[i : i + 256]],
                embedder=emb,
            )
        total = count_vectors(conn, args.user, SEMANTIC_SLUG)
    result = {"model": emb.model, "tareas": len(rows), "embebidas": done, "vectores": total}
    if args.as_json:
        _emit_json(result)
        return 0
    _say(f"embed ({emb.model}): {len(rows)} tareas, {done} vectores calculados, {total} guardados.")
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    from memex.modules.tareas.module import merge_tareas

    with connection() as conn:
        changes = merge_tareas(conn, args.user, into=args.into, absorb=args.absorb)
    if args.as_json:
        _emit_json({"into": args.into, "absorb": args.absorb, "changes": changes})
        return 0
    _say(
        f"tarea #{args.absorb} fundida en #{args.into}"
        + (f"; se sumó: {', '.join(changes)}" if changes else "; sin novedades")
    )
    return 0


def _cmd_recordatorios(args: argparse.Namespace) -> int:
    from datetime import datetime

    from memex.core.localtime import LOCAL_TZ
    from memex.modules.tareas.recordatorios import (
        HORIZONTE_DIAS,
        as_json,
        materialize_calendar_reminders,
    )

    with connection() as conn:
        st = materialize_calendar_reminders(
            conn,
            args.user,
            today=datetime.now(LOCAL_TZ).date(),
            horizon_days=args.dias if args.dias is not None else HORIZONTE_DIAS,
            apply=bool(args.apply),
        )
    if args.as_json:
        print(as_json(st))
        return 0
    modo = "" if args.apply else " (dry-run: --apply para crear)"
    print(
        f"{st.considered} ocurrencia(s) de recordatorios en el horizonte; "
        f"{st.created} nueva(s), {st.existing} ya materializada(s){modo}"
    )
    for it in st.items:
        tag = f"#{it['tarea_id']}" if "tarea_id" in it else "nueva"
        print(f"  {it['occurrence']} <{it['category']}> {it['title']} ({tag})")
    return 0


def _cmd_recheck(args: argparse.Namespace) -> int:
    import asyncio
    from datetime import date as _date

    from memex.modules.tareas.extract import recheck_duplicates

    ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
    since = _date.fromisoformat(args.since) if args.since else None
    st = asyncio.run(
        recheck_duplicates(args.user, tarea_ids=ids, since=since, apply=not args.dry_run)
    )
    if args.as_json:
        _emit_json(
            {
                "revisadas": st.revisadas,
                "juzgadas": st.juzgadas,
                "fundidas": st.fundidas,
                "sin_juez": st.sin_juez,
                "pares": st.pares,
            }
        )
        return 0
    for par in st.pares:
        if par["into"] is not None:
            _say(f"  #{par['tarea_id']} «{par['title']}» es la misma que #{par['into']}")
    _say(
        f"{'(dry-run) ' if args.dry_run else ''}{st.revisadas} revisadas, {st.juzgadas} juzgadas, "
        f"{st.fundidas} fundidas" + (f", {st.sin_juez} sin juez" if st.sin_juez else "")
    )
    return 0


def _cmd_relink(args: argparse.Namespace) -> int:
    from memex.modules.tareas.module import relink_orphans

    with connection() as conn:
        st = relink_orphans(conn, args.user, apply=args.apply)
    mode = "APLICADO" if args.apply else "dry-run (agregá --apply)"
    _say(
        f"{mode}: {st.get('remerged', 0)} de asuntos fundidos re-apuntadas, "
        f"{st['orphans']} sueltas, {st['linked']} con asunto resuelto, "
        f"{st.get('sin_fecha', 0)} con asunto pero sin fecha, "
        f"{st['dated']} con fecha de próxima sesión"
    )
    return 0


def _cmd_dedup_parents(args: argparse.Namespace) -> int:
    from memex.modules.tareas.module import dedup_parents, flatten_single_parents

    with connection() as conn:
        merged = dedup_parents(conn, args.user, asunto_id=args.asunto, apply=args.apply)
        flat = (
            flatten_single_parents(conn, args.user, asunto_id=args.asunto, apply=args.apply)
            if args.flatten_singles
            else []
        )
    mode = "APLICADO" if args.apply else "dry-run (agregá --apply)"
    for m in merged:
        _say(
            f"asunto {m['asunto_id']}: «{m['absorbed_title']}» (#{m['absorbed']}, "
            f"{m['steps']} pasos) → «{m['survivor_title']}» (#{m['survivor']})"
        )
    for f in flat:
        _say(f"asunto {f['asunto_id']}: «{f['title']}» (#{f['parent']}) → paso #{f['step']} suelto")
    _say(
        f"{mode}: {len(merged)} proyecto(s) repetido(s) fundido(s), "
        f"{len(flat)} proyecto(s) de un paso aplanado(s)"
    )
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    with connection() as conn:
        ok = delete(conn, args.user, args.id)
    if args.as_json:
        _emit_json({"deleted": ok, "id": args.id})
    else:
        _say(f"tarea #{args.id} borrada")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.modules.tareas.cli")
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "help":
        _say(_HELP)
        return 0
    log.info("tareas.cli.start", cmd=args.cmd)
    try:
        if args.cmd == "add":
            return _cmd_add(args)
        if args.cmd == "list":
            return _cmd_list(args)
        if args.cmd == "show":
            return _cmd_show(args)
        if args.cmd == "done":
            return _cmd_status(args, STATUS_COMPLETADA)
        if args.cmd == "reopen":
            return _cmd_status(args, STATUS_PENDIENTE)
        if args.cmd == "cancel":
            return _cmd_status(args, STATUS_CANCELADA)
        if args.cmd == "update":
            return _cmd_update(args)
        if args.cmd == "relink":
            return _cmd_relink(args)
        if args.cmd == "dedup-parents":
            return _cmd_dedup_parents(args)
        if args.cmd == "merge":
            return _cmd_merge(args)
        if args.cmd == "recheck":
            return _cmd_recheck(args)
        if args.cmd == "expire":
            return _cmd_expire(args)
        if args.cmd == "ritmo":
            return _cmd_ritmo(args)
        if args.cmd == "recordatorios":
            return _cmd_recordatorios(args)
        if args.cmd == "embed":
            return _cmd_embed(args)
        if args.cmd == "discard":
            return _cmd_discard(args)
        if args.cmd == "link":
            return _cmd_link(args)
        if args.cmd == "rm":
            return _cmd_rm(args)
        log.error("tareas.cli.unknown_command", cmd=args.cmd)
        return 1
    except (TareaNotFoundError, AsuntoNotFoundError, ValueError) as e:
        if getattr(args, "as_json", False):
            _emit_json({"error": type(e).__name__, "message": str(e)})
        else:
            _say(str(e), err=True)
        return 1
    except Exception as e:
        log.exception("tareas.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
