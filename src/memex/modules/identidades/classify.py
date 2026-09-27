"""Clasificador LLM del TIPO de las identidades `desconocido` (pendientes de clasificación).

El camino determinista del remitente deja a todo individuo de correo en `desconocido` (no adivina
el tipo con listas). Esta fase le pregunta al LLM, una entidad a la vez, QUÉ ES —persona,
organización (incluida una sub-unidad institucional) o producto— con **sesgo a NO adivinar**: ante
la duda devuelve `desconocido` y queda en el backlog (un falso "pendiente" es recuperable;
una falsa promoción mis-tipa el vértice). Solo promueve con confianza >= `min_confidence`.

Promover = `UPDATE kind`. Como el slug del grafo se deriva del kind ACTUAL
(`vertices.IDENTITY_SLUG_BY_KIND`), al cambiarlo la arista vieja (`identidades:desconocido`) queda
huérfana: se re-teje la afiliación bajo el slug nuevo (`weave_afiliacion`) y, al final del lote, se
poda lo stale (`reconcile_graph`). La jerarquía/nombrado de sub-unidades los hace aparte el
organizador (`hierarchy.run_organize`).

Best-effort por ítem + idempotente (`UPDATE ... AND kind='desconocido'`: re-correr no re-promueve).
Cada llamada se registra en `llm_calls` (`purpose="identidades_classify"`). Cliente LLM inyectable
(tests con fake). `LLMQuotaError` se propaga (aborta; no se quema el lote en cuota muerta).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.observability import CostAccum, record_llm_call
from memex.core.trace import attach_to_entity
from memex.db import connection
from memex.llm import (
    ChatMessage,
    LLMClient,
    LLMQuotaError,
    LLMResult,
    aclose_llm,
    build_llm_client,
)
from memex.logging import get_logger
from memex.modules.identidades.prompt import IDENTIDADES_CLASSIFY_SYSTEM_PROMPT
from memex.relations.deterministic import weave_afiliacion
from memex.relations.maintenance import reconcile_graph

_log = get_logger("memex.modules.identidades.classify")

_DEFAULT_LIMIT = 200
_DEFAULT_MIN_CONFIDENCE = 0.7
_MAX_TOKENS = 256
#: Cuántos asuntos de correo de muestra mostrarle al LLM por entidad (señal de contexto, acotada).
_SAMPLE_SUBJECTS = 3
#: Kinds que el clasificador puede PROMOVER (fuera de acá, incluido 'desconocido', no promueve).
_PROMOTABLE = frozenset({"persona", "organizacion", "producto"})


@dataclass(frozen=True)
class ClassifyDecision:
    """Veredicto del LLM: el tipo de la entidad + confianza. `kind='desconocido'` = no decidió."""

    kind: str  # 'persona' | 'organizacion' | 'producto' | 'desconocido'
    confidence: float
    rationale: str


@dataclass(frozen=True)
class ClassifyView:
    """Vista mínima de una entidad `desconocido` para mostrarle al LLM (sin ids internos en prosa).
    El dominio del correo + la org afiliada + los asuntos son la señal de clasificación."""

    id: int
    display_name: str
    identifiers: Sequence[str]
    affiliated_org: str
    subjects: Sequence[str]


@dataclass
class ClassifyStats:
    """Resumen de una corrida del clasificador de tipo."""

    items: int = 0
    promoted: int = 0  # desconocido → un kind definido
    pending: int = 0  # quedó desconocido (baja confianza / el LLM no decidió)
    errors: int = 0
    cost: CostAccum = field(default_factory=CostAccum)


def _fmt_view(v: ClassifyView) -> str:
    idf = ", ".join(v.identifiers) if v.identifiers else "(sin identificadores)"
    lines = [f"nombre actual: {v.display_name!r}", f"identificadores: [{idf}]"]
    if v.affiliated_org:
        lines.append(f"afiliada al dominio de: {v.affiliated_org!r}")
    if v.subjects:
        subj = "\n".join(f"  - {s[:120]!r}" for s in v.subjects)
        lines.append(f"asuntos de correos donde fue remitente:\n{subj}")
    return "\n".join(lines)


def _parse_classification(content: str) -> ClassifyDecision:
    """Parsea la respuesta del LLM. Ambigüedad/falla/kind inválido → `desconocido` (sesgo a no
    adivinar; un bool/no-numérico en confidence → 0.0; se acota a [0,1])."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return ClassifyDecision("desconocido", 0.0, "parse_fallback")
    if not isinstance(data, dict):
        return ClassifyDecision("desconocido", 0.0, "parse_fallback")
    raw_kind = data.get("kind")
    kind = raw_kind if isinstance(raw_kind, str) and raw_kind in _PROMOTABLE else "desconocido"
    raw_conf = data.get("confidence")
    if isinstance(raw_conf, (int, float)) and not isinstance(raw_conf, bool):
        confidence = max(0.0, min(1.0, float(raw_conf)))
    else:
        confidence = 0.0
    rationale = str(data.get("rationale") or "")[:500]
    return ClassifyDecision(kind, confidence, rationale)


