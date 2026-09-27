"""Curador de asuntos (codex, $0): salud del catálogo que el asignador no puede garantizar solo.

El asignador decide lote a lote con contexto acotado; el curador mira el CONJUNTO y arregla:
- duplicados (dos asuntos del mismo hilo de vida, p. ej. nombrados distinto en lotes distintos) →
  `merge` (los alias se conservan);
- nombres nacidos del primer mensaje («GIVE Colombia» para lo que luego fue Impact Studio) →
  `rename` (el nombre anterior queda como alias);
- asuntos que evidentemente terminaron → `close` con el estado final; reabrir si volvió señal;
- candidatos viejos con una sola evidencia → `delete` (ruido) o `keep` (espera);
- relaciones faltantes (`requisito_de` / `parte_de` / `continua`) → `set_relation`;
- tipo mal puesto → `set_kind`.
Reglas del sistema (sin LLM, `run_system_rules`): promoción de candidatos que ya juntaron
evidencia e inactividad (sin señal N días → `inactivo`). Todo pasa por `store`; cada decisión en
su SAVEPOINT (una mala no tumba la tanda); lo que el aplicador no contempla se registra con razón.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.observability import record_llm_call
from memex.db import connection
from memex.logging import get_logger
from memex.modules.asuntos import finance_link, store
from memex.modules.asuntos.settings import get_settings
from memex.webcontext._codex_subprocess import CodexRun, run_codex

_log = get_logger("memex.modules.asuntos.curator")

_PURPOSE = "asuntos_curate"
_DEFAULT_TIMEOUT_S = 600.0
_DEFAULT_BATCH = 40
SCOPES = ("duplicates", "candidates", "open", "relations", "names", "evidence", "all")
ACTIONS = frozenset(
    {
        "merge",
        "rename",
        "close",
        "reopen",
        "delete",
        "keep",
        "set_relation",
        "reject_relation",
        "reject_evidence",
        "set_kind",
        "skip",
    }
)
_CANDIDATE_MIN_AGE_DAYS = 14
_TOKEN_RX = re.compile(r"[^\w]+", re.U)
_STOP = frozenset(
    {
        "de",
        "del",
        "la",
        "el",
        "los",
        "las",
        "y",
        "en",
        "para",
        "por",
        "con",
        "a",
        "the",
        "of",
        "and",
        "for",
        "to",
        "in",
        "on",
        "2026",
        "2025",
        "un",
        "una",
    }
)


# --- ítems ---------------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Item:
    key: str  # "asunto:<id>" | "pair:<a>-<b>" | "rel:<id>" | "name:<id>" | "ev:<id>"
    asunto_ids: tuple[int, ...]
    reason: str
    extra: str = ""  #: bloque adicional del prompt (p. ej. el mensaje de una evidencia dudosa)


#: Largo mínimo de un token para contar como «distintivo» al emparejar candidatos (evita que
#: «curso», «fest» o «2026» junten cualquier cosa).
_DISTINCTIVE_TOKEN_LEN = 7


def _distinctive(tokens: frozenset[str]) -> set[str]:
    """Tokens que identifican un hilo: largos y no numéricos («bootcamp», no «curso» ni «2026»)."""
    return {t for t in tokens if len(t) >= _DISTINCTIVE_TOKEN_LEN and not t.isdigit()}


def _tokens(name: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN_RX.split(name.lower()) if t and t not in _STOP)


def _pairs(asuntos: Sequence[store.Asunto]) -> list[Item]:
    """Pares sospechosos de duplicado: nombres con Jaccard de tokens ≥ 0.5, alias iguales o ≥2
    entidades en común (solo entre asuntos vivos o cerrados recientes; los fundidos no entran)."""
    out: list[Item] = []
    toks = {a.id: _tokens(a.name).union(*(_tokens(x) for x in a.aliases)) for a in asuntos}
    names = {
        a.id: {store.normalize_name(a.name), *(store.normalize_name(x) for x in a.aliases)}
        for a in asuntos
    }
    ents = {a.id: {e.lower() for e in a.entities} for a in asuntos}
    seen: set[tuple[int, int]] = set()
    for i, a in enumerate(asuntos):
        for b in asuntos[i + 1 :]:
            if a.kind != b.kind and "servicio" in (a.kind, b.kind):
                continue
            key = (min(a.id, b.id), max(a.id, b.id))
            if key in seen:
                continue
            ta, tb = toks[a.id], toks[b.id]
            jac = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
            reason = ""
            if names[a.id] & names[b.id]:
                reason = "mismo nombre/alias"
            elif jac >= 0.5:
                reason = f"nombres parecidos (jaccard {jac:.2f})"
            elif len(ents[a.id] & ents[b.id]) >= 2 and a.kind == b.kind:
                reason = f"entidades en común: {', '.join(sorted(ents[a.id] & ents[b.id])[:3])}"
            elif len(_distinctive(ta & tb)) >= 2:
                # Dos tokens distintivos en común («impulsa bootcamp» en «Impulsa Bootcamp 2026» y
                # «Impulsa Bootcamp Satélite Bogotá S03») señalan el mismo programa visto desde otro
                # lote aunque el Jaccard quede bajo por los tokens extra: que lo juzgue el curador.
                reason = f"tokens en común: {', '.join(sorted(_distinctive(ta & tb))[:3])}"
            elif "candidato" in (a.status, b.status):
                # Un candidato recién nacido («Curso Blockchain 2026-1») que comparte un token
                # distintivo con un asunto vivo o cerrado («Sesiones Blockchain») puede ser el mismo
                # hilo visto desde otro lote: que lo juzgue el curador. Solo candidatos: acota.
                shared = _distinctive(ta & tb)
                if shared:
                    reason = f"candidato con token compartido: {', '.join(sorted(shared)[:3])}"
            if reason:
                seen.add(key)
                out.append(Item(f"pair:{key[0]}-{key[1]}", key, reason))
    return out


def collect_items(
    conn: Connection, user_id: int, *, scope: str, limit: int, inactive_days: int
) -> list[Item]:
    if scope not in SCOPES:
        raise ValueError(f"scope inválido: {scope}")
    live = store.list_asuntos(
        conn, user_id, statuses=("candidato", "abierto", "inactivo", "cerrado"), limit=5000
    )
    now = datetime.now(UTC)
    items: list[Item] = []
    if scope in ("duplicates", "all"):
        items += _pairs(
            [
                a
                for a in live
                if a.status != "cerrado" or (a.closed_at and (now - a.closed_at).days <= 60)
            ]
        )
    if scope in ("candidates", "all"):
        for a in live:
            if (
                a.status == "candidato"
                and a.last_signal_at
                and (now - a.last_signal_at).days >= _CANDIDATE_MIN_AGE_DAYS
            ):
                items.append(Item(f"asunto:{a.id}", (a.id,), "candidato viejo con poca evidencia"))
    if scope in ("open", "all"):
        # abiertos sin señal reciente e INACTIVOS: ¿terminó (cerrar con estado final) o sigue?
        inactive_pending = set(store.unreviewed_inactive(conn, user_id))
        for a in live:
            if (
                a.status == "abierto"
                and a.last_signal_at
                and (now - a.last_signal_at).days >= inactive_days // 2
                and not (a.manual_status_at and a.manual_status_at > a.last_signal_at)
            ):
                items.append(
                    Item(f"asunto:{a.id}", (a.id,), "abierto sin señal reciente: ¿terminó?")
                )
            elif a.status == "inactivo" and a.id in inactive_pending:
                items.append(
                    Item(f"asunto:{a.id}", (a.id,), "inactivo: ¿terminó (close) o sigue (keep)?")
                )
    if scope in ("names", "all"):
        for a in store.unreviewed_names(conn, user_id, limit=limit):
            items.append(
                Item(f"name:{a.id}", (a.id,), "revisar nombre y tipo (rename / set_kind / keep)")
            )
    if scope in ("evidence", "all"):
        for ev in store.suspicious_evidence(conn, user_id, limit=limit):
            when = ev.occurred_at.date().isoformat() if ev.occurred_at else "?"
            items.append(
                Item(
                    f"ev:{ev.id}",
                    (ev.asunto_id,),
                    f"evidencia dudosa: {ev.reason} (keep / reject_evidence)",
                    extra=(
                        f"MENSAJE #{ev.inbox_id} · {when} · de: {ev.sender}"
                        f"{' · lista de correo' if ev.is_list else ''}\n"
                        f"  Asunto: {ev.subject}\n  {ev.snippet}"
                    ),
                )
            )
    if scope in ("relations", "all"):
        # 1) relaciones que propuso el asignador: el curador las valida (keep) o las rechaza.
        for rid, s_id, d_id, rtype in store.llm_relations(conn, user_id):
            items.append(
                Item(
                    f"rel:{rid}",
                    (s_id, d_id),
                    f"validar relación propuesta por el asignador: A{s_id} {rtype} A{d_id}",
                )
            )
        # 2) pase HOLÍSTICO: cada asunto con cuerpo, no revisado desde su última señal, contra el
        #    ÍNDICE completo del catálogo (el prompt lo incluye una vez): ¿es requisito de, parte
        #    de o continuación de algún otro? El modelo relaciona con CUALQUIER id del índice.
        for a in store.unreviewed_relations(conn, user_id, limit=limit):
            items.append(
                Item(
                    f"relq:{a.id}",
                    (a.id,),
                    "¿relaciones con otros asuntos del índice? (set_relation / keep)",
                )
            )
    uniq: dict[str, Item] = {}
    for it in items:
        uniq.setdefault(it.key, it)
    return list(uniq.values())[:limit]


# --- prompt --------------------------------------------------------------------------------- #

_INSTRUCCIONES = """Eres el curador del catálogo de ASUNTOS de una memoria personal. Un asunto es
un hilo de vida (evento, programa, hackathon, trámite, proyecto, comunidad, servicio) que un
asignador incremental infiere de los mensajes del usuario. Tu trabajo es la salud del CONJUNTO:
fundir duplicados, corregir nombres, cerrar lo terminado, borrar ruido, enlazar asuntos
relacionados y corregir tipos.
Puedes verificar en la web fechas y naturaleza de programas/eventos públicos; nunca busques
personas.

