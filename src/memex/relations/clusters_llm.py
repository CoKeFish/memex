"""Validador LLM de CÚMULOS: por cada cúmulo candidato (o que requiere re-validación), una
llamada al LLM que revisa sus VÉRTICES y sus ARISTAS internas y devuelve veredicto + confianza +
nombre + descripción + poda. Es el decisor (Fase 4) a nivel cúmulo.

Y es TAMBIÉN el resolvedor de aristas (Fase 4 cableado EN CONTEXTO, no de a pares): al confirmar un
cúmulo, sus PISTAS internas de co-ocurrencia se promueven en cascada a `confirmed` (con la confianza
del cúmulo); al podar un miembro o rechazar el cúmulo, esas pistas se `rejected`. Una sola llamada
amortiza todas las aristas internas. Las aristas `confirmed` deterministas NUNCA se tocan (ver
`_cascade_edges`).

Molde: `modules/identidades/relations_llm.py` (loop best-effort por unidad, una llamada por cúmulo,
cliente inyectable, parseo ultra-defensivo, `LLMQuotaError` PROPAGA). Idempotente: solo toca cúmulos
`candidate`/`needs_revalidation`; un confirmado estable no se re-juzga (costo acotado). Al final
materializa las aristas `miembro_de` (idempotente + GC). Sin `attach_to_root` (un cúmulo NO es
per-mensaje: el costo se atribuye por `purpose`, patrón calendar/finance).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.config import settings
from memex.core.observability import CostAccum, record_llm_call
from memex.db import connection
from memex.llm import ChatMessage, LLMClient, LLMResult, aclose_llm, build_llm_client
from memex.llm.client import LLMQuotaError
from memex.logging import get_logger
from memex.relations.cluster_store import (
    StoredCluster,
    create_child,
    delete_cluster,
    load_clusters,
    mark_dissolved,
    materialize_cluster_edges,
    reject_cluster,
    sync_child,
)
from memex.relations.decisions import (
    METHOD_PARTIDOR,
    VERDICT_CONFIRM,
    VERDICT_REJECT,
    edge_sources,
    evidence_signature,
    record_decision,
)
from memex.relations.edges import (
    PRODUCER_INBOX,
    RELTYPE_COOCURRENCIA,
    RELTYPE_MIEMBRO_DE,
    STATUS_CONFIRMED,
    STATUS_PISTA,
    STATUS_REJECTED,
    Ref,
    RelationEdge,
    list_edges,
    resolve_edge,
)
from memex.relations.prompt import GRAPH_CLUSTER_PARTITION_SYSTEM_PROMPT
from memex.relations.vertices import Vertex, list_vertices

_log = get_logger("memex.relations.clusters_llm")

#: Cota de tokens de salida (un veredicto + nombre + descripción + lista de poda es chico).
_MAX_TOKENS = 2048
#: Tope de aristas internas serializadas (confirmed-primero) para acotar el prompt de cúmulos.
_INTERNAL_EDGE_CAP = 300


@dataclass
class ClusterPartitionStats:
    """Resumen de una corrida del PARTIDOR."""

    blobs: int = 0  # blobs con llamada LLM (particionados)
    groups: int = 0  # contextos (hijos confirmed) creados o sincronizados
    created: int = 0  # hijos nuevos
    synced: int = 0  # hijos actualizados EN SITIO (identidad preservada al crecer)
    dissolved: int = 0  # hijos disueltos (su contexto desapareció del blob)
    rejected: int = 0  # blobs todo-ruido (memo de rechazo)
    promoted: int = 0  # pistas intra-grupo promovidas a confirmed
    rejected_edges: int = 0  # pistas rechazadas por veredicto EXPLÍCITO del LLM (rejected_edges)
    skipped: int = 0  # blobs saltados (serialización > tope)
    errors: int = 0
    cost: CostAccum = field(default_factory=CostAccum)


@dataclass(frozen=True)
class _Pending:
    """Un cúmulo pendiente de validar + sus miembros vivos (id local = índice + 1)."""

    id: int
    signature: str
    members: list[Ref]


# --- carga (sin LLM) --------------------------------------------------------------- #


def _load_pending(conn: Connection, user_id: int, limit: int) -> list[_Pending]:
    """Blobs `candidate` a particionar, los más grandes primero. Miembros ordenados → id local
    estable (1..n). `signature` de un candidate = la firma del BLOB."""
    rows = (
        conn.execute(
            text(
                "SELECT id, signature FROM relation_clusters "
                "WHERE user_id = :u AND status = 'candidate' "
                "ORDER BY member_count DESC, id LIMIT :lim"
            ),
            {"u": user_id, "lim": limit},
        )
        .mappings()
        .all()
    )
    pending: list[_Pending] = []
    for r in rows:
        members = [
            Ref(str(m["member_slug"]), int(m["member_id"]))
            for m in conn.execute(
                text(
                    "SELECT member_slug, member_id FROM relation_cluster_members "
                    "WHERE cluster_id = :c AND NOT pruned ORDER BY member_slug, member_id"
                ),
                {"c": int(r["id"])},
            ).mappings()
        ]
        pending.append(_Pending(int(r["id"]), str(r["signature"]), members))
    return pending


def _internal_edges(all_edges: list[RelationEdge], members: set[Ref]) -> list[RelationEdge]:
    """Aristas del grafo entre miembros del cúmulo (excluye `miembro_de` y rechazadas), confirmed
    primero, con tope `_INTERNAL_EDGE_CAP`."""
    internal = [
        e
        for e in all_edges
        if e.relation_type != RELTYPE_MIEMBRO_DE
        and e.status != STATUS_REJECTED
        and e.src in members
        and e.dst in members
    ]
    internal.sort(key=lambda e: 0 if e.status == STATUS_CONFIRMED else 1)
    return internal[:_INTERNAL_EDGE_CAP]


# --- serialización + parseo -------------------------------------------------------- #


def _serialize(members: list[Ref], internal: list[RelationEdge], vmap: dict[Ref, Vertex]) -> str:
    """Arma el cuerpo con los DOS bloques que el LLM revisa: VÉRTICES (id local, tipo, etiqueta) y
    ARISTAS internas (a-b, relación, producer, nivel, evidencia)."""
    local = {ref: i + 1 for i, ref in enumerate(members)}
    lines = ["VÉRTICES (id: tipo — etiqueta):"]
    for ref in members:
        v = vmap.get(ref)
        kind = v.kind if v is not None else ref.slug
        label = v.label if v is not None else "(?)"
        lines.append(f"{local[ref]}: {kind} — {label}")
    lines.append("")
    lines.append("ARISTAS internas (a-b: relación · producer · nivel · evidencia):")
    if not internal:
        lines.append("(ninguna)")
    for e in internal:
        a, b = local.get(e.src), local.get(e.dst)
        if a is None or b is None:
            continue
        rt = e.relation_type or "—"
        ev = f" · {e.evidence}" if e.evidence else ""
        lines.append(f"{a}-{b}: {rt} · {e.producer} · {e.status}{ev}")
    return "\n".join(lines)


# --- PARTIDOR (Fase 2): una llamada parte el blob en los N contextos que tenga ------ #


@dataclass(frozen=True)
class PartitionGroup:
    """Un contexto descubierto: ids locales (1..n) de sus miembros + metadatos del LLM."""

    members: tuple[int, ...]
    name: str
    description: str
    confidence: float


@dataclass(frozen=True)
class PartitionResult:
    """Partición parseada. `valid=False` = JSON inválido (noop/reintenta, NO memoiza);
    `valid=True, groups=()` = ruido (sin contexto coherente → memo de rechazo).
    `rejected_pairs` = aristas-pista que el LLM marcó como NO-relación (pares de ids locales,
    canónicos `a<b`); campo OPCIONAL de la respuesta — ausente = `()` = comportamiento previo."""

    groups: tuple[PartitionGroup, ...]
    valid: bool
    rejected_pairs: tuple[tuple[int, int], ...] = ()


def _clean_member_ids(raw: object, n: int) -> list[int]:
    """ids locales válidos (1..n), sin bool-como-int ni duplicados, preservando orden."""
    out: list[int] = []
    seen: set[int] = set()
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= n and x not in seen:
                seen.add(x)
                out.append(x)
    return out


def _clean_rejected_pairs(raw: object, n: int) -> tuple[tuple[int, int], ...]:
    """Pares `"a-b"` de `rejected_edges` válidos: ids locales 1..n, `a != b`, canónicos `(min,
    max)`, dedup preservando orden. La basura se IGNORA sin invalidar la partición (molde
    `_clean_member_ids`)."""
    out: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    if not isinstance(raw, list):
        return ()
    for x in raw:
        if not isinstance(x, str):
            continue
        a_s, sep, b_s = x.partition("-")
        if not sep:
            continue
        try:
            a, b = int(a_s), int(b_s)
        except ValueError:
            continue
        if not (1 <= a <= n and 1 <= b <= n) or a == b:
            continue
        pair = (a, b) if a < b else (b, a)
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return tuple(out)


def parse_partition(content: str, n_members: int) -> PartitionResult:
    """Parsea `{groups:[{members,name,description,confidence}], rejected_edges:["a-b",...]}`.
    ULTRA-DEFENSIVO: basura → `valid=False` (no memoiza). Normaliza determinista: ids 1..n sin
    bool/duplicados; ordena los grupos por su id mínimo y asigna cada vértice al PRIMER grupo que
    lo lista (dedup entre grupos); descarta grupos de < 2 miembros; clampa confianza 0..1.
    `valid=True, groups=()` = ruido. `rejected_edges` es opcional (ausente/basura → `()`)."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return PartitionResult((), False)
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        return PartitionResult((), False)
    parsed: list[tuple[list[int], str, str, float]] = []
    for g in data["groups"]:
        if not isinstance(g, dict):
            continue
        members = _clean_member_ids(g.get("members"), n_members)
        if not members:
            continue
        name = g.get("name")
        desc = g.get("description")
        conf = g.get("confidence")
        confidence = (
            float(conf) if isinstance(conf, int | float) and not isinstance(conf, bool) else 0.0
        )
        parsed.append(
            (
                members,
                name.strip()[:200] if isinstance(name, str) else "",
                desc.strip()[:1000] if isinstance(desc, str) else "",
                max(0.0, min(1.0, confidence)),
            )
        )
    parsed.sort(key=lambda t: min(t[0]))  # orden canónico → asignación determinista
    used: set[int] = set()
    groups: list[PartitionGroup] = []
    for members, name, desc, confidence in parsed:
        own = [m for m in sorted(members) if m not in used]
        if len(own) < 2:  # un split legítimo es ≥ 2; los singletons quedan afuera (pista)
            continue
        used.update(own)
        groups.append(PartitionGroup(tuple(own), name, desc, confidence))
    return PartitionResult(
        tuple(groups), True, _clean_rejected_pairs(data.get("rejected_edges"), n_members)
    )