async def classify_identity(llm: LLMClient, v: ClassifyView) -> tuple[ClassifyDecision, LLMResult]:
    """Le pregunta al LLM qué tipo es la entidad `v`. Devuelve la decisión + el LLMResult (para el
    costo). El sesgo a `desconocido` ante la duda se aplica en el prompt y en el parseo."""
    user_content = "Clasificá esta entidad de tipo aún indefinido:\n\n" + _fmt_view(v)
    result = await llm.complete(
        [
            ChatMessage("system", IDENTIDADES_CLASSIFY_SYSTEM_PROMPT),
            ChatMessage("user", user_content),
        ],
        response_format="json_object",
        temperature=0.0,
        max_tokens=_MAX_TOKENS,
    )
    return _parse_classification(result.content), result


# --- worker ------------------------------------------------------------------------ #


def _load_unknowns(conn: Connection, user_id: int, limit: int) -> list[ClassifyView]:
    """Las identidades `desconocido` del user + su contexto: identificadores, la org del dominio
    afiliada, y hasta `_SAMPLE_SUBJECTS` asuntos de correos donde fueron remitente."""
    rows = (
        conn.execute(
            text(
                """
                SELECT i.id, i.display_name,
                  (SELECT array_agg(platform || ':' || kind || ':' || value_norm)
                     FROM mod_identidades_identifiers WHERE identity_id = i.id) AS identifiers,
                  (SELECT o.display_name FROM mod_identidades_person_orgs po
                     JOIN mod_identidades o ON o.id = po.org_id
                     WHERE po.person_id = i.id AND po.user_id = i.user_id
                     ORDER BY o.id LIMIT 1) AS affiliated_org,
                  (SELECT array(
                     SELECT DISTINCT inb.payload->>'subject'
                     FROM mod_identidades_mentions m
                     JOIN inbox inb ON inb.id = ANY(m.source_inbox_ids)
                     WHERE m.resolved_identity_id = i.id AND m.user_id = i.user_id
                       AND m.resolution_method = 'sender'
                       AND coalesce(inb.payload->>'subject', '') <> ''
                     LIMIT :nsub)) AS subjects
                FROM mod_identidades i
                WHERE i.user_id = :u AND i.kind = 'desconocido'
                ORDER BY i.id
                LIMIT :lim
                """
            ),
            {"u": user_id, "lim": limit, "nsub": _SAMPLE_SUBJECTS},
        )
        .mappings()
        .all()
    )
    return [
        ClassifyView(
            id=int(r["id"]),
            display_name=str(r["display_name"]),
            identifiers=tuple(r["identifiers"] or ()),
            affiliated_org=str(r["affiliated_org"] or ""),
            subjects=tuple(str(s) for s in (r["subjects"] or ()) if s),
        )
        for r in rows
    ]