Reglas:
- merge SOLO si ambos son el MISMO hilo de vida (mismo programa/evento/trámite), aunque tengan
  nombres distintos. Un hackathon y la aceleradora que le sigue NO se funden (set_relation
  `continua`). Ediciones distintas de un mismo evento anual NO se funden (`continua`). Pagos
  fallidos de un mismo servicio en meses distintos SÍ se funden (uno por servicio). Citas o
  sesiones de una MISMA relación de servicio (mismo prestador/profesional: terapia, consultas,
  tutorías, controles) SÍ se funden en UN asunto —cada cita es un hito— y se renombra sin fecha
  («Citas de salud mental en <lugar>»). Al fundir, el
  superviviente es el de más evidencia o el nombre más reconocible; los alias se conservan solos.
- rename cuando el nombre nació de un mensaje temprano y el hilo se conoce por otro nombre; el
  nombre debe identificar el asunto sin contexto (incluye año/edición si aplica).
- close cuando el asunto evidentemente terminó (programa completado, evento pasado, trámite
  resuelto): escribe el ESTADO FINAL en una frase. reopen si un cerrado volvió a tener señal,
  EXCEPTO si está [SILENCIADO]: ese lo cerró el dueño a propósito y no se reabre (responde skip).
- delete solo candidatos/ruido (una evidencia irrelevante, newsletter, recibo suelto). keep si el
  candidato puede crecer.
