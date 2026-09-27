"""CLI `memex-inspect` — inspección de solo lectura del pipeline, por mensaje y por corrida, más el
lanzador de la operación frecuente «procesá N mensajes y mostrame qué decidió».

Subcomandos:
  recent   — los N mensajes más nuevos con su estado (tier · veredicto · extraído · resolver ·
             fallas)
  message  — camino de decisión de UN mensaje en texto (lo mismo que /datos/:id): veredicto y razón,
             ítems extraídos con evidencia, menciones → identidad resuelta, decisiones del resolver,
             fallas, costo LLM por etapa
  run      — una corrida de /procesamiento: stats + una línea por mensaje (`--detail` expande
             cada uno)
  process  — lanza una corrida por el API (dry-run → run → espera) y muestra el resumen; con
             `--review` encadena `run --detail`. Es lo que antes se hacía a mano con curl + SQL.

`recent`/`message`/`run` leen Postgres directo (`memex.db.connection`), sin LLM. `process` habla con
el API (`MEMEX_BASE_URL`, default http://localhost:8787): el gate corre en el contenedor (codex vive
ahí), por eso no se procesa in-process desde el host. `--json` imprime el objeto como ÚLTIMA línea
(convención del agente: parsear la última línea de stdout).

Ejemplos:
  memex-inspect recent --n 10
  memex-inspect message 2509
  memex-inspect run 2 --detail
  memex-inspect process --newest 10 --review
  memex-inspect process --oldest 20 --source 1 --since 2026-08-01 \
      --stages classify,relevance,extract
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from typing import Any

import httpx
from sqlalchemy import text

from memex.db import connection
from memex.logging import get_logger, setup_logging

_log = get_logger("memex.inspect.cli")

DEFAULT_STAGES = ("classify", "relevance", "extract")
_LABEL_SQL = (
    "LEFT(COALESCE(NULLIF(i.payload->>'subject', ''), NULLIF(i.payload->>'text', ''), "
    "NULLIF(i.payload->>'body_text', ''), ''), 90)"
)
_FROM_SQL = (
    "COALESCE(i.payload->'from'->>'email', i.payload->'from_'->>'email', i.payload->>'from', '')"
)


# ----------------------------------------------------------------------------- consultas


def fetch_recent(user_id: int, n: int, source_id: int | None = None) -> list[dict[str, Any]]:
    """Los N mensajes más nuevos (occurred_at DESC) con su estado de pipeline en una fila."""
    where = "i.user_id = :uid" + (" AND i.source_id = :sid" if source_id is not None else "")
    sql = f"""
        SELECT i.id, s.name AS source, i.occurred_at, {_FROM_SQL} AS sender, {_LABEL_SQL} AS label,
               c.tier, v.verdict, v.method,
               EXISTS (SELECT 1 FROM module_extractions me WHERE me.inbox_id = i.id) AS extracted,
               EXISTS (SELECT 1 FROM llm_calls lc WHERE lc.inbox_id = i.id
                       AND lc.purpose = 'identidades_resolve') AS resolved,
               (SELECT count(*) FROM work_item_failures f WHERE f.inbox_id = i.id) AS failures
        FROM inbox i
        JOIN sources s ON s.id = i.source_id
        LEFT JOIN classifications c ON c.inbox_id = i.id
        LEFT JOIN relevance_verdicts v ON v.inbox_id = i.id
        WHERE {where}
        ORDER BY i.occurred_at DESC, i.id DESC
        LIMIT :n
    """
    params: dict[str, Any] = {"uid": user_id, "n": n}
    if source_id is not None:
        params["sid"] = source_id
    with connection() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]


def _parse_json(raw: Any) -> Any:
    if isinstance(raw, dict | list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return None


def message_report(user_id: int, inbox_id: int) -> dict[str, Any] | None:
    """Todo lo que el sistema decidió sobre UN mensaje, en un dict (None si no existe)."""
    from memex.modules.orchestrator import read_extractions

    with connection() as conn:
        head = (
            conn.execute(
                text(
                    f"""
                    SELECT i.id, s.name AS source, s.type AS source_type, i.occurred_at,
                           i.received_at, {_FROM_SQL} AS sender, {_LABEL_SQL} AS label,
                           c.tier, c.metadata AS tier_meta,
                           i.code_version AS ver_ingested, c.code_version AS ver_classified,
                           (SELECT e.code_version FROM module_extractions e WHERE e.inbox_id = i.id
                             ORDER BY e.created_at DESC NULLS LAST LIMIT 1) AS ver_extracted
                    FROM inbox i
                    JOIN sources s ON s.id = i.source_id
                    LEFT JOIN classifications c ON c.inbox_id = i.id
                    WHERE i.id = :id AND i.user_id = :uid
                    """
                ),
                {"id": inbox_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
        if head is None:
            return None
        verdict = (
            conn.execute(
                text(
                    """
                    SELECT v.verdict, v.method, v.reason, v.model, v.mode, v.created_at,
                           v.code_version AS ver,
                           r.effect AS rule_effect, r.sender_value AS rule_sender,
                           r.pattern AS rule_pattern
                    FROM relevance_verdicts v
                    LEFT JOIN relevance_gate_rules r ON r.id = v.rule_id
                    WHERE v.inbox_id = :id
                    """
                ),
                {"id": inbox_id},
            )
            .mappings()
            .first()
        )
        mark = (
            conn.execute(
                text("SELECT is_relevant, reason FROM relevance_marks WHERE inbox_id = :id"),
                {"id": inbox_id},
            )
            .mappings()
            .first()
        )
        mentions = (
            conn.execute(
                text(
                    """
                    SELECT m.mentioned_name, m.mentioned_kind, m.resolution_method,
                           i.id AS identity_id, i.kind, i.display_name,
                           (SELECT display_name FROM mod_identidades p
                             WHERE p.id = i.parent_identity_id) AS parent
                    FROM mod_identidades_mentions m
                    LEFT JOIN mod_identidades i ON i.id = m.resolved_identity_id
                    WHERE m.user_id = :uid AND m.source_inbox_ids @> ARRAY[CAST(:id AS BIGINT)]
                    ORDER BY m.id
                    """
                ),
                {"id": inbox_id, "uid": user_id},
            )
            .mappings()
            .all()
        )
        calls = (
            conn.execute(
                text(
                    """
                    SELECT purpose, model, cost_usd, status, created_at, metadata, response_text
                    FROM llm_calls WHERE user_id = :uid AND inbox_id = :id ORDER BY created_at
                    """
                ),
                {"id": inbox_id, "uid": user_id},
            )
            .mappings()
            .all()
        )
        failures = (
            conn.execute(
                text(
                    "SELECT stage, status, attempts, last_error, updated_at "
                    "FROM work_item_failures WHERE user_id = :uid AND inbox_id = :id "
                    "ORDER BY updated_at DESC"
                ),
                {"id": inbox_id, "uid": user_id},
            )
            .mappings()
            .all()
        )
        trace_nodes = conn.execute(
            text("SELECT count(*) FROM trace_nodes WHERE inbox_id = :id"), {"id": inbox_id}
        ).scalar()

    cost: dict[str, float] = {}
    for c in calls:
        cost[c["purpose"]] = cost.get(c["purpose"], 0.0) + float(c["cost_usd"] or 0)
    resolver = [
        {
            "created_at": c["created_at"],
            "metadata": _parse_json(c["metadata"]),
            "decision": _parse_json(c["response_text"]),
        }
        for c in calls
        if c["purpose"] == "identidades_resolve"
    ]
    return {
        "head": dict(head),
        "verdict": dict(verdict) if verdict else None,
        "mark": dict(mark) if mark else None,
        "extraction": read_extractions(user_id, inbox_id),
        "mentions": [dict(m) for m in mentions],
        "resolver": resolver,
        "cost_by_purpose": cost,
        "failures": [dict(f) for f in failures],
        "trace_nodes": int(trace_nodes or 0),
    }


def run_report(user_id: int, run_id: int) -> dict[str, Any] | None:
    """Una corrida de /procesamiento (worker_runs) + el estado de cada mensaje objetivo."""
    with connection() as conn:
        run = (
            conn.execute(
                text(
                    "SELECT id, status, stats, error, started_at, finished_at, run_config "
                    "FROM worker_runs WHERE id = :id AND user_id = :uid AND run_type = 'reprocess'"
                ),
                {"id": run_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
    if run is None:
        return None
    cfg = _parse_json(run["run_config"]) or {}
    targets = [int(t) for t in (cfg.get("targets") or [])]
    rows: list[dict[str, Any]] = []
    if targets:
        with connection() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    text(
                        f"""
                        SELECT i.id, s.name AS source, {_FROM_SQL} AS sender, {_LABEL_SQL} AS label,
                               c.tier, v.verdict, v.method,
                               (SELECT coalesce(sum(item_count), 0) FROM module_extractions me
                                 WHERE me.inbox_id = i.id) AS items,
                               EXISTS (SELECT 1 FROM llm_calls lc WHERE lc.inbox_id = i.id
                                       AND lc.purpose = 'identidades_resolve') AS resolved,
                               (SELECT count(*) FROM work_item_failures f
                                 WHERE f.inbox_id = i.id) AS failures
                        FROM inbox i
                        JOIN sources s ON s.id = i.source_id
                        LEFT JOIN classifications c ON c.inbox_id = i.id
                        LEFT JOIN relevance_verdicts v ON v.inbox_id = i.id
                        WHERE i.id = ANY(:ids)
                        """
                    ),
                    {"ids": targets},
                )
                .mappings()
                .all()
            ]
    by_id = {r["id"]: r for r in rows}
    return {
        "run": {k: run[k] for k in ("id", "status", "error", "started_at", "finished_at")},
        "stats": _parse_json(run["stats"]) or {},
        "config": cfg,
        "targets": [by_id[t] for t in targets if t in by_id],
    }


# ----------------------------------------------------------------------------- render


def _s(x: Any, n: int = 60) -> str:
    return str(x if x is not None else "")[:n]


def _cost(v: float) -> str:
    return f"${v:.4f}"


def _local(dt: Any) -> Any:
    """Timestamps tz-aware de Postgres → hora local de la máquina (el dueño lee en Bogotá)."""
    return dt.astimezone() if hasattr(dt, "astimezone") else dt


def render_recent(rows: list[dict[str, Any]]) -> str:
    out = [
        "  id   fuente           fecha            tier       veredicto     ext res fal  "
        "remitente → asunto"
    ]
    for r in rows:
        ext = "sí" if r["extracted"] else "—"
        res = "sí" if r["resolved"] else "—"
        flags = f"{ext:>3} {res:>3} {r['failures'] or '—':>3}"
        out.append(
            f"{r['id']:>5}  {_s(r['source'], 15):<15}  {_local(r['occurred_at']):%m-%d %H:%M}  "
            f"{_s(r['tier'] or '—', 9):<9}  {_s(r['verdict'] or '—', 12):<12}  {flags}  "
            f"{_s(r['sender'], 32)} → {_s(r['label'], 60)}"
        )
    return "\n".join(out)


def render_message(rep: dict[str, Any]) -> str:
    h = rep["head"]
    lines = [
        f"#{h['id']} · {h['source']} · {_local(h['occurred_at']):%Y-%m-%d %H:%M} · {h['sender']}",
        f"  {h['label']}",
        f"  tier: {h['tier'] or 'sin clasificar'}"
        + (f" ({(h['tier_meta'] or {}).get('rule')})" if h.get("tier_meta") else ""),
    ]
    v, m = rep["verdict"], rep["mark"]
    # Sello de versión por etapa (memex.version): «—» = etapa no hecha o fila anterior al sello.
    lines.append(
        "  versión del código: "
        f"ingesta {h.get('ver_ingested') or '—'} · tier {h.get('ver_classified') or '—'} · "
        f"gate {(v or {}).get('ver') or '—'} · extracción {h.get('ver_extracted') or '—'}"
    )
    if m:
        lines.append(
            f"  relevancia: MANUAL · {'relevante' if m['is_relevant'] else 'no relevante'}"
        )
    if v:
        rule = ""
        if v["rule_effect"]:
            rule = f" · regla {v['rule_effect']}:{v['rule_sender'] or ''}" + (
                f" ~{v['rule_pattern']}" if v["rule_pattern"] else ""
            )
        lines.append(f"  gate: {v['verdict']} · {v['method']}{rule} · {v['model'] or ''}")
        if v["reason"]:
            lines.append(f"    «{v['reason']}»")
    elif not m:
        lines.append("  gate: sin veredicto")
    ext = rep["extraction"]
    if ext.get("done"):
        lines.append(f"  extracción: hecha · módulos {', '.join(ext.get('modules') or []) or '—'}")
        for f in ext.get("finance", []):
            lines.append(
                f"    finance: {_s(f.get('direction'))} {_s(f.get('amount'))} "
                f"{_s(f.get('currency'))} · {_s(f.get('counterparty') or f.get('place'))}"
                f" | «{_s(f.get('evidence'), 70)}»"
            )
        for c in ext.get("calendar", []):
            lines.append(
                f"    calendar: {_s(c.get('title'), 50)} · {_s(c.get('starts_on'))}"
                f" {_s(c.get('start_time') or '')} | «{_s(c.get('evidence'), 70)}»"
            )
        for hk in ext.get("hackathones", []):
            lines.append(f"    hackathon: {_s(hk.get('name'), 50)} · {_s(hk.get('starts_on'))}")
    else:
        lines.append("  extracción: pendiente")
    if rep["mentions"]:
        lines.append("  identidades (mención → resuelta):")
        for mn in rep["mentions"]:
            target = (
                f"#{mn['identity_id']} {mn['kind']} «{_s(mn['display_name'], 45)}»"
                + (f" ⊂ {_s(mn['parent'], 30)}" if mn["parent"] else "")
                if mn["identity_id"]
                else "SIN RESOLVER"
            )
            lines.append(
                f"    {_s(mn['mentioned_name'], 40)} [{mn['resolution_method']}] → {target}"
            )
    for r in rep["resolver"]:
        d = r["decision"] or {}
        lines.append(
            f"  resolver: fusiones={len(d.get('merges') or [])} "
            f"relaciones={len(d.get('relations') or [])} remitente="
            + (json.dumps(d.get("sender"), ensure_ascii=False) if d.get("sender") else "—")
        )
        for mg in d.get("merges") or []:
            lines.append(
                f"    fusión: #{mg.get('drop_id')} → #{mg.get('keep_id')} ({mg.get('confidence')})"
            )
        for rl in d.get("relations") or []:
            lines.append(
                f"    relación: #{rl.get('source_id')} → "
                f"#{rl.get('target_id') or rl.get('target_name')}"
                f" rol={rl.get('role') or '—'} ({rl.get('confidence')})"
            )
    for f in rep["failures"]:
        lines.append(
            f"  FALLA {f['stage']} · {f['status']} · {f['attempts']} intento(s) · "
            f"{_s(f['last_error'], 110)}"
        )
    if rep["cost_by_purpose"]:
        total = sum(rep["cost_by_purpose"].values())
        parts = ", ".join(f"{k} {_cost(c)}" for k, c in rep["cost_by_purpose"].items())
        lines.append(f"  costo LLM: {_cost(total)} ({parts})")
    lines.append(f"  traza: {rep['trace_nodes']} nodos")
    return "\n".join(lines)


def render_run(rep: dict[str, Any], detail: bool, user_id: int) -> str:
    run, st = rep["run"], rep["stats"]
    lines = [
        f"corrida #{run['id']} · {run['status']} · {run['started_at']:%Y-%m-%d %H:%M} → "
        f"{run['finished_at']:%H:%M:%S}"
        if run["finished_at"]
        else f"corrida #{run['id']} · {run['status']}",
        f"  etapas: {', '.join(rep['config'].get('stages') or [])} · filtros: "
        f"{json.dumps(rep['config'].get('filters'), ensure_ascii=False)} · "
        f"costo {_cost(float(st.get('cost_usd') or 0))}",
    ]
    for stage, res in (st.get("results") or {}).items():
        lines.append(f"  {stage}: {json.dumps(res, ensure_ascii=False)}")
    if run["error"]:
        lines.append(f"  ERROR: {run['error']}")
    lines.append("")
    for r in rep["targets"]:
        lines.append(
            f"  #{r['id']} {_s(r['source'], 14):<14} {_s(r['tier'] or '—', 9):<9} "
            f"{_s(r['verdict'] or '—', 12):<12} ítems={r['items']:<3} "
            f"resolver={'sí' if r['resolved'] else '—'} fallas={r['failures']} · "
            f"{_s(r['sender'], 30)} → {_s(r['label'], 55)}"
        )
        if detail:
            mr = message_report(user_id, int(r["id"]))
            if mr:
                lines.append("    " + render_message(mr).replace("\n", "\n    "))
                lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- process (API)


def _base_url() -> str:
    return os.environ.get("MEMEX_BASE_URL", "http://localhost:8787").rstrip("/")


def process(args: argparse.Namespace) -> int:
    n = args.newest or args.oldest
    body: dict[str, Any] = {
        "stages": [s.strip() for s in args.stages.split(",") if s.strip()],
        "limit": n,
        "newest_first": bool(args.newest),
        "force": bool(args.force),
    }
    if args.source is not None:
        body["source_id"] = args.source
    if args.since:
        body["since"] = args.since
    if args.until:
        body["until"] = args.until
    with httpx.Client(base_url=_base_url(), timeout=60.0) as api:
        dry = api.post("/processing/dry-run", json=body)
        dry.raise_for_status()
        sel = dry.json()
        print(f"objetivos: {sel['count']} · ids {sel['sample_ids']} · etapas {sel['stages']}")
        if args.dry_run or sel["count"] == 0:
            return 0
        run = api.post("/processing/run", json=body)
        run.raise_for_status()
        rid = int(run.json()["run_id"])
        _log.info("inspect.process.started", run_id=rid, count=sel["count"])
        deadline = time.monotonic() + args.timeout
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status = api.get(f"/processing/runs/{rid}").json()
            if status.get("status") != "running":
                break
            time.sleep(3)
    if status.get("status") == "running":
        print(
            f"corrida #{rid} sigue corriendo tras {args.timeout}s; "
            f"revisala con: memex-inspect run {rid}"
        )
        return 2
    rep = run_report(args.user, rid)
    if rep is None:
        print(f"corrida #{rid} no encontrada", file=sys.stderr)
        return 1
    print(render_run(rep, detail=args.review, user_id=args.user))
    return 0 if status.get("status") == "ok" else 1


# ----------------------------------------------------------------------------- refetch


def _empty_body_ids(user_id: int, source_id: int | None) -> list[int]:
    """Mensajes IMAP con `body_text` vacío: los candidatos naturales a re-traer tras un fix del
    parser (p.ej. el text/plain vacío delante del real, 2026-09-04)."""
    with connection() as conn:
        rows = conn.execute(
            text(
                """
                SELECT i.id FROM inbox i JOIN sources s ON s.id = i.source_id
                WHERE i.user_id = :uid AND s.type = 'imap'
                  AND coalesce(length(i.payload->>'body_text'), 0) = 0
                  AND (CAST(:sid AS BIGINT) IS NULL OR i.source_id = :sid)
                ORDER BY i.id
                """
            ),
            {"uid": user_id, "sid": source_id},
        ).all()
    return [int(r[0]) for r in rows]


def refetch(args: argparse.Namespace) -> int:
    ids: list[int] = list(args.inbox_ids)
    if args.empty:
        ids.extend(_empty_body_ids(args.user, args.source))
    ids = list(dict.fromkeys(ids))
    if not ids:
        print("nada que re-traer")
        return 0
    shown = ", ".join(str(i) for i in ids[:20]) + (" …" if len(ids) > 20 else "")
    print(f"objetivos: {len(ids)} · ids {shown}")
    if args.dry_run:
        return 0
    out: list[dict[str, Any]] = []
    failures = 0
    with httpx.Client(base_url=_base_url(), timeout=120.0) as api:
        for iid in ids:
            r = api.post(f"/inbox/{iid}/refetch")
            if r.status_code >= 400:
                failures += 1
                print(f"  #{iid} ERROR {r.status_code}: {r.text[:140]}")
                continue
            d = r.json()
            out.append(d)
            print(
                f"  #{iid} cuerpo {d['body_before']} → {d['body_after']} chars"
                f" ({d.get('body_source') or '—'})"
            )
    print(f"re-traídos {len(out)} · fallas {failures}")
    if args.json:
        _emit_json(out)
    return 1 if failures else 0


# ----------------------------------------------------------------------------- main


def _emit_json(obj: Any) -> None:
    print(json.dumps(obj, default=str, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    # Consola Windows en cp1252: los «→ · ⊂» del render reventarían con UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="memex-inspect", description=__doc__.split("\n\n")[0])
    p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    p.add_argument("--json", action="store_true", help="Imprime el objeto JSON como última línea.")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("recent", help="Los N mensajes más nuevos con su estado.")
    r.add_argument("--n", type=int, default=10)
    r.add_argument("--source", type=int, default=None, help="Acotar a una fuente (id).")

    m = sub.add_parser("message", help="Camino de decisión de un mensaje.")
    m.add_argument("inbox_id", type=int)

    ru = sub.add_parser("run", help="Resumen de una corrida de /procesamiento.")
    ru.add_argument("run_id", type=int)
    ru.add_argument("--detail", action="store_true", help="Expande cada mensaje objetivo.")

    pr = sub.add_parser(
        "process", help="Lanza una corrida por el API y espera el resultado (PAGA LLM)."
    )
    g = pr.add_mutually_exclusive_group(required=True)
    g.add_argument("--newest", type=int, help="Los N más nuevos por fecha.")
    g.add_argument("--oldest", type=int, help="Los N más viejos pendientes (orden de ingesta).")
    pr.add_argument("--source", type=int, default=None)
    pr.add_argument("--since", default=None, help="YYYY-MM-DD (inclusive).")
    pr.add_argument("--until", default=None, help="YYYY-MM-DD (exclusivo).")
    pr.add_argument("--stages", default=",".join(DEFAULT_STAGES))
    pr.add_argument("--force", action="store_true", help="Re-hace lo ya procesado.")
    pr.add_argument("--dry-run", action="store_true", help="Solo muestra la selección.")
    pr.add_argument("--review", action="store_true", help="Al terminar, imprime run --detail.")
    pr.add_argument("--timeout", type=int, default=900, help="Segundos máximos de espera.")

    rf = sub.add_parser(
        "refetch",
        help="Vuelve a traer mensajes desde su fuente (IMAP por UID) y refresca el payload.",
    )
    rf.add_argument("inbox_ids", type=int, nargs="*", help="Ids de inbox a re-traer.")
    rf.add_argument(
        "--empty", action="store_true", help="Agrega todos los mensajes IMAP con cuerpo vacío."
    )
    rf.add_argument("--source", type=int, default=None, help="Con --empty: acotar a una fuente.")
    rf.add_argument("--dry-run", action="store_true", help="Solo muestra la selección.")

    args = p.parse_args(argv)
    if args.cmd == "recent":
        rows = fetch_recent(args.user, args.n, args.source)
        print(render_recent(rows))
        if args.json:
            _emit_json(rows)
        return 0
    if args.cmd == "message":
        rep = message_report(args.user, args.inbox_id)
        if rep is None:
            print(f"no existe el inbox #{args.inbox_id}", file=sys.stderr)
            return 1
        print(render_message(rep))
        if args.json:
            _emit_json(rep)
        return 0
    if args.cmd == "run":
        rep = run_report(args.user, args.run_id)
        if rep is None:
            print(f"no existe la corrida #{args.run_id}", file=sys.stderr)
            return 1
        print(render_run(rep, detail=args.detail, user_id=args.user))
        if args.json:
            _emit_json(rep)
        return 0
    if args.cmd == "process":
        return process(args)
    if args.cmd == "refetch":
        return refetch(args)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
