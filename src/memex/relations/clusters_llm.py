"""Validador LLM de CÚMULOS: por cada cúmulo candidato (o que requiere re-validación), una
llamada al LLM que revisa sus VÉRTICES y sus ARISTAS internas y devuelve veredicto + confianza +
nombre + descripción + poda. Es el decisor (Fase 4) a nivel cúmulo.

Molde: `modules/identidades/relations_llm.py` (loop best-effort por unidad, una llamada por cúmulo,
cliente inyectable, parseo ultra-defensivo, `LLMQuotaError` PROPAGA). Idempotente: solo toca cúmulos
`candidate`/`needs_revalidation`; un confirmado estable no se re-juzga (costo acotado). Al final
materializa las aristas `miembro_de` (idempotente + GC). Sin `attach_to_root` (un cúmulo NO es
per-mensaje: el costo se atribuye por `purpose`, patrón calendar/finance).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.config import settings
from memex.core.observability import CostAccum, record_llm_call
from memex.db import connection
from memex.llm import ChatMessage, DeepSeekClient, LLMClient, LLMConfig, LLMResult
from memex.llm.client import LLMQuotaError
from memex.logging import get_logger
from memex.relations.cluster_store import materialize_cluster_edges, reject_cluster
from memex.relations.edges import (
    RELTYPE_MIEMBRO_DE,
    STATUS_CONFIRMED,
    STATUS_REJECTED,
    Ref,
    RelationEdge,
    list_edges,
)
from memex.relations.prompt import GRAPH_CLUSTER_VALIDATION_SYSTEM_PROMPT
from memex.relations.vertices import Vertex, list_vertices

_log = get_logger("memex.relations.clusters_llm")

#: Cota de tokens de salida (un veredicto + nombre + descripción + lista de poda es chico).
_MAX_TOKENS = 2048
#: Tope de aristas internas serializadas (confirmed-primero) para acotar el prompt de cúmulos.
_INTERNAL_EDGE_CAP = 300


@dataclass
class ClusterValidationStats:
    """Resumen de una corrida del validador."""

    clusters: int = 0  # cúmulos con llamada LLM
    confirmed: int = 0
    rejected: int = 0
    pruned_members: int = 0
    skipped: int = 0  # saltados por tamaño (> cluster_max_members)
    errors: int = 0
    cost: CostAccum = field(default_factory=CostAccum)


@dataclass(frozen=True)
class ClusterVerdict:
    """Veredicto parseado del LLM. `verdict=None` = JSON inválido (no memoiza; se reintenta)."""

    verdict: str | None  # 'keep' | 'reject' | None
    confidence: float
    name: str
    description: str
    prune: list[int]  # ids LOCALES (1..n) válidos


@dataclass(frozen=True)
class _Pending:
    """Un cúmulo pendiente de validar + sus miembros vivos (id local = índice + 1)."""

    id: int
    signature: str
    members: list[Ref]


# --- carga (sin LLM) --------------------------------------------------------------- #


def _load_pending(conn: Connection, user_id: int, limit: int) -> list[_Pending]:
    """Cúmulos `candidate` o con `needs_revalidation`, los más grandes primero. Miembros VIVOS (no
    podados) ordenados → id local estable."""
    rows = (
        conn.execute(
            text(
                "SELECT id, signature FROM relation_clusters "
                "WHERE user_id = :u AND (status = 'candidate' OR needs_revalidation) "
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


def parse_verdict(content: str, n_members: int) -> ClusterVerdict:
    """Parsea `{verdict, confidence, name, description, prune}`. ULTRA-DEFENSIVO: basura → veredicto
    `None` (no memoiza); descarta `prune` fuera de `1..n`, bool-como-int y duplicados; clampa la
    confianza a 0..1."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return ClusterVerdict(None, 0.0, "", "", [])
    if not isinstance(data, dict):
        return ClusterVerdict(None, 0.0, "", "", [])
    verdict = data.get("verdict")
    if verdict not in ("keep", "reject"):
        return ClusterVerdict(None, 0.0, "", "", [])
    conf = data.get("confidence")
    confidence = (
        float(conf) if isinstance(conf, int | float) and not isinstance(conf, bool) else 0.0
    )
    confidence = max(0.0, min(1.0, confidence))
    name = data.get("name")
    description = data.get("description")
    raw = data.get("prune")
    prune: list[int] = []
    if isinstance(raw, list):
        seen: set[int] = set()
        for x in raw:
            if (
                isinstance(x, int)
                and not isinstance(x, bool)
                and 1 <= x <= n_members
                and x not in seen
            ):
                seen.add(x)
                prune.append(x)
    return ClusterVerdict(
        verdict,
        confidence,
        name.strip()[:200] if isinstance(name, str) else "",
        description.strip()[:1000] if isinstance(description, str) else "",
        prune,
    )


async def validate_cluster(
    llm: LLMClient, members: list[Ref], internal: list[RelationEdge], vmap: dict[Ref, Vertex]
) -> tuple[ClusterVerdict, LLMResult]:
    """Una llamada al LLM para validar UN cúmulo. Devuelve el veredicto parseado + el LLMResult."""
    result = await llm.complete(
        [
            ChatMessage("system", GRAPH_CLUSTER_VALIDATION_SYSTEM_PROMPT),
            ChatMessage("user", _serialize(members, internal, vmap)),
        ],
        response_format="json_object",
        temperature=0.0,
        max_tokens=_MAX_TOKENS,
    )
    return parse_verdict(result.content, len(members)), result