- set_relation: `parte_de` (src parte de dst), `requisito_de` (src requisito para dst), `continua`
  (src continúa en dst). Solo si es claro. Para un ítem `rel:<id>` (relación que propuso el
  asignador) responde keep si es correcta o reject_relation si es falsa o forzada (afinidad
  temática, misma institución o mismo remitente NO son relación).
- Ítems `name:<id>`: ¿el nombre identifica el hilo sin contexto (qué + quién/dónde + edición o
  año si aplica) y el tipo es correcto? Si no, rename (sin «v18.0», sufijos de correo ni «RE:»;
  un meetup recurrente se nombra por la serie, no por una edición) o set_kind; si sí, keep.
- Ítems `ev:<id>` (una evidencia dudosa con su mensaje): keep si el mensaje pertenece al hilo del
  asunto; reject_evidence si es newsletter/genérico/de otro asunto. No toques el asunto.
- Ítems `relq:<id>`: mira el ÍNDICE y decide si este asunto es requisito_de / parte_de /
  continua de OTRO asunto del índice (o al revés). Usa set_relation con asunto_id = src y
  other_id = dst (cualquier id del índice; uno de los dos debe ser el del ítem); puedes emitir
  varias decisiones para el mismo ítem. Solo con evidencia explícita (el hilo lo dice: «requisito
  de grado», «fast-track al accelerator», «la prueba de admisión del programa») o conocimiento
  público verificable; la afinidad temática o la misma institución NO son relación. Si no hay
  ninguna, keep.