def _promote(conn: Connection, user_id: int, identity_id: int, kind: str) -> bool:
    """Promueve el kind de un `desconocido` y re-teje su afiliación bajo el slug nuevo. Idempotente:
    el `AND kind='desconocido'` hace que re-correr no re-promueva (devuelve False si ya cambió)."""
    changed = conn.execute(
        text(
            "UPDATE mod_identidades SET kind = :k, updated_at = NOW() "
            "WHERE id = :id AND user_id = :u AND kind = 'desconocido' RETURNING id"
        ),
        {"k": kind, "id": identity_id, "u": user_id},
    ).first()
    if changed is None:
        return False
    # El slug del vértice se deriva del kind ACTUAL: re-proyecta la arista `afiliado` bajo el slug
    # nuevo (la vieja, de slug `identidades:desconocido`, la poda `reconcile_graph` al final).
    weave_afiliacion(conn, user_id, identity_id)
    return True


def _attach_classification(
    conn: Connection, user_id: int, identity_id: int, call_id: int, decision: ClassifyDecision
) -> None:
    """Cuelga la clasificación LLM a la entidad como hoja de traza (best-effort; no-op si no tiene
    nodo, p. ej. un remitente que nunca pasó por extracción por-mensaje)."""
    node = attach_to_entity(conn, user_id=user_id, table="mod_identidades", ref_id=identity_id)
    if node is not None:
        node.llm(
            call_id,
            label=f"clasificación LLM → {decision.kind}",
            status="ok",
            detail={"kind": decision.kind, "confidence": round(decision.confidence, 2)},
        )


async def run_classify(
    user_id: int,
    *,
    limit: int = _DEFAULT_LIMIT,
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
    client: LLMClient | None = None,
) -> ClassifyStats:
    """Clasifica el backlog de `desconocido` del user con el LLM. Promueve solo con confianza >=
    `min_confidence`; lo dudoso queda `desconocido`. Best-effort por ítem; `LLMQuotaError` aborta.
    Idempotente. `client` inyectable (tests con fake)."""
    stats = ClassifyStats()
    with connection() as conn:
        items = _load_unknowns(conn, user_id, limit)
    if not items:
        _log.info("identidades.classify.empty", user_id=user_id)
        return stats

    owns_client = client is None
    llm: LLMClient = client or build_llm_client("identidades_classify", user_id=user_id)
    _log.info("identidades.classify.start", user_id=user_id, items=len(items))
    try:
        for v in items:
            stats.items += 1
            try:
                decision, result = await classify_identity(llm, v)
            except LLMQuotaError:
                raise  # saldo agotado: abortar (no quemar el resto del lote en cuota muerta)
            except Exception as e:  # best-effort: un ítem fallido no frena los demás
                stats.errors += 1
                _log.error(
                    "identidades.classify.item_failed",
                    id=v.id,
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
                continue
            promote = decision.kind in _PROMOTABLE and decision.confidence >= min_confidence
            with connection() as conn:
                if promote and _promote(conn, user_id, v.id, decision.kind):
                    stats.promoted += 1
                else:
                    stats.pending += 1  # baja confianza / el LLM no decidió / ya no era desconocido
            call_id = record_llm_call(
                user_id=user_id,
                purpose="identidades_classify",
                model=result.model,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cache_hit_tokens=result.usage.cache_hit_tokens,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                status="ok",
                source_id=None,
                metadata={"id": v.id, "kind": decision.kind, "confidence": decision.confidence},
            )
            with connection() as conn:
                _attach_classification(conn, user_id, v.id, call_id, decision)
            stats.cost.calls += 1
            stats.cost.prompt_tokens += result.usage.prompt_tokens
            stats.cost.completion_tokens += result.usage.completion_tokens
            stats.cost.cost_usd += result.cost_usd
        # Poda las aristas del slug viejo (`identidades:desconocido`) que quedaron huérfanas al
        # promover. Una sola pasada al final (idempotente; sin churn — ver test_merge).
        if stats.promoted:
            with connection() as conn:
                reconcile_graph(conn, user_id)
    finally:
        if owns_client:
            await aclose_llm(llm)

    _log.info(
        "identidades.classify.end",
        user_id=user_id,
        items=stats.items,
        promoted=stats.promoted,
        pending=stats.pending,
        errors=stats.errors,
        llm_calls=stats.cost.calls,
        llm_cost_usd=str(stats.cost.cost_usd),
    )
    return stats