async def partition_cluster(
    llm: LLMClient, members: list[Ref], internal: list[RelationEdge], vmap: dict[Ref, Vertex]
) -> tuple[PartitionResult, LLMResult]:
    """Una llamada al LLM que PARTE un blob en sus contextos. Devuelve la partición + LLMResult."""
    result = await llm.complete(
        [
            ChatMessage("system", GRAPH_CLUSTER_PARTITION_SYSTEM_PROMPT),
            ChatMessage("user", _serialize(members, internal, vmap)),
        ],
        response_format="json_object",
        temperature=0.0,
        max_tokens=_MAX_TOKENS,
    )
    return parse_partition(result.content, len(members)), result


# --- aplicación del veredicto ------------------------------------------------------ #


@dataclass(frozen=True)
class _Applied:
    """Resultado de aplicar un veredicto: outcome + conteos de la cascada de aristas."""

    outcome: str  # 'keep' | 'reject' | 'noop'
    pruned: int = 0
    promoted: int = 0
    rejected_edges: int = 0


def _cascade_edges(
    conn: Connection,
    user_id: int,
    cluster_id: int,
    internal: list[RelationEdge],
    *,
    survivors: frozenset[Ref] | None,
    confidence: float,
    rejected_pairs: frozenset[frozenset[Ref]] = frozenset(),
) -> tuple[int, int]:
    """Promueve/rechaza en cascada las PISTAS internas de co-ocurrencia (las ÚNICAS promovibles; las
    aristas `confirmed` deterministas —contraparte/afiliado/cumple/…— NUNCA se tocan, por eso el
    filtro estricto `pista + producer=inbox + co-ocurrencia`). Es el decisor de Fase 4 cableado EN
    CONTEXTO: una pista la confirma el juicio del cúmulo, no de a pares.

    NO-DESTRUCTIVO: solo CONFIRMA lo que el LLM avala; nada se mata por no encajar.
    - `rejected_pairs` (veredicto EXPLÍCITO del LLM, `rejected_edges` de la respuesta): esa pista
      es una NO-relación → `rejected` (terminal en la arista misma: status + decided_at). Es la
      única poda a nivel arista; va ANTES del juicio por membresía.
    - `survivors` set (cúmulo confirmado): pista entre dos sobrevivientes → `confirmed` (estampa la
      confianza del cúmulo); pista que toca un PODADO → se DEJA `pista` (el LLM dijo "no pertenece
      a ESTE cúmulo", no "no es relación"; queda para otro contexto).
    - `survivors=None` (cúmulo rechazado): toda pista interna → `rejected` (el caller ya consultó
      `cluster_reject_pistas`; el rechazo terminal es opt-in, solo para ruido explícito).

    HISTORIAL: el `evidence` original (`inbox:N`) NO se pisa (antes se reescribía con
    `cluster:{id}` y destruía la procedencia); el veredicto del partidor queda como fila en
    `relation_edge_decisions` (`method='partidor'`, `rule='cluster:{id}'`) con la sig de la
    evidencia del par al decidir. `resolve_edge` es monótono (no re-evalúa terminales) e
    idempotente: con varios grupos del mismo blob, el par rechazado se cuenta UNA vez (la decisión
    se inserta solo cuando la transición ocurrió). Devuelve (promovidas, rechazadas)."""
    conf = Decimal(str(round(confidence, 3)))
    rule = f"cluster:{cluster_id}"
    pistas = [
        e
        for e in internal
        if e.status == STATUS_PISTA
        and e.producer == PRODUCER_INBOX
        and e.relation_type == RELTYPE_COOCURRENCIA
    ]
    sources = edge_sources(conn, [e.id for e in pistas])

    def _decide(edge_id: int, verdict: str) -> None:
        record_decision(
            conn,
            user_id,
            edge_id,
            verdict=verdict,
            method=METHOD_PARTIDOR,
            rule=rule,
            confidence=conf,
            evidence_sig=evidence_signature(sources.get(edge_id, set())),
        )

    promoted = rejected = 0
    for e in pistas:
        if frozenset((e.src, e.dst)) in rejected_pairs:  # absorbe la orientación src/dst
            if resolve_edge(conn, e.id, status=STATUS_REJECTED):
                _decide(e.id, VERDICT_REJECT)
                rejected += 1
            continue
        if survivors is None:  # cúmulo rechazado (caller gateó cluster_reject_pistas)
            if resolve_edge(conn, e.id, status=STATUS_REJECTED):
                _decide(e.id, VERDICT_REJECT)
                rejected += 1
        elif (
            e.src in survivors
            and e.dst in survivors
            and resolve_edge(conn, e.id, status=STATUS_CONFIRMED, confidence=conf)
        ):
            _decide(e.id, VERDICT_CONFIRM)
            promoted += 1
        # pista que toca un miembro PODADO: se DEJA como pista (no se mata)
    return promoted, rejected


