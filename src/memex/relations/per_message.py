"""Confirmación de co-ocurrencia POR-MENSAJE (metodología B): la PRIMERA fase del grafo.

Una co-ocurrencia nace `extracted+ambiguous` (la co-aparición es un hecho; la relación, sospecha sin
juzgar). Esta fase abre cada mensaje y, en UNA llamada LLM por mensaje, juzga TODOS los pares de
co-ocurrencia que nacieron de él: por par devuelve veredicto (confirm/reject/dejar) + una `relation`
nombrada EMERGENTE; y en la misma llamada, un `summary` del mensaje. Es el reemplazo del resolver
par-por-par viejo (basado en cúmulos + citas): acá la unidad es el MENSAJE, no el vecindario, y la
compuerta anti-alucinación es determinista (el vértice confirmado debe aparecer en el cuerpo por
nombre/alias, `relations.gate`), no una cita textual.

Esta fase es el ÚNICO productor de `summaries` (reemplaza al summarizer). El resumen del juicio se
persiste SOLO para tier `individual` (correo = unidad); los `batch` (chat/email batch) se resumen
por LOTE en `run_summaries` (`relations.summary`), que también cubre los individuales sin pares de
co-ocurrencia. Así toda unidad relevante termina con resumen, lo juzgue o no la co-ocurrencia.

Cascada determinista-primero (FrugalGPT/Snorkel):
1. A PRIORI sin LLM: un par cuya evidencia incluye un RECIBO (mensaje que produjo una transacción de
   finanzas) se confirma por regla `extracted` — el dato real ya vouchó la relación.
2. LLM por mensaje (presupuestado): el resto. Confirm exige confianza ≥ `per_message_min_confidence`
   (0.85) Y pasar la compuerta alias-aware; si no, degrada a ambiguo.

Aristas soportadas por varios mensajes: el juicio es por-mensaje, la arista es una. Agregación
MONÓTONA, confirm gana: un confirm grounded en cualquier mensaje confirma (la mejor confianza, su
`relation`); los mensajes que no la justifican no la tumban. Evaluada COMPLETA sin confirm y todos
reject → rejected; evaluada completa sin veredicto → memo `dejar` (no se re-gasta LLM mientras la
evidencia no cambie) y la arista queda `inferred+ambiguous` (la IA la miró y no supo). Parcial
(presupuesto/chat) → nada (se reintenta).

Procedencia: una confirmación/rechazo del LLM es `inferred`; el a-priori del recibo es `extracted`.
Groundwork incremental (ADR-021): al confirmar, los dos vértices se marcan `dirty`.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.classifier.rules import TIER_BLACKLIST, classify
from memex.config import settings
from memex.core.observability import CostAccum, record_llm_call
from memex.core.source import SourceKind
from memex.core.trace import attach_to_root
from memex.db import connection
from memex.llm import ChatMessage, LLMClient, LLMResult, aclose_llm, build_llm_client
from memex.llm.client import LLMQuotaError
from memex.logging import get_logger
from memex.processing.render import render_payload
from memex.relations.cooccurrence import generate_cooccurrence, vertex_inbox_ids
from memex.relations.decisions import (
    METHOD_LLM,
    METHOD_REGLA,
    VERDICT_CONFIRM,
    VERDICT_DEJAR,
    VERDICT_REJECT,
    edge_sources,
    evidence_signature,
    latest_decisions,
    record_decision,
)
from memex.relations.edges import (
    CANAL_SLUG,
    PRODUCER_INBOX,
    PROVENANCE_EXTRACTED,
    PROVENANCE_INFERRED,
    RELTYPE_COOCURRENCIA,
    VERDICT_AMBIGUOUS,
    VERDICT_CONFIRMED,
    VERDICT_REJECTED,
    Ref,
    RelationEdge,
    list_edges,
    mark_vertices_dirty,
    resolve_edge,
)
from memex.relations.gate import both_endpoints_present, normalize_body, vertex_surface_forms
from memex.relations.prompt import GRAPH_CONFIRM_SYSTEM_PROMPT
from memex.relations.summary import persist_summary, run_summaries
from memex.relations.vertices import Vertex, list_vertices
from memex.sources import kind_for_type

_log = get_logger("memex.relations.per_message")

# --- Etiquetas del prefiltro (TIPO de mensaje; labeling functions deterministas) ------ #
LABEL_RECIBO = "recibo"  #: el mensaje produjo una transacción de finanzas (señal alta a priori)
LABEL_BULK = "bulk"  #: marcadores de correo masivo — PRIOR, contexto, no veredicto
LABEL_CHAT = "chat"  #: mensaje de un medio de chat (SourceKind.CHAT)
LABEL_DESCONOCIDO = "desconocido"  #: sin señal determinista

RULE_RECIBO = "recibo"
_MAX_RELATION_CHARS = 80
_MAX_TOKENS = 4096
_VALID_LLM_VERDICTS = frozenset({VERDICT_CONFIRM, VERDICT_REJECT, VERDICT_DEJAR})
_BULK_NOTE = (
    "[Señal determinista: el mensaje declara encabezados de correo masivo (lista/newsletter). "
    "Es contexto, no un veredicto: un correo masivo igual puede contener una relación real.]"
)


@dataclass
class ConfirmStats:
    """Resumen de una corrida de la confirmación (los conteos de dry-run son proyecciones)."""

    edges: int = 0  # aristas ambiguas en el universo (tras saltar los `dejar` vigentes)
    skipped_dejar: int = 0  # aristas saltadas: memo `dejar` con la misma evidencia
    confirmed_recibo: int = 0  # confirmadas a priori (recibo de finanzas), sin LLM
    messages: int = 0  # mensajes distintos con pares por juzgar
    chat_skipped: int = 0  # mensajes de chat NO enviados al LLM (confirm_judge_chats=False)
    llm_calls: int = 0  # llamadas LLM efectivas
    llm_confirmed: int = 0  # confirmadas por el LLM (compuerta + confianza)
    llm_rejected: int = 0  # rechazadas por el LLM (todos sus mensajes lo rechazan)
    llm_dejar: int = 0  # memo `dejar` del LLM (evaluada completa, sin veredicto)
    gated: int = 0  # confirms del LLM degradados: un extremo no aparece en el cuerpo
    summaries: int = 0  # resúmenes persistidos (individual del juicio + lotes de run_summaries)
    budget_exhausted: bool = False
    estimated_calls: int = 0  # dry-run: llamadas que haría
    errors: int = 0
    cost: CostAccum = field(default_factory=CostAccum)


# --- prefiltro determinista (labeling functions) -------------------------------------- #


class MessageLabeler:
    """Etiqueta el TIPO de cada mensaje con señales deterministas, precargadas en bloque:
    RECIBO (produjo una transacción de finanzas) > BULK (marcadores de correo masivo, vía
    `classifications` con fallback a `classifier.rules.classify`) > CHAT (medio de chat) >
    DESCONOCIDO."""

    def __init__(self, conn: Connection, user_id: int, inbox_ids: Iterable[int]) -> None:
        self._labels: dict[int, str] = {}
        self._tiers: dict[int, str] = {}  #: tier efectivo (clasificación o fallback), por mensaje
        ids = sorted(set(inbox_ids))
        if not ids:
            return
        recibo: set[int] = {
            int(x)
            for x in conn.execute(
                text(
                    "SELECT DISTINCT mid FROM mod_finance_transactions t "
                    "CROSS JOIN LATERAL unnest(t.source_inbox_ids) AS mid "
                    "WHERE t.user_id = :u AND mid = ANY(:ids)"
                ),
                {"u": user_id, "ids": ids},
            ).scalars()
        }
        tiers: dict[int, str] = {
            int(r[0]): str(r[1])
            for r in conn.execute(
                text(
                    "SELECT inbox_id, tier FROM classifications "
                    "WHERE user_id = :u AND inbox_id = ANY(:ids)"
                ),
                {"u": user_id, "ids": ids},
            )
        }
        rows = conn.execute(
            text(
                "SELECT i.id AS id, i.payload AS payload, s.type AS type "
                "FROM inbox i JOIN sources s ON s.id = i.source_id "
                "WHERE i.user_id = :u AND i.id = ANY(:ids)"
            ),
            {"u": user_id, "ids": ids},
        ).mappings()
        for r in rows:
            mid = int(r["id"])
            tier = tiers.get(mid)
            if tier is None:
                tier = classify(dict(r["payload"])).tier
            self._tiers[mid] = tier
            if mid in recibo:
                self._labels[mid] = LABEL_RECIBO
                continue
            if tier == TIER_BLACKLIST:
                self._labels[mid] = LABEL_BULK
                continue
            try:
                kind = kind_for_type(str(r["type"]))
            except KeyError:
                kind = None
            self._labels[mid] = LABEL_CHAT if kind == SourceKind.CHAT else LABEL_DESCONOCIDO

    def label(self, inbox_id: int) -> str:
        return self._labels.get(inbox_id, LABEL_DESCONOCIDO)

    def tier(self, inbox_id: int) -> str | None:
        """El tier efectivo del mensaje (clasificación, con fallback a `classify`); None si no se
        precargó. Se usa para persistir el resumen del juicio SOLO en `individual` (los `batch` los
        resume `run_summaries` por lote)."""
        return self._tiers.get(inbox_id)


# --- universo + evidencia ------------------------------------------------------------- #


def _load_universe(conn: Connection, user_id: int) -> list[RelationEdge]:
    """Las aristas a juzgar: co-ocurrencia del inbox AMBIGUA, sin extremos `canal` (el canal es un
    hub estructural; su relación real ya es `participa_en`)."""
    return [
        e
        for e in list_edges(conn, user_id, verdict=VERDICT_AMBIGUOUS, producer=PRODUCER_INBOX)
        if e.relation_type == RELTYPE_COOCURRENCIA
        and e.src.slug != CANAL_SLUG
        and e.dst.slug != CANAL_SLUG
    ]


def _attach_evidence(
    conn: Connection, user_id: int, universe: list[RelationEdge]
) -> dict[int, frozenset[int]]:
    """Los mensajes-evidencia de cada arista desde `relation_edge_sources`; fallback para pistas
    pre-backfill (sin filas): la intersección de la provenance de ambos extremos."""
    srcs = edge_sources(conn, [e.id for e in universe])
    if any(not srcs.get(e.id) for e in universe):
        prov = vertex_inbox_ids(conn, user_id)
        for e in universe:
            if not srcs.get(e.id):
                srcs[e.id] = prov.get(e.src, set()) & prov.get(e.dst, set())
    return {e.id: frozenset(srcs.get(e.id, set())) for e in universe}


# --- parseo de la respuesta B --------------------------------------------------------- #


@dataclass(frozen=True)
class PairVerdict:
    """El veredicto del LLM para UN par (id local 1..n) en UN mensaje."""

    pair: int
    verdict: str
    relation: str
    confidence: float


def parse_confirm(content: str, n_pairs: int) -> tuple[dict[int, PairVerdict], str]:
    """Parsea `{"verdicts":[{pair, verdict, relation, confidence}], "summary": "..."}`. ULTRA
    DEFENSIVO (molde `resolve_llm.parse_verdicts` + `experiments/parsing.py`): basura → `({}, "")`;
    ids fuera de 1..n o bool-como-int fuera; verdict fuera del vocabulario fuera; `confirm` sin
    `relation` degrada a `dejar`; confianza clampeada 0..1; dedup primero-gana."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {}, ""
    if not isinstance(data, dict):
        return {}, ""
    summary = data.get("summary")
    summary_str = summary.strip() if isinstance(summary, str) else ""
    raw = data.get("verdicts")
    if not isinstance(raw, list):
        return {}, summary_str
    out: dict[int, PairVerdict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = item.get("pair")
        if not (isinstance(pid, int) and not isinstance(pid, bool) and 1 <= pid <= n_pairs):
            continue
        if pid in out:
            continue
        verdict = item.get("verdict")
        if not isinstance(verdict, str) or verdict not in _VALID_LLM_VERDICTS:
            continue
        rel = item.get("relation")
        relation = rel.strip()[:_MAX_RELATION_CHARS] if isinstance(rel, str) else ""
        conf = item.get("confidence")
        confidence = (
            float(conf) if isinstance(conf, int | float) and not isinstance(conf, bool) else 0.0
        )
        if verdict == VERDICT_CONFIRM and not relation:
            verdict = VERDICT_DEJAR  # confirm sin nombre no es accionable
        out[pid] = PairVerdict(pid, verdict, relation, max(0.0, min(1.0, confidence)))
    return out, summary_str


# --- render + serialización del prompt ------------------------------------------------ #


def load_rendered(conn: Connection, user_id: int, inbox_id: int) -> str:
    """El mensaje renderizado a texto plano (payload + OCR de sus imágenes), truncado a
    `per_message_render_max_chars`. Es el texto que ve el LLM Y contra el que la compuerta busca los
    nombres/alias. Mensaje purgado → ""."""
    row = conn.execute(
        text(
            """
            SELECT i.payload AS payload, COALESCE(ma.ocr_text, '') AS ocr
            FROM inbox i
            LEFT JOIN (
                SELECT inbox_id, string_agg(ocr_text, E'\n' ORDER BY id) AS ocr_text
                FROM media_assets
                WHERE ocr_status = 'ok' AND ocr_text IS NOT NULL AND ocr_text <> ''
                GROUP BY inbox_id
            ) ma ON ma.inbox_id = i.id
            WHERE i.user_id = :u AND i.id = :m
            """
        ),
        {"u": user_id, "m": inbox_id},
    ).first()
    if row is None:
        return ""
    return render_payload(dict(row[0]), str(row[1] or ""))[: settings.per_message_render_max_chars]


def _vlabel(ref: Ref, vmap: dict[Ref, Vertex]) -> str:
    v = vmap.get(ref)
    return f"{v.label} ({v.kind})" if v is not None else f"{ref.slug}#{ref.id}"


def _serialize(rendered: str, edges: list[RelationEdge], vmap: dict[Ref, Vertex], note: str) -> str:
    """El cuerpo del prompt B: nota de señales (si hay) + MENSAJE renderizado + PARES numerados."""
    lines: list[str] = []
    if note:
        lines.append(note)
        lines.append("")
    lines.append("MENSAJE:")
    lines.append(rendered if rendered else "(mensaje vacío o purgado)")
    lines.append("")
    lines.append("PARES A JUZGAR (id: entidad ↔ entidad):")
    for i, e in enumerate(edges, start=1):
        lines.append(f"{i}: {_vlabel(e.src, vmap)} ↔ {_vlabel(e.dst, vmap)}")
    return "\n".join(lines)


async def judge_message(
    llm: LLMClient,
    rendered: str,
    edges: list[RelationEdge],
    vmap: dict[Ref, Vertex],
    note: str = "",
) -> tuple[dict[int, PairVerdict], str, LLMResult]:
    """UNA llamada B que juzga todos los pares de UN mensaje + devuelve su resumen. Devuelve
    (veredictos por id local 1..n, summary, LLMResult)."""
    result = await llm.complete(
        [
            ChatMessage("system", GRAPH_CONFIRM_SYSTEM_PROMPT),
            ChatMessage("user", _serialize(rendered, edges, vmap, note)),
        ],
        response_format="json_object",
        temperature=0.0,
        max_tokens=_MAX_TOKENS,
    )
    verdicts, summary = parse_confirm(result.content, len(edges))
    return verdicts, summary, result


# --- agregación de votos por arista --------------------------------------------------- #


@dataclass(frozen=True)
class _Vote:
    """El veredicto de UN mensaje sobre UNA arista, ya pasado por la compuerta alias-aware."""

    inbox_id: int
    verdict: str
    relation: str
    confidence: float
    gated_ok: bool  #: confirm cuyos dos extremos aparecen en el cuerpo (pasa la compuerta)


def _dec(x: float) -> Decimal:
    return Decimal(str(round(x, 3)))


# --- worker --------------------------------------------------------------------------- #


def _parse_vertex(raw: str) -> Ref:
    """`slug:id` → Ref (el slug puede contener `:`, p.ej. `identidades:person:7` → rsplit)."""
    slug, sep, sid = raw.rpartition(":")
    if not sep or not slug or not sid.isdigit():
        raise ValueError(f"vértice inválido: {raw!r} (esperado slug:id)")
    return Ref(slug, int(sid))


async def run_per_message_confirm(
    user_id: int,
    *,
    limit: int | None = None,
    budget: int | None = None,
    dry_run: bool = False,
    no_llm: bool = False,
    client: LLMClient | None = None,
) -> ConfirmStats:
    """Corre la confirmación por-mensaje sobre todas las co-ocurrencias ambiguas. Idempotente:
    re-correr sin evidencia nueva re-juzga solo lo no decidido (terminales fuera del universo,
    `dejar` saltado por sig). `dry_run` clasifica sin escribir; `no_llm` aplica solo el a-priori del
    recibo. `client` inyectable (tests con fake). `LLMQuotaError` PROPAGA tras aplicar lo pagado."""
    stats = ConfirmStats()
    run_id = uuid.uuid4().hex
    budget = budget if budget is not None else settings.per_message_max_llm_calls
    min_conf = settings.per_message_min_confidence

    # Paso 7 (generación): materializa las pistas de co-ocurrencia (determinista, idempotente) ANTES
    # de cargar el universo a juzgar. Reemplaza al barrido global `build_relations`. En `dry_run` NO
    # se genera (no se escribe nada): la proyección se hace sobre las pistas ya existentes.
    if not dry_run:
        with connection() as conn:
            generate_cooccurrence(conn, user_id, cap=settings.cooccurrence_cap)

    with connection() as conn:
        universe = _load_universe(conn, user_id)
        evidence = _attach_evidence(conn, user_id, universe)
        decisions = latest_decisions(conn, user_id, [e.id for e in universe])
        work: list[RelationEdge] = []
        for e in universe:
            d = decisions.get(e.id)
            sig = evidence_signature(evidence[e.id])
            if d is not None and d.verdict == VERDICT_DEJAR and d.evidence_sig == sig:
                stats.skipped_dejar += 1
                continue
            work.append(e)
        if limit is not None:
            work = work[:limit]
        stats.edges = len(work)
        all_msgs = {m for e in work for m in evidence[e.id]}
        labeler = MessageLabeler(conn, user_id, all_msgs)
        vmap = {v.ref: v for v in list_vertices(conn, user_id)}

    # --- FASE 1: a priori (recibo) — sin LLM, sin compuerta -------------------------- #
    recibo_edges: list[tuple[RelationEdge, int]] = []  # (arista, mensaje-recibo)
    gray: list[RelationEdge] = []
    for e in work:
        recibos = sorted(m for m in evidence[e.id] if labeler.label(m) == LABEL_RECIBO)
        if recibos:
            recibo_edges.append((e, recibos[0]))
        else:
            gray.append(e)

    # Mensajes que NO se mandan al LLM (chats, si confirm_judge_chats=False): para no marcar como
    # "evaluada completa" una arista cuya evidencia de chat nunca se juzga.
    excluded_msgs: set[int] = set()
    if not settings.confirm_judge_chats:
        excluded_msgs = {m for m in all_msgs if labeler.label(m) == LABEL_CHAT}

    by_msg: dict[int, list[RelationEdge]] = defaultdict(list)
    for e in gray:
        for m in sorted(evidence[e.id]):
            if m not in excluded_msgs:
                by_msg[m].append(e)
    stats.messages = len(by_msg)
    stats.chat_skipped = len({m for e in gray for m in evidence[e.id]} & excluded_msgs)

    if dry_run:
        stats.confirmed_recibo = len(recibo_edges)
        stats.estimated_calls = min(len(by_msg), budget) if not no_llm else 0
        _log.info(
            "relation.confirm.dry_run",
            user_id=user_id,
            edges=stats.edges,
            confirmed_recibo=stats.confirmed_recibo,
            messages=stats.messages,
            chat_skipped=stats.chat_skipped,
            estimated_calls=stats.estimated_calls,
        )
        return stats

    dirty_refs: set[Ref] = set()
    with connection() as conn:
        for e, mid in recibo_edges:
            if resolve_edge(
                conn,
                e.id,
                verdict=VERDICT_CONFIRMED,
                provenance=PROVENANCE_EXTRACTED,
                relation="comparten un recibo (transacción de finanzas)",
            ):
                record_decision(
                    conn,
                    user_id,
                    e.id,
                    verdict=VERDICT_CONFIRM,
                    method=METHOD_REGLA,
                    rule=RULE_RECIBO,
                    inbox_id=mid,
                    evidence_sig=evidence_signature(evidence[e.id]),
                    run_id=run_id,
                )
                dirty_refs.update((e.src, e.dst))
                stats.confirmed_recibo += 1
        if dirty_refs:
            mark_vertices_dirty(conn, user_id, sorted(dirty_refs, key=lambda r: (r.slug, r.id)))

    # --- FASE 2: LLM por mensaje (presupuestado) ------------------------------------- #
    if gray and by_msg and not no_llm:
        await _run_llm_phase(
            user_id,
            gray,
            by_msg,
            evidence,
            excluded_msgs,
            vmap,
            labeler,
            budget=budget,
            min_conf=min_conf,
            run_id=run_id,
            stats=stats,
            client=client,
        )

    # --- FASE 3: resumen de las unidades sin resumir (único productor de `summaries`) - #
    # Cubre lo que la Fase 2 no resumió: lotes (chat / email batch) e individuales sin pares de
    # co-ocurrencia. El `client` inyectado (test fake) se reusa; en producción `run_summaries` arma
    # el suyo (consumer `summarizer`). `LLMQuotaError` propaga (lo pagado quedó persistido por
    # unidad). El budget del juicio y el LIMIT del resumen son topes independientes.
    if not no_llm:
        summ = await run_summaries(user_id, client=client)
        stats.summaries += summ.summaries
        stats.errors += summ.errors
        stats.cost.calls += summ.cost.total.calls
        stats.cost.prompt_tokens += summ.cost.total.prompt_tokens
        stats.cost.completion_tokens += summ.cost.total.completion_tokens
        stats.cost.cost_usd += summ.cost.total.cost_usd

    _log.info(
        "relation.confirm.done",
        user_id=user_id,
        edges=stats.edges,
        skipped_dejar=stats.skipped_dejar,
        confirmed_recibo=stats.confirmed_recibo,
        messages=stats.messages,
        chat_skipped=stats.chat_skipped,
        llm_calls=stats.llm_calls,
        llm_confirmed=stats.llm_confirmed,
        llm_rejected=stats.llm_rejected,
        llm_dejar=stats.llm_dejar,
        gated=stats.gated,
        summaries=stats.summaries,
        budget_exhausted=stats.budget_exhausted,
        errors=stats.errors,
        llm_cost_usd=str(stats.cost.cost_usd),
    )
    return stats


async def _run_llm_phase(
    user_id: int,
    gray: list[RelationEdge],
    by_msg: dict[int, list[RelationEdge]],
    evidence: Mapping[int, frozenset[int]],
    excluded_msgs: set[int],
    vmap: dict[Ref, Vertex],
    labeler: MessageLabeler,
    *,
    budget: int,
    min_conf: float,
    run_id: str,
    stats: ConfirmStats,
    client: LLMClient | None,
) -> None:
    """El loop por mensaje (orden #pares DESC, máxima amortización), tope `budget` llamadas. Cada
    llamada juzga los pares PENDIENTES del mensaje (los ya confirmados en esta corrida se saltan),
    persiste el resumen si el mensaje es `individual`, aplica la compuerta a los confirms y acumula
    votos. Agrega al final (también si el presupuesto/cuota cortaron: lo pagado no se tira)."""
    order = sorted(by_msg, key=lambda m: (-len(by_msg[m]), m))
    # Formas de superficie de todos los extremos involucrados (para la compuerta).
    refs = {e.src for e in gray} | {e.dst for e in gray}
    with connection() as conn:
        forms = vertex_surface_forms(conn, user_id, refs, vmap)

    votes: dict[int, list[_Vote]] = defaultdict(list)
    evaluated: dict[int, set[int]] = defaultdict(set)
    confirmed_ids: set[int] = set()
    calls = 0
    quota: LLMQuotaError | None = None

    owns_client = client is None
    llm: LLMClient = client or build_llm_client("relations_confirm", user_id=user_id)
    _log.info("relation.confirm.llm.start", user_id=user_id, messages=len(by_msg), budget=budget)
    try:
        for mid in order:
            pend = [e for e in by_msg[mid] if e.id not in confirmed_ids]
            if not pend:
                continue
            if calls >= budget:
                stats.budget_exhausted = True
                _log.info("relation.confirm.llm.budget_exhausted", calls=calls, budget=budget)
                break
            pend = pend[: settings.per_message_max_pairs_per_call]
            with connection() as conn:
                rendered = load_rendered(conn, user_id, mid)
            note = _BULK_NOTE if labeler.label(mid) == LABEL_BULK else ""
            try:
                verdicts, summary, result = await judge_message(llm, rendered, pend, vmap, note)
            except LLMQuotaError as exc:
                quota = exc
                break
            except Exception as exc:  # best-effort: un mensaje fallido no frena los demás
                stats.errors += 1
                _log.error(
                    "relation.confirm.judge_failed",
                    inbox_id=mid,
                    exc_type=type(exc).__name__,
                    exc_msg=str(exc),
                )
                continue
            calls += 1
            body_norm = normalize_body(rendered)
            gated_msg = 0
            for i, e in enumerate(pend, start=1):
                v = verdicts.get(i)
                if v is None:  # el LLM omitió el par
                    vote = _Vote(mid, VERDICT_DEJAR, "", 0.0, False)
                elif v.verdict == VERDICT_CONFIRM and not both_endpoints_present(
                    forms.get(e.src, frozenset()), forms.get(e.dst, frozenset()), body_norm
                ):
                    gated_msg += 1
                    stats.gated += 1
                    _log.info("relation.confirm.gated", inbox_id=mid, edge_id=e.id)
                    vote = _Vote(mid, VERDICT_DEJAR, "", v.confidence, False)
                else:
                    vote = _Vote(
                        mid, v.verdict, v.relation, v.confidence, v.verdict == VERDICT_CONFIRM
                    )
                votes[e.id].append(vote)
                evaluated[e.id].add(mid)
                if vote.gated_ok and vote.confidence >= min_conf:
                    confirmed_ids.add(e.id)
            # Persistir el resumen SOLO para tier individual (correo = unidad). Los batch (chat /
            # email batch) los resume `run_summaries` por LOTE → no se persiste acá, para no partir
            # el lote (sus co-miembros sin pares aún no se resumieron). Ver `relations.summary`.
            if summary and labeler.tier(mid) == "individual":
                with connection() as conn:
                    persist_summary(
                        conn, user_id, [mid], summary, tier="individual", origin="graph_confirm"
                    )
                stats.summaries += 1
            _record_call_cost(user_id, mid, len(pend), gated_msg, result, stats)
    finally:
        if owns_client:
            await aclose_llm(llm)
    stats.llm_calls = calls

    _apply_votes(
        user_id,
        gray,
        votes,
        evaluated,
        evidence,
        excluded_msgs,
        min_conf=min_conf,
        run_id=run_id,
        stats=stats,
    )
    if quota is not None:
        raise quota


def _record_call_cost(
    user_id: int, mid: int, n_pairs: int, gated: int, result: LLMResult, stats: ConfirmStats
) -> None:
    """Telemetría de la llamada: `record_llm_call` (purpose='graph_confirm', inbox en metadata —la
    FK puede purgarse) + el costo colgado del ROOT de traza del mensaje si existe."""
    call_id = record_llm_call(
        user_id=user_id,
        purpose="graph_confirm",
        model=result.model,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cache_hit_tokens=result.usage.cache_hit_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        status="ok",
        source_id=None,
        metadata={"inbox_id": mid, "pairs": n_pairs, "gated": gated},
    )
    with connection() as conn:
        node = attach_to_root(conn, user_id=user_id, inbox_id=mid)
        if node is not None:
            node.llm(
                call_id,
                label="confirmación co-ocurrencia",
                status="ok",
                detail={"pairs": n_pairs, "gated": gated},
            )
    stats.cost.calls += 1
    stats.cost.prompt_tokens += result.usage.prompt_tokens
    stats.cost.completion_tokens += result.usage.completion_tokens
    stats.cost.cost_usd += result.cost_usd


def _apply_votes(
    user_id: int,
    gray: list[RelationEdge],
    votes: dict[int, list[_Vote]],
    evaluated: dict[int, set[int]],
    evidence: Mapping[int, frozenset[int]],
    excluded_msgs: set[int],
    *,
    min_conf: float,
    run_id: str,
    stats: ConfirmStats,
) -> None:
    """Agrega los votos por arista y aplica en UNA tx (monótono, confirm gana). Un confirm que pasó
    la compuerta con confianza ≥ umbral en CUALQUIER mensaje → confirmed/inferred (la mejor
    gana, con su `relation`). Evaluada COMPLETA (todos sus mensajes no-excluidos juzgados) y todos
    reject ≥ umbral → rejected/inferred; evaluada completa sin veredicto → memo `dejar` y la arista
    queda `inferred+ambiguous` (la IA la miró). Parcial → nada (se reintenta)."""
    dirty_refs: set[Ref] = set()
    with connection() as conn:
        for e in sorted(gray, key=lambda e: e.id):
            vs = votes.get(e.id, [])
            if not vs:
                continue
            sig = evidence_signature(evidence[e.id])
            confirms = [v for v in vs if v.gated_ok and v.confidence >= min_conf]
            if confirms:
                best = max(confirms, key=lambda v: (v.confidence, -v.inbox_id))
                if resolve_edge(
                    conn,
                    e.id,
                    verdict=VERDICT_CONFIRMED,
                    provenance=PROVENANCE_INFERRED,
                    relation=best.relation,
                    confidence=_dec(best.confidence),
                ):
                    record_decision(
                        conn,
                        user_id,
                        e.id,
                        verdict=VERDICT_CONFIRM,
                        method=METHOD_LLM,
                        inbox_id=best.inbox_id,
                        quote=best.relation,
                        confidence=_dec(best.confidence),
                        evidence_sig=sig,
                        run_id=run_id,
                    )
                    dirty_refs.update((e.src, e.dst))
                    stats.llm_confirmed += 1
                continue
            intended = evidence[e.id] - excluded_msgs
            if not (evaluated.get(e.id, set()) >= intended):
                continue  # parcial (presupuesto/chat): ni terminal ni memo, se reintenta
            if all(v.verdict == VERDICT_REJECT and v.confidence >= min_conf for v in vs):
                best = max(vs, key=lambda v: (v.confidence, -v.inbox_id))
                if resolve_edge(
                    conn,
                    e.id,
                    verdict=VERDICT_REJECTED,
                    provenance=PROVENANCE_INFERRED,
                    confidence=_dec(best.confidence),
                ):
                    record_decision(
                        conn,
                        user_id,
                        e.id,
                        verdict=VERDICT_REJECT,
                        method=METHOD_LLM,
                        inbox_id=best.inbox_id,
                        confidence=_dec(best.confidence),
                        evidence_sig=sig,
                        run_id=run_id,
                    )
                    stats.llm_rejected += 1
                continue
            # Evaluada completa sin veredicto: memo `dejar` + marcar que la IA la miró (inferred).
            conn.execute(
                text("UPDATE relation_edges SET provenance = :p WHERE id = :id"),
                {"p": PROVENANCE_INFERRED, "id": e.id},
            )
            record_decision(
                conn,
                user_id,
                e.id,
                verdict=VERDICT_DEJAR,
                method=METHOD_LLM,
                evidence_sig=sig,
                run_id=run_id,
            )
            stats.llm_dejar += 1
        if dirty_refs:
            mark_vertices_dirty(conn, user_id, sorted(dirty_refs, key=lambda r: (r.slug, r.id)))
