"""Resolver PAR-POR-PAR del long-tail de co-ocurrencias: veredicto por arista con grounding.

El partidor de cúmulos (`clusters_llm`) decide pistas EN CONTEXTO (por vecindario denso); las que
caen en barrios chicos, rechazados o con miembros podados quedan `pista` para siempre — sin que
nadie abra el mensaje para distinguir un recibo (relación lícita) de un newsletter (co-aparición).
Este módulo cierra ese hueco: toma un GRUPO de vértices (un cúmulo real, la componente de un
vértice, o las componentes conexas del subgrafo de pistas no resueltas) y decide cada pista
interna `confirm`/`reject`/`dejar`.

Cascada con presupuesto (determinismo primero, patrón FrugalGPT/Snorkel):
1. PREFILTRO determinista por TIPO de mensaje (labeling functions): un par cuya evidencia incluye
   un RECIBO (mensaje que produjo una transacción de finanzas) se confirma por regla — el dato
   real ya vouchó el contexto. Las señales de correo masivo (`classifier/rules.py`) son PRIOR y
   contexto, NO veredicto (el dueño advirtió el sesgo: el sistema no SABE que algo es newsletter);
   `resolve_reject_bulk` (off) habilita el rechazo determinista de pares todo-bulk si se quiere.
2. ZONA GRIS al LLM (`resolve_llm`), agrupada POR MENSAJE (una llamada decide todos los pares
   grises de ese mensaje) con cita textual OBLIGATORIA verificada por el grounder compartido.

Historial (NELL candidate→promoted / Wikidata references): el veredicto es la transición monótona
del MISMO edge (`resolve_edge`, evidencia original intacta) + una fila en
`relation_edge_decisions` con el fundamento. `dejar` no transiciona: es memo por `memo_signature`
(evidencia + resúmenes vigentes del summarizer — aparecer o cambiar un resumen reabre el memo UNA
vez; sin cambio no se re-gasta LLM). Los terminales registran la sig PLANA (`evidence_signature`),
que es la que compara el reporte de staleness. Idempotente: terminales salen del universo,
`dejar` se salta por sig, presupuesto corto NO memoiza (queda pendiente real).

STALENESS (reporte, no acción — capturar ≠ actuar): un edge ya decidido cuya evidencia CRECIÓ
(la procedencia sigue acumulándose en `relation_edge_sources` aun sobre terminales) se cuenta y
loguea; un `rejected` que ganó un mensaje RECIBO es el conflicto fuerte (warning explícito) — la
monotonía no permite reabrirlo automático, lo resuelve el dueño (`method='humano'`).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.classifier.rules import TIER_BLACKLIST, classify
from memex.config import settings
from memex.core.observability import CostAccum
from memex.core.source import SourceKind
from memex.db import connection
from memex.llm import LLMClient
from memex.logging import get_logger
from memex.relations.decisions import (
    METHOD_REGLA,
    VERDICT_CONFIRM,
    VERDICT_DEJAR,
    VERDICT_REJECT,
    edge_sources,
    evidence_signature,
    latest_decisions,
    memo_signature,
    record_decision,
)
from memex.relations.deterministic import vertex_inbox_ids
from memex.relations.edges import (
    CANAL_SLUG,
    PRODUCER_INBOX,
    RELTYPE_COOCURRENCIA,
    STATUS_CONFIRMED,
    STATUS_PISTA,
    STATUS_REJECTED,
    Ref,
    RelationEdge,
    list_edges,
    resolve_edge,
)
from memex.sources import kind_for_type
from memex.summarizer.lookup import InboxSummary, summaries_for_inboxes

_log = get_logger("memex.relations.resolve")

# --- Etiquetas del prefiltro (TIPO de mensaje; labeling functions deterministas) ------ #
LABEL_RECIBO = "recibo"  #: el mensaje produjo una transacción de finanzas (señal alta)
LABEL_BULK = "bulk"  #: marcadores de correo masivo (list_unsubscribe/precedence/...) — PRIOR
LABEL_CHAT = "chat"  #: mensaje de un medio de chat (SourceKind.CHAT)
LABEL_DESCONOCIDO = "desconocido"  #: sin señal determinista

#: rule de las decisiones por regla
RULE_RECIBO = "recibo"
RULE_BULK = "bulk"
RULE_SIN_EVIDENCIA = "sin_evidencia"


@dataclass(frozen=True)
class ResolvePair:
    """Una pista de co-ocurrencia con su evidencia: el edge + TODOS sus mensajes + las firmas
    (`sig` plana para veredictos terminales/staleness; `memo_sig` con resúmenes para `dejar`)."""

    edge: RelationEdge
    inbox_ids: frozenset[int]
    sig: str
    memo_sig: str


@dataclass
class ResolveStats:
    """Resumen de una corrida del resolver (los conteos de dry-run son PROYECCIONES)."""

    groups: int = 0  # grupos procesados (componentes / cúmulo / ego)
    pairs: int = 0  # pares considerados (tras saltar los `dejar` vigentes)
    skipped_dejar: int = 0  # pares saltados: memo `dejar` con la misma evidencia
    confirmed_recibo: int = 0  # confirmados por regla (recibo de finanzas)
    rejected_bulk: int = 0  # rechazados por regla (todo-bulk; solo con resolve_reject_bulk)
    sin_evidencia: int = 0  # memo `dejar`: el par ya no tiene mensajes de evidencia
    gray_pairs: int = 0  # pares que van a la zona gris LLM
    gray_messages: int = 0  # mensajes distintos de la zona gris
    llm_confirmed: int = 0  # confirmados por el LLM (cita grounded + confianza)
    llm_rejected: int = 0  # rechazados por el LLM (todos sus mensajes lo rechazan)
    llm_dejar: int = 0  # memo `dejar` del LLM (evaluado completo, sin veredicto)
    ungrounded: int = 0  # confirms del LLM degradados: cita ausente/corta/no hallada
    budget_exhausted: bool = False  # el presupuesto cortó antes de cubrir la zona gris
    estimated_calls: int = 0  # dry-run: llamadas LLM que haría (min(mensajes, budget))
    stale_conflicts: int = 0  # decididos cuya evidencia creció después del veredicto
    stale_recibo_conflicts: int = 0  # rechazados que ganaron un RECIBO (conflicto fuerte)
    errors: int = 0
    cost: CostAccum = field(default_factory=CostAccum)


# --- universo + evidencia ------------------------------------------------------------ #


def _load_universe(conn: Connection, user_id: int) -> list[RelationEdge]:
    """Las pistas resolubles: co-ocurrencia del inbox sin extremos `canal` (el canal es un hub
    estructural: une componentes artificialmente y su relación real ya es `participa_en`; sus
    pistas quedan para la vía del partidor, que las juzga en contexto)."""
    return [
        e
        for e in list_edges(conn, user_id, status=STATUS_PISTA, producer=PRODUCER_INBOX)
        if e.relation_type == RELTYPE_COOCURRENCIA
        and e.src.slug != CANAL_SLUG
        and e.dst.slug != CANAL_SLUG
    ]


def _attach_evidence(
    conn: Connection, user_id: int, universe: list[RelationEdge]
) -> tuple[list[ResolvePair], dict[int, InboxSummary]]:
    """Evidencia por par desde `relation_edge_sources`; fallback para pistas pre-backfill (sin
    filas aún): la intersección de la provenance de ambos extremos (`vertex_inbox_ids`).

    Devuelve también el SNAPSHOT de resúmenes de toda la evidencia (un solo fetch): la misma
    foto alimenta la `memo_sig` de cada par Y el bloque de contexto del prompt — un resumen
    creado a mitad de corrida no puede desfasarlos (entra recién en la próxima)."""
    srcs = edge_sources(conn, [e.id for e in universe])
    if any(not srcs.get(e.id) for e in universe):
        prov = vertex_inbox_ids(conn, user_id)
        for e in universe:
            if not srcs.get(e.id):
                srcs[e.id] = prov.get(e.src, set()) & prov.get(e.dst, set())
    summaries: dict[int, InboxSummary] = {}
    if settings.resolve_summary_max_chars > 0:
        all_ids = {m for ids in srcs.values() for m in ids}
        summaries = summaries_for_inboxes(conn, user_id, all_ids)
    sum_ids = {m: s.summary_id for m, s in summaries.items()}
    out: list[ResolvePair] = []
    for e in universe:
        ids = srcs.get(e.id, set())
        out.append(
            ResolvePair(e, frozenset(ids), evidence_signature(ids), memo_signature(ids, sum_ids))
        )
    return out, summaries


# --- formación de grupos -------------------------------------------------------------- #


def _components(pairs: list[ResolvePair]) -> list[list[ResolvePair]]:
    """Componentes conexas del subgrafo de pistas (union-find), ordenadas CHICAS primero (las de
    2 vértices jamás llegan al partidor: son exactamente la cola) con desempate determinista."""
    parent: dict[Ref, Ref] = {}

    def find(x: Ref) -> Ref:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs:
        ra, rb = find(p.edge.src), find(p.edge.dst)
        if ra != rb:
            parent[rb] = ra

    by_root: dict[Ref, list[ResolvePair]] = defaultdict(list)
    for p in pairs:
        by_root[find(p.edge.src)].append(p)

    def _vertices(group: list[ResolvePair]) -> set[Ref]:
        return {p.edge.src for p in group} | {p.edge.dst for p in group}

    groups = list(by_root.values())
    groups.sort(key=lambda g: (len(_vertices(g)), min((r.slug, r.id) for r in _vertices(g))))
    return groups


def _cluster_members(conn: Connection, user_id: int, cluster_id: int) -> set[Ref]:
    """Los miembros VIVOS (no podados) de un cúmulo persistido."""
    return {
        Ref(str(r["member_slug"]), int(r["member_id"]))
        for r in conn.execute(
            text(
                "SELECT member_slug, member_id FROM relation_cluster_members "
                "WHERE user_id = :u AND cluster_id = :c AND NOT pruned"
            ),
            {"u": user_id, "c": cluster_id},
        ).mappings()
    }


def _select_groups(
    conn: Connection,
    user_id: int,
    pairs: list[ResolvePair],
    *,
    cluster_id: int | None,
    vertex: Ref | None,
    limit: int | None,
) -> list[list[ResolvePair]]:
    """El/los grupos a procesar según el modo: `--cluster` (pistas internas al cúmulo),
    `--vertex` (la componente que lo contiene) o auto (componentes chicas primero, con corte)."""
    if cluster_id is not None:
        members = _cluster_members(conn, user_id, cluster_id)
        group = [p for p in pairs if p.edge.src in members and p.edge.dst in members]
        return [group] if group else []
    if vertex is not None:
        for g in _components(pairs):
            if any(vertex in (p.edge.src, p.edge.dst) for p in g):
                return [g]
        return []
    cut = limit if limit is not None else settings.resolve_group_limit
    return _components(pairs)[:cut]


# --- prefiltro determinista (labeling functions) -------------------------------------- #


class MessageLabeler:
    """Etiqueta el TIPO de cada mensaje con señales deterministas, precargadas en bloque:
    RECIBO (produjo una transacción de finanzas) > BULK (marcadores de correo masivo, vía
    `classifications` con fallback a `classifier.rules.classify` si el job nunca corrió) >
    CHAT (medio de chat) > DESCONOCIDO."""

    def __init__(self, conn: Connection, user_id: int, inbox_ids: Iterable[int]) -> None:
        self._labels: dict[int, str] = {}
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
            if mid in recibo:
                self._labels[mid] = LABEL_RECIBO
                continue
            tier = tiers.get(mid)
            if tier is None:
                tier = classify(dict(r["payload"])).tier
            if tier == TIER_BLACKLIST:
                self._labels[mid] = LABEL_BULK
                continue
            try:
                kind = kind_for_type(str(r["type"]))
            except KeyError:
                kind = None
            self._labels[mid] = LABEL_CHAT if kind == SourceKind.CHAT else LABEL_DESCONOCIDO

    def label(self, inbox_id: int) -> str:
        """La etiqueta del mensaje (un id fuera del precargado — p.ej. inbox purgado — cae a
        DESCONOCIDO: sin señal)."""
        return self._labels.get(inbox_id, LABEL_DESCONOCIDO)


#: Resultado del prefiltro por par.
_PRE_CONFIRM = "confirm"
_PRE_REJECT = "reject"
_PRE_GRIS = "gris"
_PRE_SIN_EVIDENCIA = "sin_evidencia"


def _prefilter(pair: ResolvePair, labeler: MessageLabeler) -> str:
    """Veredicto determinista del par, si lo hay: algún RECIBO → confirm; todo-BULK → reject SOLO
    con el knob `resolve_reject_bulk` (las señales bulk son prior, no veredicto); evidencia vacía
    → sin_evidencia (memo). El resto es zona gris."""
    if not pair.inbox_ids:
        return _PRE_SIN_EVIDENCIA
    labels = {labeler.label(m) for m in pair.inbox_ids}
    if LABEL_RECIBO in labels:
        return _PRE_CONFIRM
    if settings.resolve_reject_bulk and labels == {LABEL_BULK}:
        return _PRE_REJECT
    return _PRE_GRIS


def _recibo_message(pair: ResolvePair, labeler: MessageLabeler) -> int:
    """El mensaje-recibo que fundamenta el confirm (el menor id, determinista)."""
    return min(m for m in pair.inbox_ids if labeler.label(m) == LABEL_RECIBO)


# --- staleness / conflictos (reporte, no acción) --------------------------------------- #


def _report_stale_terminals(
    conn: Connection, user_id: int, labeler: MessageLabeler
) -> tuple[int, int]:
    """Cuenta (y loguea) los edges YA DECIDIDOS cuya evidencia creció después del veredicto
    (la sig actual de sus sources difiere de la `evidence_sig` de su última decisión). El
    conflicto FUERTE: un `rejected` que ahora tiene un mensaje RECIBO — la monotonía no lo
    reabre; queda para veredicto manual (`method='humano'`). Devuelve (conflictos, fuertes)."""
    terminals = [
        e
        for e in list_edges(conn, user_id, producer=PRODUCER_INBOX)
        if e.relation_type == RELTYPE_COOCURRENCIA
        and e.status in (STATUS_CONFIRMED, STATUS_REJECTED)
        and e.src.slug != CANAL_SLUG
        and e.dst.slug != CANAL_SLUG
    ]
    if not terminals:
        return (0, 0)
    decisions = latest_decisions(conn, user_id, [e.id for e in terminals])
    srcs = edge_sources(conn, [e.id for e in terminals])
    stale = strong = 0
    for e in terminals:
        d = decisions.get(e.id)
        if d is None:  # decidido antes del historial: sin baseline para comparar
            continue
        current = srcs.get(e.id, set())
        if evidence_signature(current) == d.evidence_sig:
            continue
        stale += 1
        has_recibo = any(labeler.label(m) == LABEL_RECIBO for m in current)
        if e.status == STATUS_REJECTED and has_recibo:
            strong += 1
            _log.warning(
                "relation.resolve.stale_recibo_conflict",
                edge_id=e.id,
                src=f"{e.src.slug}:{e.src.id}",
                dst=f"{e.dst.slug}:{e.dst.id}",
                inbox_ids=sorted(current),
            )
        else:
            _log.info("relation.resolve.stale_evidence", edge_id=e.id, status=e.status)
    return (stale, strong)


# --- worker ----------------------------------------------------------------------------- #


def _parse_vertex(raw: str) -> Ref:
    """`slug:id` → Ref (el slug puede contener `:`, p.ej. `identidades:person:7` → rsplit)."""
    slug, sep, sid = raw.rpartition(":")
    if not sep or not slug or not sid.isdigit():
        raise ValueError(f"vértice inválido: {raw!r} (esperado slug:id)")
    return Ref(slug, int(sid))


async def run_resolve(
    user_id: int,
    *,
    cluster_id: int | None = None,
    vertex: Ref | None = None,
    limit: int | None = None,
    max_llm_calls: int | None = None,
    dry_run: bool = False,
    no_llm: bool = False,
    client: LLMClient | None = None,
) -> ResolveStats:
    """Corre el resolver sobre el/los grupos elegidos. Idempotente: re-correr sin evidencia nueva
    es no-op (terminales fuera del universo, `dejar` saltado por sig). `dry_run` clasifica sin
    escribir NADA (los conteos son proyecciones) y estima las llamadas; `no_llm` aplica solo el
    prefiltro (la zona gris queda pendiente, sin memo). `client` inyectable (tests con fake).
    `LLMQuotaError` PROPAGA (tras aplicar lo ya pagado)."""
    stats = ResolveStats()
    run_id = uuid.uuid4().hex
    budget = max_llm_calls if max_llm_calls is not None else settings.resolve_max_llm_calls

    with connection() as conn:
        universe = _load_universe(conn, user_id)
        pairs_all, summaries = _attach_evidence(conn, user_id, universe)
        decisions = latest_decisions(conn, user_id, [p.edge.id for p in pairs_all])
        work: list[ResolvePair] = []
        for p in pairs_all:
            d = decisions.get(p.edge.id)
            if d is not None and d.verdict == VERDICT_DEJAR and d.evidence_sig == p.memo_sig:
                stats.skipped_dejar += 1
                continue
            work.append(p)
        groups = _select_groups(
            conn, user_id, work, cluster_id=cluster_id, vertex=vertex, limit=limit
        )
        selected = sorted((p for g in groups for p in g), key=lambda p: p.edge.id)
        stats.groups = len(groups)
        stats.pairs = len(selected)
        evidence_ids: set[int] = set()
        for p in selected:
            evidence_ids |= p.inbox_ids
        # El labeler cubre la evidencia seleccionada Y la de los terminales (para el reporte).
        terminal_ids = {
            m
            for ids in edge_sources(
                conn,
                [
                    e.id
                    for e in list_edges(conn, user_id, producer=PRODUCER_INBOX)
                    if e.relation_type == RELTYPE_COOCURRENCIA and e.status != STATUS_PISTA
                ],
            ).values()
            for m in ids
        }
        labeler = MessageLabeler(conn, user_id, evidence_ids | terminal_ids)
        stats.stale_conflicts, stats.stale_recibo_conflicts = _report_stale_terminals(
            conn, user_id, labeler
        )

    # --- FASE 1: prefiltro determinista --------------------------------------------- #
    confirms: list[tuple[ResolvePair, int]] = []  # (par, mensaje-recibo)
    rejects: list[ResolvePair] = []
    sin_evidencia: list[ResolvePair] = []
    gray: list[ResolvePair] = []
    for p in selected:
        verdict = _prefilter(p, labeler)
        if verdict == _PRE_CONFIRM:
            confirms.append((p, _recibo_message(p, labeler)))
        elif verdict == _PRE_REJECT:
            rejects.append(p)
        elif verdict == _PRE_SIN_EVIDENCIA:
            sin_evidencia.append(p)
        else:
            gray.append(p)

    stats.gray_pairs = len(gray)
    gray_msgs = {m for p in gray for m in p.inbox_ids}
    stats.gray_messages = len(gray_msgs)

    if dry_run:
        stats.confirmed_recibo = len(confirms)
        stats.rejected_bulk = len(rejects)
        stats.sin_evidencia = len(sin_evidencia)
        stats.estimated_calls = min(len(gray_msgs), budget)
        _log.info(
            "relation.resolve.dry_run",
            user_id=user_id,
            groups=stats.groups,
            pairs=stats.pairs,
            confirm_recibo=stats.confirmed_recibo,
            reject_bulk=stats.rejected_bulk,
            sin_evidencia=stats.sin_evidencia,
            gray_pairs=stats.gray_pairs,
            estimated_calls=stats.estimated_calls,
        )
        return stats

    with connection() as conn:
        for p, mid in confirms:
            if resolve_edge(conn, p.edge.id, status=STATUS_CONFIRMED):
                record_decision(
                    conn,
                    user_id,
                    p.edge.id,
                    verdict=VERDICT_CONFIRM,
                    method=METHOD_REGLA,
                    rule=RULE_RECIBO,
                    inbox_id=mid,
                    evidence_sig=p.sig,
                    run_id=run_id,
                )
                stats.confirmed_recibo += 1
        for p in rejects:
            if resolve_edge(conn, p.edge.id, status=STATUS_REJECTED):
                record_decision(
                    conn,
                    user_id,
                    p.edge.id,
                    verdict=VERDICT_REJECT,
                    method=METHOD_REGLA,
                    rule=RULE_BULK,
                    evidence_sig=p.sig,
                    run_id=run_id,
                )
                stats.rejected_bulk += 1
        for p in sin_evidencia:
            record_decision(
                conn,
                user_id,
                p.edge.id,
                verdict=VERDICT_DEJAR,
                method=METHOD_REGLA,
                rule=RULE_SIN_EVIDENCIA,
                evidence_sig=p.memo_sig,
                run_id=run_id,
            )
            stats.sin_evidencia += 1

    # --- FASE 2: zona gris al LLM (presupuestada) ------------------------------------ #
    if gray and not no_llm:
        # Import local: resolve_llm importa ResolvePair de acá (evita el ciclo en frío).
        from memex.relations.resolve_llm import resolve_gray_zone

        await resolve_gray_zone(
            user_id,
            gray,
            labeler=labeler,
            summaries=summaries,
            budget=budget,
            run_id=run_id,
            stats=stats,
            client=client,
        )

    _log.info(
        "relation.resolve.done",
        user_id=user_id,
        groups=stats.groups,
        pairs=stats.pairs,
        skipped_dejar=stats.skipped_dejar,
        confirmed_recibo=stats.confirmed_recibo,
        rejected_bulk=stats.rejected_bulk,
        sin_evidencia=stats.sin_evidencia,
        gray_pairs=stats.gray_pairs,
        gray_messages=stats.gray_messages,
        llm_confirmed=stats.llm_confirmed,
        llm_rejected=stats.llm_rejected,
        llm_dejar=stats.llm_dejar,
        ungrounded=stats.ungrounded,
        budget_exhausted=stats.budget_exhausted,
        stale_conflicts=stats.stale_conflicts,
        stale_recibo_conflicts=stats.stale_recibo_conflicts,
        errors=stats.errors,
        llm_calls=stats.cost.calls,
        llm_cost_usd=str(stats.cost.cost_usd),
    )
    return stats