def _jaccard(a: frozenset[Ref], b: frozenset[Ref]) -> float:
    union = len(a | b)
    return len(a & b) / union if union else 1.0


def _group_has_confirmed(members: frozenset[Ref], internal: list[RelationEdge]) -> bool:
    """¿El grupo tiene una arista confirmed REAL entre dos miembros (ancla, no co-ocurrencia)?"""
    return any(
        e.status == STATUS_CONFIRMED
        and e.relation_type != RELTYPE_COOCURRENCIA
        and e.src in members
        and e.dst in members
        for e in internal
    )


def _greedy_match_groups(
    groups: list[tuple[frozenset[Ref], PartitionGroup]], children: list[StoredCluster]
) -> dict[int, int]:
    """Match codicioso 1-a-1 `group → child` por Jaccard ≥ `cluster_match_jaccard`: preserva la
    IDENTIDAD de un hijo cuando el contexto re-particionado es (casi) el mismo set. Determinista:
    `(jaccard desc, group_idx, child_id)`."""
    cands: list[tuple[float, int, int]] = []
    for gi, (gmembers, _g) in enumerate(groups):
        for c in children:
            j = _jaccard(gmembers, c.live_members)
            if j >= settings.cluster_match_jaccard:
                cands.append((j, gi, c.id))
    cands.sort(key=lambda t: (-t[0], t[1], t[2]))
    matched: dict[int, int] = {}
    used: set[int] = set()
    for _j, gi, cid in cands:
        if gi not in matched and cid not in used:
            matched[gi] = cid
            used.add(cid)
    return matched