- confianza en [0,1]; bajo 0.7 mejor skip con la razón.
Responde SOLO el JSON del esquema.
"""


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["decisions"],
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "item",
                        "action",
                        "confidence",
                        "rationale",
                        "asunto_id",
                        "other_id",
                        "name",
                        "state",
                        "relation_type",
                        "kind",
                    ],
                    "properties": {
                        "item": {"type": "string"},
                        "action": {"type": "string", "enum": sorted(ACTIONS)},
                        "confidence": {"type": "number"},
                        "rationale": {"type": "string"},
                        "asunto_id": {"type": ["integer", "null"]},
                        "other_id": {"type": ["integer", "null"]},
                        "name": {"type": ["string", "null"]},
                        "state": {"type": ["string", "null"]},
                        "relation_type": {"type": ["string", "null"]},
                        "kind": {"type": ["string", "null"]},
                    },
                },
            }
        },
    }


def _render_asunto(conn: Connection, user_id: int, a: store.Asunto) -> str:
    ev = store.evidence_of(conn, user_id, a.id)
    sample = "; ".join(
        f"[{e.occurred_at:%Y-%m-%d}] {e.sender[:30]}: {e.subject[:70]}"
        for e in ev[:5]
        if e.occurred_at
    )
    hitos = "; ".join(f"{e.hito_date or '?'} {e.hito_text}" for e in ev if e.hito_text)[:400]
    rels = "; ".join(
        f"{r.relation_type} {'→' if r.direction == 'out' else '←'} A{r.other_id} {r.other_name}"
        for r in store.relations_of(conn, user_id, a.id)
    )
    first = a.first_signal_at.date().isoformat() if a.first_signal_at else "?"
    last = a.last_signal_at.date().isoformat() if a.last_signal_at else "?"
    return (
        f"A{a.id} · {a.name} · tipo={a.kind} · estado_ciclo={a.status} · alias={list(a.aliases)}\n"
        f"  descripción: {a.description[:200]}\n  entidades: {', '.join(a.entities[:8])}\n"
        f"  evidencia: {a.evidence_count} mensajes, {first} → {last}\n  muestra: {sample}\n"
        f"  hitos: {hitos or '—'}\n  estado vigente: {a.state_text[:200] or '—'}\n"
        f"  relaciones: {rels or '—'}"
    )


def build_prompt(conn: Connection, user_id: int, items: Sequence[Item], *, today: datetime) -> str:
    blocks = []
    cache: dict[int, str] = {}
    for it in items:
        parts = []
        for aid in it.asunto_ids:
            if aid not in cache:
                a = store.get_asunto(conn, user_id, aid)
                cache[aid] = _render_asunto(conn, user_id, a) if a else f"A{aid} (no existe)"
            parts.append(cache[aid])
        if it.extra:
            parts.append(it.extra)
        blocks.append(f"## item={it.key} · motivo: {it.reason}\n" + "\n".join(parts))
    index = ""
    if any(it.key.startswith("relq:") for it in items):
        index = (
            "## ÍNDICE DEL CATÁLOGO (id · nombre · tipo · ciclo · estado) — para set_relation\n"
            + "\n".join(store.index_lines(conn, user_id))
            + "\n\n"
        )
    return (
        f"{_INSTRUCCIONES}\nHoy: {today.date().isoformat()}\n\n{index}"
        f"Ítems ({len(items)}): para cada uno una decisión con item exacto.\n\n"
        + "\n\n".join(blocks)
    )


# --- aplicación ------------------------------------------------------------------------------ #


@dataclass
class CuratorStats:
    items: int = 0
    calls: int = 0
    merged: int = 0
    renamed: int = 0
    closed: int = 0
    reopened: int = 0
    deleted: int = 0
    kept: int = 0
    related: int = 0
    relations_rejected: int = 0
    evidence_rejected: int = 0
    retyped: int = 0
    skipped: int = 0
    dropped: int = 0
    errors: int = 0
    applied: list[dict[str, Any]] = field(default_factory=list)


def apply_decision(
    conn: Connection,
    user_id: int,
    d: dict[str, Any],
    items_by_key: dict[str, Item],
    *,
    min_confidence: float,
) -> str:
    it = items_by_key.get(str(d.get("item", "")))
    if it is None:
        return "dropped:item_desconocido"
    action = str(d.get("action", "skip"))
    conf = float(d.get("confidence") or 0)
    if action == "skip":
        return "skipped"
    if conf < min_confidence:
        return "skipped:low_confidence"
    ids = set(it.asunto_ids)

    def _id(v: Any, default: int | None = None) -> int | None:
        try:
            x = int(v)
        except (TypeError, ValueError):
            return default
        return x if x in ids else default

    if action == "keep":
        if it.key.startswith("relq:"):
            store.mark_relations_reviewed(conn, user_id, next(iter(ids)))
        elif it.key.startswith("name:"):
            store.mark_name_reviewed(conn, user_id, next(iter(ids)))
        elif it.key.startswith("ev:"):
            store.mark_evidence_reviewed(conn, user_id, [int(it.key.split(":", 1)[1])])
        elif it.key.startswith("asunto:"):
            store.mark_inactive_reviewed(conn, user_id, next(iter(ids)))
        return "kept"
    if action == "reject_evidence":
        if not it.key.startswith("ev:"):
            return "dropped:reject_evidence_sin_item_ev"
        eid = int(it.key.split(":", 1)[1])
        store.mark_evidence_reviewed(conn, user_id, [eid])
        return (
            "evidence_rejected"
            if store.reject_evidence(conn, user_id, eid) is not None
            else "dropped:evidencia_no_existe"
        )
    if action == "reject_relation":
        if not it.key.startswith("rel:"):
            return "dropped:reject_relation_sin_item_rel"
        rid = int(it.key.split(":", 1)[1])
        return (
            "relation_rejected"
            if store.reject_relation(conn, user_id, rid)
            else "dropped:relacion_no_existe"
        )
    if action == "merge":
        surv = _id(d.get("asunto_id"), None)
        absorbed = _id(d.get("other_id"), None)
        if surv is None or absorbed is None or surv == absorbed:
            if len(ids) == 2 and surv is not None:
                absorbed = next(iter(ids - {surv}))
            else:
                return "dropped:merge_sin_par"
        return (
            "merged"
            if store.merge_asuntos(conn, user_id, survivor_id=surv, absorbed_id=absorbed)
            else "dropped:merge_noop"
        )
    target = _id(d.get("asunto_id"), next(iter(ids)) if len(ids) == 1 else None)
    if target is None:
        return "dropped:asunto_fuera_del_item"
    if action == "rename":
        name = str(d.get("name") or "").strip()
        if not name:
            return "dropped:rename_sin_nombre"
        store.rename(conn, user_id, target, name)
        kind_after = str(d.get("kind") or "")
        if kind_after in store.KINDS:
            store.update_fields(conn, user_id, target, kind=kind_after)
        store.mark_name_reviewed(conn, user_id, target)
        return "renamed"
    if action == "close":
        store.record_state(
            conn, user_id, target, str(d.get("state") or ""), closed=True, source="curator"
        )
        return "closed"
    if action == "reopen":
        # Un silenciado no vuelve: `set_status` lo rechaza y devuelve False (ver store).
        store.set_status(conn, user_id, target, "abierto", source="curator")
        if d.get("state"):
            store.record_state(
                conn, user_id, target, str(d["state"]), closed=False, source="curator"
            )
        return "reopened"
    if action == "delete":
        a = store.get_asunto(conn, user_id, target)
        if a is None:
            return "dropped:no_existe"
        if a.status != "candidato" and a.evidence_count > 2:
            return "dropped:delete_solo_candidatos_o_ruido"
        return "deleted" if store.delete_asunto(conn, user_id, target) else "dropped:delete_noop"
    if action == "set_kind":
        kind = str(d.get("kind") or "")
        if kind not in store.KINDS:
            return "dropped:kind_invalido"
        store.update_fields(conn, user_id, target, kind=kind)
        if it.key.startswith("name:"):
            store.mark_name_reviewed(conn, user_id, target)
        return "retyped"
    if action == "set_relation":
        rt = str(d.get("relation_type") or "")
        if it.key.startswith("relq:"):
            # Pase holístico: cualquier id del índice, mientras uno de los dos sea el del ítem.
            me = next(iter(ids))
            try:
                s_id, d_id = int(d.get("asunto_id") or 0), int(d.get("other_id") or 0)
            except (TypeError, ValueError):
                return "dropped:relacion_invalida"
            if me not in (s_id, d_id) or s_id == d_id or rt not in store.RELATION_TYPES:
                return "dropped:relacion_invalida"
            other = d_id if s_id == me else s_id
            if store.get_asunto(conn, user_id, other) is None:
                return "dropped:relacion_asunto_inexistente"
            store.mark_relations_reviewed(conn, user_id, me)
            return (
                "related"
                if store.add_relation(
                    conn, user_id, s_id, d_id, rt, confidence=conf, source="curator"
                )
                else "dropped:relacion_existente"
            )
        src = _id(d.get("asunto_id"), None)
        dst = _id(d.get("other_id"), None)
        if rt not in store.RELATION_TYPES or src is None or dst is None:
            return "dropped:relacion_invalida"
        return (
            "related"
            if store.add_relation(conn, user_id, src, dst, rt, confidence=conf, source="curator")
            else "dropped:relacion_existente"
        )
    return f"dropped:accion_desconocida:{action}"


def apply_decisions(
    conn: Connection,
    user_id: int,
    decisions: Sequence[dict[str, Any]],
    items: Sequence[Item],
    *,
    min_confidence: float,
    stats: CuratorStats,
) -> None:
    by_key = {it.key: it for it in items}
    counters = {
        "merged": "merged",
        "renamed": "renamed",
        "closed": "closed",
        "reopened": "reopened",
        "deleted": "deleted",
        "kept": "kept",
        "related": "related",
        "relation_rejected": "relations_rejected",
        "evidence_rejected": "evidence_rejected",
        "retyped": "retyped",
    }
    for d in decisions:
        sp = conn.begin_nested()
        try:
            result = apply_decision(conn, user_id, d, by_key, min_confidence=min_confidence)
            sp.commit()
        except Exception as e:
            sp.rollback()
            result = f"error:{type(e).__name__}"
            stats.errors += 1
        if result in counters:
            setattr(stats, counters[result], getattr(stats, counters[result]) + 1)
        elif result.startswith("skipped"):
            stats.skipped += 1
        elif result.startswith("dropped"):
            stats.dropped += 1
        stats.applied.append(
            {
                "item": d.get("item"),
                "action": d.get("action"),
                "result": result,
                "confidence": d.get("confidence"),
                "rationale": str(d.get("rationale", ""))[:300],
            }
        )
        _log.info(
            "asuntos.curator.decision",
            user_id=user_id,
            item=d.get("item"),
            action=d.get("action"),
            result=result,
            confidence=d.get("confidence"),
        )


# --- corrida ------------------------------------------------------------------------------- #


def _resolve_binary() -> list[str]:
    resolved = shutil.which("codex")
    if resolved is None:
        raise RuntimeError("binario `codex` no encontrado en PATH (¿npm i -g @openai/codex?)")
    return [resolved]


async def _ask_codex(
    prompt: str, *, binary: Sequence[str], web_search: bool, timeout_s: float
) -> CodexRun:
    import os

    sandbox = os.environ.get("MEMEX_CODEX_SANDBOX", "read-only")
    with tempfile.TemporaryDirectory(prefix="memex-asuntos-curator-") as tmp:
        schema_file = Path(tmp) / "schema.json"
        schema_file.write_text(json.dumps(_schema()), encoding="utf-8")
        extra = ["--output-schema", str(schema_file)]
        if web_search:
            extra = ["-c", "web_search_mode=live", *extra]
        return await run_codex(
            binary, prompt, extra_args=extra, sandbox=sandbox, timeout_s=timeout_s
        )


def parse_decisions(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    ds = data.get("decisions") if isinstance(data, dict) else None
    return [d for d in (ds or []) if isinstance(d, dict) and str(d.get("action", "")) in ACTIONS]


async def run_curator(
    user_id: int,
    *,
    scope: str = "all",
    limit: int = 200,
    batch: int = _DEFAULT_BATCH,
    dry_run: bool = False,
    web_search: bool = True,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    binary: Sequence[str] | None = None,
    ask: Any | None = None,
) -> CuratorStats:
    """Una pasada del curador. `ask(prompt) -> str` inyectable (tests); si no, codex."""
    stats = CuratorStats()
    with connection() as conn:
        cfg = get_settings(conn, user_id)
        items = collect_items(
            conn, user_id, scope=scope, limit=limit, inactive_days=cfg.inactive_days
        )
        stats.items = len(items)
        if not items:
            return stats
        now = datetime.now(UTC)
        prompts = [
            (items[i : i + batch], build_prompt(conn, user_id, items[i : i + batch], today=now))
            for i in range(0, len(items), batch)
        ]
    bin_ = list(binary) if binary else (None if ask else _resolve_binary())
    for chunk, prompt in prompts:
        try:
            if ask is not None:
                raw, usage, latency = str(await ask(prompt)), None, 0
            else:
                run = await _ask_codex(
                    prompt, binary=bin_ or [], web_search=web_search, timeout_s=timeout_s
                )
                raw, usage, latency = run.last_message, run.usage, run.latency_ms
        except Exception as e:
            stats.errors += 1
            _log.warning(
                "asuntos.curator.call_error",
                user_id=user_id,
                exc_type=type(e).__name__,
                exc_msg=str(e)[:300],
            )
            continue
        stats.calls += 1
        decisions = parse_decisions(raw)
        if not dry_run:
            with connection() as conn:
                apply_decisions(
                    conn,
                    user_id,
                    decisions,
                    chunk,
                    min_confidence=max(cfg.min_confidence, 0.7),
                    stats=stats,
                )
        else:
            for d in decisions:
                stats.applied.append(
                    {
                        "item": d.get("item"),
                        "action": d.get("action"),
                        "result": "dry_run",
                        "confidence": d.get("confidence"),
                        "rationale": str(d.get("rationale", ""))[:300],
                    }
                )
        record_llm_call(
            user_id=user_id,
            purpose=_PURPOSE,
            model="codex/default" if ask is None else "injected",
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_hit_tokens=getattr(usage, "cached_input_tokens", 0) or 0,
            cost_usd=Decimal("0"),
            latency_ms=latency,
            status="ok",
            response_text=raw,
            metadata={
                "scope": scope,
                "items": [it.key for it in chunk],
                "decisions": len(decisions),
                "dry_run": dry_run,
            },
        )
    _log.info(
        "asuntos.curator.end",
        user_id=user_id,
        **{k: v for k, v in asdict(stats).items() if k != "applied"},
    )
    return stats


# --- reglas del sistema + mantenimiento ------------------------------------------------------- #


@dataclass
class SystemRulesStats:
    promoted: int = 0
    inactivated: int = 0
    reactivated: int = 0
    restated: int = 0  #: estado vigente re-derivado por fecha efectiva del mensaje
    hitos_deduped: int = 0  #: hitos repetidos (entrega duplicada / mismo texto) retirados
    seeded: int = 0  #: asuntos sin estado que recibieron su estado inicial (hito o descripción)
    closed_by_date: int = 0  #: eventos/hackatones cuyo último hito ya pasó → cerrados
    priority_raised: int = 0  #: hito futuro a <= 14 días → prioridad alta (regla)
    due_cleared: int = 0  #: plazos vencidos con señal posterior → borrados (ruido)
    closed_paid: int = 0  #: asuntos de pago con pago posterior registrado en finanzas → cerrados


def run_system_rules(conn: Connection, user_id: int) -> SystemRulesStats:
    """Sin LLM: promueve candidatos que ya juntaron evidencia; `abierto` sin señal N días →
    `inactivo` e `inactivo` con señal nueva → `abierto` (`store.sweep_inactivity`, respeta la
    palabra del usuario); evento/hackatón con el último hito ya pasado, con plazo vencido o sin
    ninguna fecha y semanas sin señal → `cerrado` (`store.close_expired`). Las mismas reglas
    corren al cierre de cada corrida del asignador."""
    cfg = get_settings(conn, user_id)
    st = SystemRulesStats()
    for a in store.list_asuntos(conn, user_id, statuses=("candidato",), limit=5000):
        if store.promote_if_ready(conn, user_id, a.id, min_evidence_open=cfg.min_evidence_open):
            st.promoted += 1
    st.inactivated, st.reactivated = store.sweep_inactivity(
        conn, user_id, inactive_days=cfg.inactive_days
    )
    st.closed_by_date = store.close_expired(conn, user_id, undated_days=cfg.inactive_days // 2)
    st.priority_raised = store.raise_priority_for_deadlines(conn, user_id)
    st.due_cleared = store.clear_stale_due(conn, user_id)
    st.closed_paid = finance_link.close_paid(conn, user_id)
    for a in store.list_asuntos(conn, user_id, limit=5000):
        st.restated += int(store.rederive_state(conn, user_id, a.id))
    st.hitos_deduped = store.dedupe_hitos(conn, user_id)
    st.seeded = store.seed_missing_states(conn, user_id)
    return st


def get_marks(conn: Connection, user_id: int) -> dict[str, datetime | None]:
    row = conn.execute(
        text("SELECT curator_marks FROM asuntos_settings WHERE user_id = :u"), {"u": user_id}
    ).scalar()
    marks = row if isinstance(row, dict) else {}
    v = marks.get("last_run_at")
    return {"last_run_at": datetime.fromisoformat(str(v)) if v else None}


def _set_marks(conn: Connection, user_id: int, when: datetime) -> None:
    from memex.modules.asuntos.settings import upsert_settings

    upsert_settings(conn, user_id)  # asegura la fila
    conn.execute(
        text(
            "UPDATE asuntos_settings SET curator_marks = jsonb_set(curator_marks, '{last_run_at}', "
            "to_jsonb(CAST(:v AS TEXT))), updated_at = NOW() WHERE user_id = :u"
        ),
        {"u": user_id, "v": when.isoformat()},
    )


@dataclass
class MaintenanceStats:
    promoted: int = 0
    inactivated: int = 0
    reactivated: int = 0
    restated: int = 0
    hitos_deduped: int = 0
    seeded: int = 0
    curator_items: int = 0
    curator_calls: int = 0
    merged: int = 0
    renamed: int = 0
    closed: int = 0
    deleted: int = 0
    related: int = 0
    evidence_rejected: int = 0
    errors: int = 0


async def run_maintenance(
    user_id: int, *, web_search: bool = True, dry_run: bool = False, ask: Any | None = None
) -> MaintenanceStats:
    """Job `asuntos_curate` / CLI `memex asuntos maintain`: reglas del sistema + curador `all`."""
    out = MaintenanceStats()
    with connection() as conn:
        s = run_system_rules(conn, user_id)
        out.promoted, out.inactivated = s.promoted, s.inactivated
        out.reactivated, out.restated = s.reactivated, s.restated
        out.seeded = s.seeded
        out.hitos_deduped = s.hitos_deduped
    c = await run_curator(user_id, scope="all", web_search=web_search, dry_run=dry_run, ask=ask)
    out.curator_items, out.curator_calls = c.items, c.calls
    out.merged, out.renamed, out.closed, out.deleted, out.related, out.errors = (
        c.merged,
        c.renamed,
        c.closed,
        c.deleted,
        c.related,
        c.errors,
    )
    out.evidence_rejected = c.evidence_rejected
    if not dry_run:
        with connection() as conn:
            _set_marks(conn, user_id, datetime.now(UTC))
    _log.info("asuntos.maintenance.end", user_id=user_id, **asdict(out))
    return out