# --- aplicación del veredicto ------------------------------------------------------ #


def _apply_verdict(
    conn: Connection, user_id: int, pc: _Pending, verdict: ClusterVerdict
) -> tuple[str, int]:
    """Aplica el veredicto en su propia tx. Devuelve `(outcome, podados)` con outcome ∈
    {keep, reject, noop}. keep exige confianza ≥ umbral y ≥ 2 sobrevivientes; reject memoiza; JSON
    basura (`verdict=None`) es no-op (queda candidate, reintenta). UPDATE condicional para no pisar
    un cambio concurrente."""
    if verdict.verdict is None:
        return ("noop", 0)
    prune_refs = {pc.members[i - 1] for i in verdict.prune}
    survivors = len(pc.members) - len(prune_refs)
    keep = (
        verdict.verdict == "keep"
        and verdict.confidence >= settings.cluster_min_confidence
        and survivors >= 2
    )
    if not keep:
        reject_cluster(
            conn, user_id, pc.id, pc.signature, name=verdict.name, description=verdict.description
        )
        return ("reject", 0)
    rows = conn.execute(
        text(
            "UPDATE relation_clusters SET status = 'confirmed', name = :name, description = :desc, "
            "confidence = :conf, validated_signature = signature, validated_at = NOW(), "
            "needs_revalidation = FALSE, miss_count = 0, decided_at = NOW(), updated_at = NOW() "
            "WHERE id = :id AND (status = 'candidate' OR needs_revalidation)"
        ),
        {
            "name": verdict.name,
            "desc": verdict.description,
            "conf": round(verdict.confidence, 3),
            "id": pc.id,
        },
    ).rowcount
    if rows == 0:  # lo cambió otra corrida → no-op
        return ("noop", 0)
    for ref in prune_refs:
        conn.execute(
            text(
                "UPDATE relation_cluster_members SET pruned = TRUE "
                "WHERE cluster_id = :c AND member_slug = :s AND member_id = :i"
            ),
            {"c": pc.id, "s": ref.slug, "i": ref.id},
        )
    return ("keep", len(prune_refs))


# --- worker ------------------------------------------------------------------------ #


async def run_cluster_validation(
    user_id: int, *, limit: int | None = None, client: LLMClient | None = None
) -> ClusterValidationStats:
    """Valida con el LLM los cúmulos pendientes del user (1 llamada por cúmulo, best-effort). Emite
    confirmed/rejected + poda y, al final, materializa las aristas `miembro_de`. `client` inyectable
    (tests con fake). `LLMQuotaError` PROPAGA (el scheduler la captura). Idempotente."""
    limit = limit if limit is not None else settings.cluster_validate_limit
    stats = ClusterValidationStats()
    with connection() as conn:
        pending = _load_pending(conn, user_id, limit)
        vmap = {v.ref: v for v in list_vertices(conn, user_id)}
        all_edges = list_edges(conn, user_id)
    if not pending:
        _log.info("relation.cluster.validate.empty", user_id=user_id)
        return stats

    owns_client = client is None
    llm: LLMClient = client if client is not None else DeepSeekClient(LLMConfig.from_env())
    _log.info("relation.cluster.validate.start", user_id=user_id, pending=len(pending))
    try:
        for pc in pending:
            if len(pc.members) > settings.cluster_max_members:
                stats.skipped += 1
                _log.info(
                    "relation.cluster.validate.skip_too_big",
                    cluster_id=pc.id,
                    members=len(pc.members),
                )
                continue
            if len(pc.members) < 2:  # degenerado (quedó chico tras podas previas) → rechazar
                with connection() as conn:
                    reject_cluster(conn, user_id, pc.id, pc.signature)
                stats.rejected += 1
                continue
            internal = _internal_edges(all_edges, set(pc.members))
            try:
                verdict, result = await validate_cluster(llm, pc.members, internal, vmap)
            except LLMQuotaError:
                raise  # propaga: corta el resto del run
            except Exception as e:  # best-effort: un cúmulo fallido no frena los demás
                stats.errors += 1
                _log.error(
                    "relation.cluster.validate.failed",
                    cluster_id=pc.id,
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
                continue
            stats.clusters += 1
            with connection() as conn:
                outcome, pruned = _apply_verdict(conn, user_id, pc, verdict)
            if outcome == "keep":
                stats.confirmed += 1
                stats.pruned_members += pruned
            elif outcome == "reject":
                stats.rejected += 1
            record_llm_call(
                user_id=user_id,
                purpose="graph_cluster_validation",
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
                    "verdict": verdict.verdict,
                },
            )
            stats.cost.calls += 1
            stats.cost.prompt_tokens += result.usage.prompt_tokens
            stats.cost.completion_tokens += result.usage.completion_tokens
            stats.cost.cost_usd += result.cost_usd
    finally:
        if owns_client and isinstance(llm, DeepSeekClient):
            await llm.aclose()

    with connection() as conn:
        materialize_cluster_edges(conn, user_id)

    _log.info(
        "relation.cluster.validate.end",
        user_id=user_id,
        clusters=stats.clusters,
        confirmed=stats.confirmed,
        rejected=stats.rejected,
        pruned_members=stats.pruned_members,
        skipped=stats.skipped,
        errors=stats.errors,
        llm_calls=stats.cost.calls,
        llm_cost_usd=str(stats.cost.cost_usd),
    )
    return stats