def _apply_partition(
    conn: Connection,
    user_id: int,
    pc: _Pending,
    part: PartitionResult,
    internal: list[RelationEdge],
) -> tuple[int, int, int, int, int, int]:
    """Aplica la partición de UN blob en su tx. Cada contexto con confianza ≥ umbral crea un hijo
    confirmed NUEVO o sincroniza EN SITIO el hijo que matchea (identidad preservada al crecer);
    promueve sus pistas intra-grupo y RECHAZA las que el LLM vetó explícitamente
    (`rejected_pairs`); disuelve los hijos viejos del blob que ningún contexto reclama; consume el
    candidato. Sin contexto confiado → memo de rechazo (ahí los `rejected_pairs` NO se aplican: el
    memo es no-destructivo y no hay cascada). JSON basura → no-op (el candidate queda, se
    reintenta). Devuelve `(created, synced, dissolved, promoted, rejected, rejected_edges)`."""
    if not part.valid:
        return (0, 0, 0, 0, 0, 0)
    blob_sig = pc.signature
    gate = settings.cluster_partition_min_confidence
    groups: list[tuple[frozenset[Ref], PartitionGroup]] = [
        (frozenset(pc.members[i - 1] for i in g.members), g)
        for g in part.groups
        if g.confidence >= gate
    ]
    blob_set = set(pc.members)
    overlapping = [
        c for c in load_clusters(conn, user_id, ("confirmed", "stale")) if c.live_members & blob_set
    ]
    if not groups:  # blob sin contexto confiado → ruido: memo + disolver los hijos viejos del blob.
        for c in overlapping:
            mark_dissolved(conn, user_id, c.id)
        reject_cluster(conn, user_id, pc.id, blob_sig)
        return (0, 0, len(overlapping), 0, 1, 0)
    # Los ids locales de `rejected_pairs` ya vienen validados 1..n (n = len(pc.members), el mismo
    # n que vio parse_partition). frozenset de 2 Refs → la orientación src/dst no importa.
    rej: frozenset[frozenset[Ref]] = frozenset(
        frozenset((pc.members[a - 1], pc.members[b - 1])) for a, b in part.rejected_pairs
    )
    matched = _greedy_match_groups(groups, overlapping)
    matched_children: set[int] = set()
    created = synced = promoted = rejected_edges = 0
    for gi, (gmembers, g) in enumerate(groups):
        if gi in matched:
            cid = matched[gi]
            sync_child(conn, user_id, cid, blob_sig, gmembers, confidence=g.confidence)
            matched_children.add(cid)
            synced += 1
        else:
            cid = create_child(
                conn,
                user_id,
                blob_sig,
                gmembers,
                name=g.name,
                description=g.description,
                confidence=g.confidence,
                has_confirmed_edge=_group_has_confirmed(gmembers, internal),
            )
            created += 1
        p, r = _cascade_edges(
            conn,
            user_id,
            cid,
            internal,
            survivors=gmembers,
            confidence=g.confidence,
            rejected_pairs=rej,
        )
        promoted += p
        rejected_edges += r
    dissolved = 0
    for c in overlapping:  # hijo viejo del blob que ningún contexto reclamó → su contexto se fue.
        if c.id not in matched_children:
            mark_dissolved(conn, user_id, c.id)
            dissolved += 1
    delete_cluster(conn, pc.id)  # consume el candidato; los hijos llevan su blob_signature
    return (created, synced, dissolved, promoted, 0, rejected_edges)


# --- worker ------------------------------------------------------------------------ #


async def run_cluster_partition(
    user_id: int, *, limit: int | None = None, client: LLMClient | None = None
) -> ClusterPartitionStats:
    """Parte con el LLM los blobs `candidate` del user: cada blob → N contextos (hijos confirmed),
    preservando la identidad de los hijos al re-particionar (sync en sitio), promoviendo las pistas
    intra-grupo y dejando el resto como pista. Al final materializa `miembro_de`. `client`
    inyectable (tests con fake). `LLMQuotaError` PROPAGA. Idempotente (un blob ya particionado no es
    candidate)."""
    limit = limit if limit is not None else settings.cluster_validate_limit
    stats = ClusterPartitionStats()
    with connection() as conn:
        pending = _load_pending(conn, user_id, limit)
        vmap = {v.ref: v for v in list_vertices(conn, user_id)}
        all_edges = list_edges(conn, user_id)
    if not pending:
        _log.info("relation.cluster.partition.empty", user_id=user_id)
        return stats

    owns_client = client is None
    llm: LLMClient = client or build_llm_client("relations_clusters", user_id=user_id)
    _log.info("relation.cluster.partition.start", user_id=user_id, pending=len(pending))
    try:
        for pc in pending:
            if len(pc.members) > settings.cluster_max_members:
                stats.skipped += 1
                _log.info(
                    "relation.cluster.partition.skip_too_big",
                    cluster_id=pc.id,
                    members=len(pc.members),
                )
                continue
            internal = _internal_edges(all_edges, set(pc.members))
            try:
                part, result = await partition_cluster(llm, pc.members, internal, vmap)
            except LLMQuotaError:
                raise  # propaga: corta el resto del run
            except Exception as e:  # best-effort: un blob fallido no frena los demás
                stats.errors += 1
                _log.error(
                    "relation.cluster.partition.failed",
                    cluster_id=pc.id,
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
                continue
            stats.blobs += 1
            with connection() as conn:
                created, synced, dissolved, promoted, rejected, rejected_edges = _apply_partition(
                    conn, user_id, pc, part, internal
                )
            stats.created += created
            stats.synced += synced
            stats.groups += created + synced
            stats.dissolved += dissolved
            stats.promoted += promoted
            stats.rejected += rejected
            stats.rejected_edges += rejected_edges
            record_llm_call(
                user_id=user_id,
                purpose="graph_cluster_partition",
                model=result.model,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cache_hit_tokens=result.usage.cache_hit_tokens,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                status="ok",
                source_id=None,
                metadata={
                    "cluster_id": pc.id,
                    "members": len(pc.members),
                    "groups": created + synced,
                },
            )
            stats.cost.calls += 1
            stats.cost.prompt_tokens += result.usage.prompt_tokens
            stats.cost.completion_tokens += result.usage.completion_tokens
            stats.cost.cost_usd += result.cost_usd
    finally:
        if owns_client:
            await aclose_llm(llm)

    with connection() as conn:
        materialize_cluster_edges(conn, user_id)

    _log.info(
        "relation.cluster.partition.end",
        user_id=user_id,
        blobs=stats.blobs,
        created=stats.created,
        synced=stats.synced,
        dissolved=stats.dissolved,
        rejected=stats.rejected,
        promoted=stats.promoted,
        rejected_edges=stats.rejected_edges,
        skipped=stats.skipped,
        errors=stats.errors,
        llm_calls=stats.cost.calls,
        llm_cost_usd=str(stats.cost.cost_usd),
    )
    return stats
