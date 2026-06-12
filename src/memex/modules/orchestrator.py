"""Orquestador de extracción (ADR-015 §2): Etapa A (ruteo) + Etapa B (extracción agrupada).

Parte de la etapa COMBINADA sobre los mensajes clasificados ORIGINALES (junto al summarizer,
no downstream). La unidad de trabajo es una ventana (lote batch / mensaje individual).

Etapa A — ruteo (SIEMPRE primero; dependency-aware): pre-filtro determinista por
`consumes_kinds`; con 0/1 candidato hace short-circuit (sin LLM); con ≥2 candidatos, 1 llamada
LLM barata elige los relevantes. Cierra con `depends_on` + topo-sort (`resolve_order`).

Etapa B — extracción `grouped` (default): los módulos elegidos se co-extraen en UNA sola llamada
LLM por ventana (FASE 1, sin efectos), partiendo en varias solo si superan `group_size` o ante
dependencias duras (`per_module` y `all` siguen disponibles como perilla). Luego se persiste
SECUENCIAL en orden topológico (FASE 2): cada item se valida contra el `extraction_schema` y se
descarta si su atribución cae fuera del lote (alucinación). Persistir filas + cursor
(`module_extractions`) es atómico por (módulo, ventana); el costo va a `llm_calls`. El benchmark
(experiments/batching_cache_bench) respalda agrupar: separar por módulo reenvía el correo N veces.

Best-effort: una ventana o módulo que falla se loguea + registra error y NO frena las demás
(la idempotencia del cursor evita re-trabajo). Cliente LLM inyectable (tests sin red).
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.deadletter import STAGE_EXTRACT, record_failures
from memex.core.observability import (
    NO_COST as _NO_COST,
)
from memex.core.observability import (
    CostBySource,
    record_llm_call,
)
from memex.core.observability import (
    cost_fields as _cost_fields,
)
from memex.core.trace import create_root, open_module_tracer
from memex.db import connection
from memex.llm import ChatMessage, DeepSeekClient, LLMClient, LLMConfig, LLMQuotaError, LLMResult
from memex.logging import bound_log_context, get_logger
from memex.modules import known_modules, resolve
from memex.modules.attribution import attributed_counts
from memex.modules.contract import (
    CAP_DEBUG_INBOX,
    CAP_PROVIDE_DOMAIN,
    DomainProvider,
    ExtractionItem,
    InboxDebugProvider,
    InterestModule,
    ModuleContext,
    parse_items,
    validate_item,
)
from memex.modules.grouping import (
    GROUPED_SYSTEM_PROMPT,
    build_grouped_user_content,
    parse_grouped_items,
    plan_groups,
)
from memex.modules.routing import (
    ROUTING_SYSTEM_PROMPT,
    build_routing_user_content,
    candidates_for_kind,
    parse_routing,
    resolve_order,
)
from memex.modules.workset import load_module_workset
from memex.processing.render import render_payload
from memex.processing.windows import (
    MAX_GAP_SECONDS,
    MAX_WINDOW_SIZE,
    Window,
    WorkRow,
    plan_windows,
)
from memex.sources import kind_for_type

_log = get_logger("memex.modules.orchestrator")

_DEFAULT_LIMIT = 200
_ROUTE_MAX_TOKENS = 256
_EXTRACT_MAX_TOKENS = 2048
#: 0 = sin split → una sola llamada de ruteo (comportamiento por defecto). Con >0, se rutea en
#: chunks de a lo sumo ese nº de módulos (perilla para muchos módulos; ADR-015 §2).
_ROUTE_CHUNK_DEFAULT = 0
#: Módulos por llamada de extracción con `batching_policy="grouped"` (ignorado en per_module/all).
#: Default 8 = headroom sobre los módulos actuales (4) → una sola llamada agrupada por ventana; el
#: split por conteo recién entra si los módulos elegidos superan este cap (perilla para "muchos
#: módulos"). El benchmark (experiments/batching_cache_bench) muestra que separar por módulo es lo
#: más caro: conviene mandar cada correo una sola vez.
_GROUP_SIZE_DEFAULT = 8
#: Tope de tokens de salida de una extracción agrupada (se escala por nº de módulos, con este cap).
_GROUPED_MAX_TOKENS_CAP = 8192
#: finish_reasons de respuesta COMPLETA. Otro valor (p. ej. "length" por max_tokens) = truncada →
#: el JSON queda inválido y `parse_items` da []; se trata como error reintentable (sin cursor), no
#: como "0 items extraídos". Espejo del summarizer (`summarizer/worker.py`).
_OK_FINISH = frozenset({"stop"})


@dataclass
class ExtractStats:
    """Resumen de una corrida de extracción."""

    windows: int = 0
    routed: int = 0  # llamadas LLM de ruteo (0 con ≤1 candidato; >1 por ventana si hay chunking)
    items: int = 0  # filas persistidas (gastos, etc.)
    discarded: int = 0  # items descartados (schema inválido / atribución alucinada)
    errors: int = 0
    by_module: dict[str, int] = field(default_factory=dict)
    #: Costo LLM acumulado de la corrida (total + por source); se emite en `extract.run.end`.
    cost: CostBySource = field(default_factory=CostBySource)


# --- helpers de DB ----------------------------------------------------------------- #


def _active_modules(conn: Connection, user_id: int) -> list[InterestModule]:
    slugs = (
        conn.execute(
            text("SELECT module_slug FROM module_settings WHERE user_id = :uid AND enabled"),
            {"uid": user_id},
        )
        .scalars()
        .all()
    )
    modules: list[InterestModule] = []
    for slug in slugs:
        try:
            modules.append(resolve(str(slug))())
        except KeyError:
            _log.warning("module.unknown_slug", slug=slug)
    return modules


def _done_by_id(conn: Connection, inbox_ids: list[int]) -> dict[int, set[str]]:
    if not inbox_ids:
        return {}
    rows = conn.execute(
        text("SELECT inbox_id, module_slug FROM module_extractions WHERE inbox_id = ANY(:ids)"),
        {"ids": inbox_ids},
    ).all()
    done: dict[int, set[str]] = defaultdict(set)
    for inbox_id, slug in rows:
        done[int(inbox_id)].add(str(slug))
    return done


def _insert_cursor(
    conn: Connection,
    user_id: int,
    module: InterestModule,
    inbox_ids: list[int],
) -> None:
    """Marca el cursor (module_slug, inbox_id). `item_count` = hechos públicos que el dominio
    ATRIBUYE a cada mensaje (`attributed_counts`, la MISMA función que `memex-quality
    backfill-counts`), sea cual sea el camino (persist / empty_input / ruteado-fuera) y aunque el
    dedup haya unido los hechos a filas pre-existentes — un reproceso o un ruteo distinto no
    degrada la señal de relevancia. ON CONFLICT DO NOTHING preserva el count previo — re-extraer
    sin `force` no lo pisa (con `force` se borró antes y se reescribe)."""
    if not inbox_ids:
        return
    by_id = attributed_counts(module, conn, user_id, inbox_ids)
    conn.execute(
        text(
            "INSERT INTO module_extractions (user_id, module_slug, inbox_id, item_count) "
            "VALUES (:uid, :slug, :iid, :cnt) ON CONFLICT (module_slug, inbox_id) DO NOTHING"
        ),
        [
            {"uid": user_id, "slug": module.slug, "iid": i, "cnt": by_id.get(i, 0)}
            for i in inbox_ids
        ],
    )


# --- render del lote --------------------------------------------------------------- #


def _build_messages(window: Window) -> tuple[str, dict[int, str]]:
    """JSON `[{id, ts, text}]` del lote (contenido ORIGINAL) + mapa id→texto para la evidencia."""
    rendered_by_id: dict[int, str] = {}
    items: list[dict[str, object]] = []
    for row in window.rows:
        rendered = render_payload(row.payload, row.ocr_text)
        rendered_by_id[row.inbox_id] = rendered
        items.append({"id": row.inbox_id, "ts": row.occurred_at.isoformat(), "text": rendered})
    return json.dumps(items, ensure_ascii=False), rendered_by_id


# --- Etapa A: ruteo ---------------------------------------------------------------- #


async def _route_chunk(
    user_id: int,
    llm: LLMClient,
    window: Window,
    messages_json: str,
    chunk: list[InterestModule],
    stats: ExtractStats,
    *,
    chunk_idx: int = 0,
    n_chunks: int = 1,
) -> list[str]:
    """Una llamada LLM de ruteo sobre un chunk de candidatos. Devuelve los slugs elegidos del
    chunk (∩ chunk); parse inválido → todos los del chunk (fallback conservador)."""
    chunk_slugs = {c.slug for c in chunk}
    catalog = [(c.slug, c.interest) for c in chunk]
    msgs = [
        ChatMessage("system", ROUTING_SYSTEM_PROMPT),
        ChatMessage("user", build_routing_user_content(catalog, messages_json)),
    ]
    result = await llm.complete(
        msgs, response_format="json_object", temperature=0.0, max_tokens=_ROUTE_MAX_TOKENS
    )
    stats.routed += 1
    parsed = parse_routing(result.content)
    record_llm_call(
        user_id=user_id,
        purpose="module_route",
        model=result.model,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cache_hit_tokens=result.usage.cache_hit_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        status="ok",
        source_id=window.source_id,  # atribución first-class por source
        metadata={
            "slugs_in": sorted(chunk_slugs),
            "chosen": parsed if parsed is not None else "parse_fallback",
            "n": len(window.rows),
            "chunk": chunk_idx,
            "chunks": n_chunks,
        },
        response_text=result.content,
    )
    stats.cost.record(
        window.source_id,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cost_usd=result.cost_usd,
    )
    if parsed is None:
        _log.warning("route.parse_fallback", source_id=window.source_id, chunk=chunk_idx)
        return sorted(chunk_slugs)
    return [s for s in parsed if s in chunk_slugs]


async def _route(
    user_id: int,
    llm: LLMClient,
    window: Window,
    candidates: list[InterestModule],
    active_by_slug: dict[str, InterestModule],
    stats: ExtractStats,
    *,
    route_chunk_size: int = _ROUTE_CHUNK_DEFAULT,
) -> list[str]:
    """Devuelve los slugs a extraer, en orden topológico. Short-circuit con ≤1 candidato (sin LLM).

    Con ≥2 candidatos rutea por LLM. Si `route_chunk_size>0` y hay más candidatos que ese tope, se
    rutea en SUB-PASADAS (un chunk por llamada) y se UNEN los elegidos. Después `resolve_order`
    corre UNA vez sobre el mapa completo, así que `depends_on` cross-chunk queda resuelto por el
    cierre transitivo (una dep en otro chunk se arrastra aunque su router no la haya elegido)."""
    if len(candidates) <= 1:
        chosen: list[str] = [c.slug for c in candidates]
    else:
        messages_json, _ = _build_messages(window)
        if route_chunk_size <= 0 or len(candidates) <= route_chunk_size:
            chosen = await _route_chunk(user_id, llm, window, messages_json, candidates, stats)
        else:
            ordered_c = sorted(candidates, key=lambda c: c.slug)
            chunks = [
                ordered_c[i : i + route_chunk_size]
                for i in range(0, len(ordered_c), route_chunk_size)
            ]
            union: set[str] = set()
            for idx, chunk in enumerate(chunks):
                union.update(
                    await _route_chunk(
                        user_id,
                        llm,
                        window,
                        messages_json,
                        chunk,
                        stats,
                        chunk_idx=idx,
                        n_chunks=len(chunks),
                    )
                )
            chosen = sorted(union)

    ordered = resolve_order(chosen, active_by_slug)
    if ordered.dropped:
        _log.warning("route.dropped", dropped=list(ordered.dropped), source_id=window.source_id)
    _log.info(
        "route.decision",
        source_id=window.source_id,
        chosen=list(ordered.order),
        n=len(window.rows),
        inbox_ids=[r.inbox_id for r in window.rows],
    )
    return list(ordered.order)


# --- Etapa B: extracción ----------------------------------------------------------- #


def _record_cost(
    user_id: int,
    slug: str,
    stats: ExtractStats,
    *,
    status: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: Decimal,
    latency_ms: int,
    n: int,
    source_id: int | None,
    cache_hit_tokens: int = 0,
    items: int = 0,
    discarded: int = 0,
    error_message: str | None = None,
    response_text: str | None = None,
) -> None:
    record_llm_call(
        user_id=user_id,
        purpose=f"extract_{slug}",
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        status=status,
        source_id=source_id,  # atribución first-class por source
        error_message=error_message,
        metadata={
            "slug": slug,
            "policy": "per_module",
            "n": n,
            "items": items,
            "discarded": discarded,
        },
        response_text=response_text,
    )
    stats.cost.record(
        source_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
    )


def _build_deps(
    module: InterestModule,
    conn: Connection,
    user_id: int,
    active_by_slug: dict[str, InterestModule],
) -> dict[str, object]:
    """`ctx.deps` del módulo: por cada slug en su `depends_on` (duro) u `optional_deps` (blando)
    cuyo proveedor activo declara `provide_domain`, inyecta su handle tipado ligado a `(conn,
    user_id)` — así el módulo usa el dominio de su dependencia sin SQL crudo. Vacío si no declara
    dependencias con dominio. Una `optional_deps` cuyo proveedor está apagado simplemente no aparece
    en `ctx.deps` (el módulo cae a su camino best-effort). El proveedor ya corrió antes en la
    ventana (topo-orden), así que el handle ve datos frescos."""
    deps: dict[str, object] = {}
    # dedup defensivo: un slug en ambas listas se inyecta una sola vez.
    for slug in dict.fromkeys((*module.depends_on, *module.optional_deps)):
        provider = active_by_slug.get(slug)
        if provider is None or CAP_PROVIDE_DOMAIN not in provider.capabilities:
            continue
        if isinstance(provider, DomainProvider):
            deps[slug] = provider.provide_domain(conn, user_id)
    return deps


@dataclass(frozen=True)
class _Unit:
    """Unidad de extracción de una ventana: un módulo SOLO o un GRUPO co-extraído en una llamada.

    La EXTRACCIÓN (FASE 1) corre concurrente entre unidades (no dependen entre sí); la PERSISTENCIA
    (FASE 2) corre en el ORDEN de las unidades — que respeta el topo-orden de `chosen` — así una
    dependencia (cuya unidad va antes) ya persistió cuando el dependiente resuelve `ctx.deps`."""

    modules: tuple[InterestModule, ...]
    rows: tuple[WorkRow, ...]
    grouped: bool  # True → una sola llamada LLM agrupada (≥2 módulos); False → módulo único
    group_size: int
    policy: str


@dataclass
class _UnitOutcome:
    """Resultado de extraer una unidad (FASE 1). SIN efectos: no toca DB, `stats` ni `llm_calls`."""

    status: str  # "ok" | "error" | "empty_input"
    valid_by_slug: dict[str, list[ExtractionItem]]
    discarded_by_slug: dict[str, int]
    result: LLMResult | None  # para registrar el costo en FASE 2; None si empty_input
    error_message: str | None = None


async def _extract_unit(user_id: int, llm: LLMClient, unit: _Unit) -> _UnitOutcome:
    """FASE 1 (extraer): UNA llamada LLM (por módulo o agrupada) + validación. NO persiste, NO toca
    `stats` ni el costo — solo devuelve los items validados o un marcador error/empty para que la
    FASE 2 (secuencial) los procese sin carreras. Es seguro correr varias en `asyncio.gather`."""
    window = Window(tier=unit.rows[0].tier, source_id=unit.rows[0].source_id, rows=unit.rows)
    messages_json, rendered_by_id = _build_messages(window)
    inbox_ids = [r.inbox_id for r in unit.rows]
    slugs = [m.slug for m in unit.modules]

    if not any(t.strip() for t in rendered_by_id.values()):
        return _UnitOutcome("empty_input", {}, {}, None)

    if unit.grouped:
        msgs = [
            ChatMessage("system", GROUPED_SYSTEM_PROMPT),
            ChatMessage("user", build_grouped_user_content(list(unit.modules), messages_json)),
        ]
        max_tokens = min(_EXTRACT_MAX_TOKENS * len(unit.modules), _GROUPED_MAX_TOKENS_CAP)
    else:
        msgs = [
            ChatMessage("system", unit.modules[0].extraction_prompt),
            ChatMessage("user", "Mensajes (JSON):\n" + messages_json),
        ]
        max_tokens = _EXTRACT_MAX_TOKENS

    result = await llm.complete(
        msgs, response_format="json_object", temperature=0.0, max_tokens=max_tokens
    )

    if not result.content.strip():
        return _UnitOutcome("error", {}, {}, result, "empty content")
    # Truncada (p. ej. "length"): el JSON queda cortado → parse daría [] y cursorearíamos "0 items"
    # (pérdida silenciosa). Tratar como error reintentable: sin cursor.
    if result.finish_reason is not None and result.finish_reason not in _OK_FINISH:
        return _UnitOutcome("error", {}, {}, result, f"truncated ({result.finish_reason})")

    lote = frozenset(inbox_ids)
    by_slug = (
        parse_grouped_items(result.content, slugs)
        if unit.grouped
        else {unit.modules[0].slug: parse_items(result.content)}
    )
    valid_by_slug: dict[str, list[ExtractionItem]] = {}
    discarded_by_slug: dict[str, int] = {}
    for module in unit.modules:
        raw_items = by_slug[module.slug]
        valid: list[ExtractionItem] = []
        for raw in raw_items:
            item = validate_item(
                module.extraction_schema, raw, lote=lote, rendered_by_id=rendered_by_id
            )
            if item is not None:
                valid.append(item)
        valid_by_slug[module.slug] = valid
        discarded_by_slug[module.slug] = len(raw_items) - len(valid)
    return _UnitOutcome("ok", valid_by_slug, discarded_by_slug, result)


def _record_grouped_cost(
    user_id: int,
    unit: _Unit,
    result: LLMResult,
    stats: ExtractStats,
    *,
    status: str,
    source_id: int | None,
    n: int,
    items_by_slug: dict[str, int] | None = None,
    discarded_by_slug: dict[str, int] | None = None,
    error_message: str | None = None,
) -> None:
    """UN solo `extract_grouped` `llm_call` por llamada agrupada (el costo per-módulo no es
    separable: prompt+completion compartido). Conserva la atribución items/discarded por slug."""
    metadata: dict[str, object] = {
        "policy": unit.policy,
        "group_size": unit.group_size,
        "slugs": [m.slug for m in unit.modules],
        "n": n,
    }
    if items_by_slug is not None:
        metadata["items_by_slug"] = items_by_slug
    if discarded_by_slug is not None:
        metadata["discarded_by_slug"] = discarded_by_slug
    record_llm_call(
        user_id=user_id,
        purpose="extract_grouped",
        model=result.model,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cache_hit_tokens=result.usage.cache_hit_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        status=status,
        source_id=source_id,  # atribución first-class por source
        error_message=error_message,
        metadata=metadata,
        response_text=result.content,
    )
    stats.cost.record(
        source_id,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cost_usd=result.cost_usd,
    )


async def _persist_unit(
    user_id: int,
    llm: LLMClient,
    unit: _Unit,
    outcome: _UnitOutcome,
    stats: ExtractStats,
    active_by_slug: dict[str, InterestModule],
    *,
    trace_root_id: int | None = None,
) -> None:
    """FASE 2 (persistir/dedup): SECUENCIAL y en topo-orden. Materializa los items de la unidad —
    cada módulo en su PROPIA tx con `ctx.deps` ya poblado (la dependencia persistió en una unidad
    ANTERIOR), escribe el cursor (atómico con el persist) y registra costo + `stats`."""
    inbox_ids = [r.inbox_id for r in unit.rows]
    source_id = unit.rows[0].source_id
    slugs = [m.slug for m in unit.modules]
    n = len(unit.rows)

    if outcome.status == "empty_input":
        # Lote sin texto útil → nada que extraer; se marca el cursor de cada módulo (no recargarlo).
        with connection() as conn:
            for module in unit.modules:
                _insert_cursor(conn, user_id, module, inbox_ids)
        _log.info("extract.unit.empty_input", slugs=slugs, n=n, inbox_ids=inbox_ids)
        return

    if outcome.status == "error":
        stats.errors += 1
        result = outcome.result
        if result is not None:  # en 'error' siempre lo es; el guard es para el type-checker
            if unit.grouped:
                _record_grouped_cost(
                    user_id,
                    unit,
                    result,
                    stats,
                    status="error",
                    source_id=source_id,
                    n=n,
                    error_message=outcome.error_message,
                )
            else:
                _record_cost(
                    user_id,
                    unit.modules[0].slug,
                    stats,
                    status="error",
                    model=result.model,
                    prompt_tokens=result.usage.prompt_tokens,
                    completion_tokens=result.usage.completion_tokens,
                    cache_hit_tokens=result.usage.cache_hit_tokens,
                    cost_usd=result.cost_usd,
                    latency_ms=result.latency_ms,
                    n=n,
                    source_id=source_id,
                    error_message=outcome.error_message,
                    response_text=result.content,
                )
        _log.warning(
            "extract.unit.error", slugs=slugs, n=n, inbox_ids=inbox_ids, error=outcome.error_message
        )
        return  # sin cursor → reintentable

    result = outcome.result
    if result is None:  # 'ok' siempre trae result; defensivo para el type-checker
        return
    items_by_slug: dict[str, int] = {}
    # Persistir cada módulo en su tx (atomicidad per-módulo). El orden de unidades (topo) garantiza
    # que la dependencia ya commiteó, así que `_build_deps` ve su dominio fresco.
    for i, module in enumerate(unit.modules):
        with connection() as conn:
            # Span del módulo bajo el root (atómico con persist + cursor en esta tx). NULL_TRACER
            # si la traza está apagada (trace_root_id None → batch / window multi-mensaje).
            tracer = open_module_tracer(
                conn,
                user_id=user_id,
                inbox_id=inbox_ids[0],
                root_id=trace_root_id,
                slug=module.slug,
                label=module.slug,
                seq=i,
            )
            ctx = ModuleContext(
                user_id=user_id,
                conn=conn,
                llm=llm,
                deps=_build_deps(module, conn, user_id, active_by_slug),
                summary_id=None,
                inbox_ids=tuple(inbox_ids),
                trace=tracer,
            )
            persisted = await module.persist(ctx, outcome.valid_by_slug[module.slug])
            # Cursor en la MISMA tx, tras persist → `attributed_counts` ve las filas frescas.
            # NO usar `persisted` como conteo: es el total de la VENTANA (sobre-atribuiría en
            # batch) y cuenta solo lo insertado (un hecho unido por dedup a una fila existente
            # dejaría el mensaje en 0 → relevancia degradada al reprocesar).
            _insert_cursor(conn, user_id, module, inbox_ids)
        items_by_slug[module.slug] = persisted
        stats.items += persisted
        stats.discarded += outcome.discarded_by_slug[module.slug]
        stats.by_module[module.slug] = stats.by_module.get(module.slug, 0) + persisted

    if unit.grouped:
        _record_grouped_cost(
            user_id,
            unit,
            result,
            stats,
            status="ok",
            source_id=source_id,
            n=n,
            items_by_slug=items_by_slug,
            discarded_by_slug=outcome.discarded_by_slug,
        )
    else:
        slug = unit.modules[0].slug
        _record_cost(
            user_id,
            slug,
            stats,
            status="ok",
            model=result.model,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            cache_hit_tokens=result.usage.cache_hit_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            n=n,
            source_id=source_id,
            items=items_by_slug[slug],
            discarded=outcome.discarded_by_slug[slug],
            response_text=result.content,
        )
    _log.info(
        "extract.unit.done",
        slugs=slugs,
        n=n,
        inbox_ids=inbox_ids,
        items=sum(items_by_slug.values()),
    )


async def _process_window(
    user_id: int,
    llm: LLMClient,
    window: Window,
    active: list[InterestModule],
    active_by_slug: dict[str, InterestModule],
    stats: ExtractStats,
    *,
    route_chunk_size: int = _ROUTE_CHUNK_DEFAULT,
    batching_policy: str = "grouped",
    group_size: int = _GROUP_SIZE_DEFAULT,
    trace_root_id: int | None = None,
) -> None:
    kind = kind_for_type(window.rows[0].source_type)
    candidates = candidates_for_kind(kind, active)
    if not candidates:
        return  # el workset no debería traer kinds no-consumidos; defensivo

    with connection() as conn:
        done = _done_by_id(conn, [r.inbox_id for r in window.rows])

    chosen = await _route(
        user_id, llm, window, candidates, active_by_slug, stats, route_chunk_size=route_chunk_size
    )
    chosen_set = set(chosen)

    # Etapa B — construir las UNIDADES de extracción (mismo cálculo de filas pending/co/leftover) en
    # el ORDEN de `chosen` (topo): la FASE 2 persiste en este orden, así una dependencia persiste
    # antes que su dependiente.
    units: list[_Unit] = []
    for group in plan_groups(chosen, active_by_slug, batching_policy, group_size):
        modules = [active_by_slug[s] for s in group]
        if len(modules) == 1:
            module = modules[0]
            pending = tuple(
                r for r in window.rows if module.slug not in done.get(r.inbox_id, set())
            )
            if pending:
                units.append(_Unit((module,), pending, False, group_size, batching_policy))
            continue
        # Grupo ≥2: co-extraer SOLO las filas que NINGÚN miembro procesó (intersección).
        co = tuple(
            r
            for r in window.rows
            if all(m.slug not in done.get(r.inbox_id, set()) for m in modules)
        )
        if co:
            units.append(_Unit(tuple(modules), co, True, group_size, batching_policy))
        # Fallback: filas con progreso parcial → cada módulo rezagado las hace per-módulo (así
        # ningún módulo recién habilitado queda sin procesar filas que otro del grupo ya hizo).
        co_ids = {r.inbox_id for r in co}
        for module in modules:
            leftover = tuple(
                r
                for r in window.rows
                if r.inbox_id not in co_ids and module.slug not in done.get(r.inbox_id, set())
            )
            if leftover:
                units.append(_Unit((module,), leftover, False, group_size, batching_policy))

    # FASE 1 (extraer): todas las unidades CONCURRENTEMENTE, sin efectos. `return_exceptions=True`
    # para no dejar tareas huérfanas si una revienta. Como nada se persistió aún, la ventana queda
    # 100% reintentable: una excepción dura se re-lanza (best-effort por ventana en run_extraction;
    # quota TIENE PRIORIDAD y aborta la corrida).
    async def _extract_unit_bound(u: _Unit) -> _UnitOutcome:
        # Correlación por unidad: cada task de gather COPIA el contexto al crearse, así el bind
        # vive solo en su copia (sin contaminación cruzada) y los logs internos (retries del LLM)
        # salen atribuidos. inbox_id solo si la unidad es de 1 mensaje; la lista va en los eventos.
        with bound_log_context(
            source_id=u.rows[0].source_id,
            inbox_id=u.rows[0].inbox_id if len(u.rows) == 1 else None,
        ):
            return await _extract_unit(user_id, llm, u)

    results = await asyncio.gather(*(_extract_unit_bound(u) for u in units), return_exceptions=True)
    for res in results:
        if isinstance(res, LLMQuotaError):
            raise res
    outcomes: list[_UnitOutcome] = []
    for res in results:
        if isinstance(res, BaseException):
            raise res
        outcomes.append(res)

    # FASE 2 (persistir/dedup): SECUENCIAL y en el orden de las unidades (topo) — el dependiente ve
    # el dominio fresco de su dependencia, aunque ambos se extrajeron a la vez en la FASE 1.
    # El bind por unidad cubre los logs de los MÓDULOS adentro (identidades.dedup.done, llm.call):
    # heredan source_id — e inbox_id cuando la unidad es de 1 mensaje — sin tocar su firma.
    for unit, outcome in zip(units, outcomes, strict=True):
        with bound_log_context(
            source_id=unit.rows[0].source_id,
            inbox_id=unit.rows[0].inbox_id if len(unit.rows) == 1 else None,
        ):
            await _persist_unit(
                user_id, llm, unit, outcome, stats, active_by_slug, trace_root_id=trace_root_id
            )

    # Candidatos ruteados-fuera: marcar "considerado" para no re-rutearlos eternamente.
    for module in candidates:
        if module.slug in chosen_set:
            continue
        pending_ids = [
            r.inbox_id for r in window.rows if module.slug not in done.get(r.inbox_id, set())
        ]
        if pending_ids:
            with connection() as conn:
                _insert_cursor(conn, user_id, module, pending_ids)
            _log.info(
                "route.skipped_module",
                slug=module.slug,
                source_id=window.source_id,
                n=len(pending_ids),
            )


# --- entry point ------------------------------------------------------------------- #


async def run_extraction(
    user_id: int,
    *,
    source_id: int | None = None,
    limit: int = _DEFAULT_LIMIT,
    max_window_size: int = MAX_WINDOW_SIZE,
    max_gap_seconds: int = MAX_GAP_SECONDS,
    route_chunk_size: int = _ROUTE_CHUNK_DEFAULT,
    batching_policy: str = "grouped",
    group_size: int = _GROUP_SIZE_DEFAULT,
    inbox_ids: list[int] | None = None,
    force: bool = False,
    client: LLMClient | None = None,
) -> ExtractStats:
    """Corre la extracción sobre el work-set clasificado no-extraído del user.

    Perillas (flags de CLI; ADR-015 §2): `max_window_size`/`max_gap_seconds` (ventaneo),
    `route_chunk_size` (sub-pasadas de ruteo con muchos módulos), `batching_policy`
    (`per_module`/`grouped`/`all`) + `group_size` (módulos por llamada de extracción).
    `inbox_ids` acota a un set explícito (reproceso por lote, vía `reprocess`): respeta los mismos
    tiers que el daemon (el work-set ya filtra blacklist). `force` re-extrae esos ids aunque ya
    tengan cursor (borra cursor + filas por módulo primero, espejo de `extract_inbox`).
    `client` inyectable (tests con fake). Best-effort por ventana.
    """
    stats = ExtractStats()

    with connection() as conn:
        active = _active_modules(conn, user_id)
    if not active:
        _log.info("extract.run.no_modules", user_id=user_id)
        return stats
    active_by_slug = {m.slug: m for m in active}

    if force and inbox_ids:
        with connection() as conn:
            conn.execute(
                text("DELETE FROM module_extractions WHERE inbox_id = ANY(:iids)"),
                {"iids": inbox_ids},
            )
            # De-hardcodeado: cada módulo borra sus filas por `forget_inbox` (igual que
            # `extract_inbox` en force). Se iteran TODOS los registrados, no solo los activos.
            for slug in known_modules():
                resolve(slug)().forget_inbox(conn, user_id, inbox_ids)

    # Un set explícito no debe cortarse por el LIMIT a nivel de mensaje.
    eff_limit = max(limit, len(inbox_ids)) if inbox_ids else limit
    with connection() as conn:
        workset = load_module_workset(
            conn, user_id, source_id=source_id, modules=active, limit=eff_limit, inbox_ids=inbox_ids
        )
    windows = plan_windows(
        workset, max_window_size=max_window_size, max_gap_seconds=max_gap_seconds
    )
    if not windows:
        _log.info("extract.run.empty", user_id=user_id, source_id=source_id)
        return stats

    owns_client = client is None
    llm: LLMClient = client if client is not None else DeepSeekClient(LLMConfig.from_env())
    _log.info(
        "extract.run.start",
        user_id=user_id,
        windows=len(windows),
        modules=[m.slug for m in active],
    )
    try:
        for window in windows:
            try:
                await _process_window(
                    user_id,
                    llm,
                    window,
                    active,
                    active_by_slug,
                    stats,
                    route_chunk_size=route_chunk_size,
                    batching_policy=batching_policy,
                    group_size=group_size,
                )
            except LLMQuotaError:
                # Saldo agotado: abortar la corrida (no es best-effort por ventana). El cliente se
                # cierra en el finally y el CLI sale con un mensaje accionable.
                _log.error("extract.run.aborted_no_quota", source_id=window.source_id)
                raise
            except Exception as e:  # best-effort: una ventana fallida no frena las demás
                stats.errors += 1
                _log.error(
                    "extract.window.failed",
                    source_id=window.source_id,
                    tier=window.tier,
                    n=len(window.rows),
                    inbox_ids=[r.inbox_id for r in window.rows],
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
                _record_cost(
                    user_id,
                    "unknown",
                    stats,
                    status="error",
                    model="unknown",
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_usd=Decimal("0"),
                    latency_ms=0,
                    n=len(window.rows),
                    source_id=window.source_id,
                    error_message=str(e)[:500],
                )
                # Dead-letter: suma fallo a cada mensaje; al 3er fallo → 'pendiente de revisión'.
                record_failures(user_id, STAGE_EXTRACT, [r.inbox_id for r in window.rows], str(e))
            stats.windows += 1
    finally:
        if owns_client and isinstance(llm, DeepSeekClient):
            await llm.aclose()

    _log.info(
        "extract.run.end",
        user_id=user_id,
        windows=stats.windows,
        routed=stats.routed,
        items=stats.items,
        discarded=stats.discarded,
        errors=stats.errors,
        **{f"module_{slug}": n for slug, n in stats.by_module.items()},
        **stats.cost.log_fields(),
    )
    return stats


# --- extracción por mensaje (dashboard: extraer UN inbox o su ventana) -------------- #

#: Tope alto para que la ventana de un mensaje reciente entre en el scan (occurred_at ASC).
_WINDOW_SCAN_LIMIT = 10_000


class InboxNotClassifiedError(Exception):
    """El mensaje existe pero no tiene clasificación (precondición de extraer)."""


def _load_one_workrow(user_id: int, inbox_id: int) -> WorkRow:
    """WorkRow de un inbox puntual con `source_type` (para kind/consumes_kinds) + tier + ocr."""
    with connection() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT i.source_id, i.occurred_at, i.payload, c.tier, s.type AS source_type,
                           COALESCE((
                               SELECT string_agg(ocr_text, E'\n' ORDER BY id)
                               FROM media_assets
                               WHERE inbox_id = i.id AND ocr_status = 'ok'
                                 AND ocr_text IS NOT NULL AND ocr_text <> ''
                           ), '') AS ocr_text
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
    if row is None:
        raise LookupError(f"inbox {inbox_id} not found")
    if row["tier"] is None:
        raise InboxNotClassifiedError(f"inbox {inbox_id} not classified")
    return WorkRow(
        inbox_id=inbox_id,
        source_id=int(row["source_id"]),
        occurred_at=row["occurred_at"],
        payload=_coerce_payload(row["payload"]),
        tier=str(row["tier"]),
        source_type=str(row["source_type"]),
        ocr_text=str(row["ocr_text"]),
    )


def read_extractions(user_id: int, inbox_id: int) -> dict[str, Any]:
    """Estado de extracción de un inbox: módulos ya corridos (cursor) + filas PÚBLICAS por módulo.

    `done`=True aunque NO haya filas: el cursor en `module_extractions` marca "ya procesado, 0 datos
    relevantes" — clave para que la UI distinga "sin extraer" de "extraído sin resultados".

    De-hardcodeado: itera el registry y le pide a cada módulo su `read_for_inbox` (su puerta
    pública), en vez de hardcodear un SELECT por tabla. Un módulo nuevo aparece acá solo. Se iteran
    TODOS los registrados (no solo los activos): uno deshabilitado puede tener filas históricas y no
    se debe soltar su clave. Las claves por slug preservan finance/calendar/hackathones (que lee el
    front) y suman las nuevas (identidades)."""
    with connection() as conn:
        modules = (
            conn.execute(
                text(
                    "SELECT DISTINCT module_slug FROM module_extractions "
                    "WHERE inbox_id = :id ORDER BY module_slug"
                ),
                {"id": inbox_id},
            )
            .scalars()
            .all()
        )
        result: dict[str, Any] = {
            "done": len(modules) > 0,
            "modules": [str(m) for m in modules],
        }
        for slug in known_modules():
            result[slug] = resolve(slug)().read_for_inbox(conn, user_id, [inbox_id])
    return result


def read_extractions_debug(user_id: int, inbox_id: int) -> dict[str, dict[str, Any]]:
    """Estado INTERNO por-módulo de un inbox para la vista de DEBUG (`/datos/:id`): de-hardcodeado,
    itera el registry y le pide su `debug_for_inbox` a cada módulo que declara `CAP_DEBUG_INBOX`.
    Cada valor es `{"rows": [...], "internal_calls": [...]}` (estado por-entidad + las llamadas LLM
    internas correlacionadas, con su costo). Solo módulos con la capacidad (finance/identidades);
    el resto se omite. Read-only."""
    out: dict[str, dict[str, Any]] = {}
    with connection() as conn:
        for slug in known_modules():
            module = resolve(slug)()
            if CAP_DEBUG_INBOX not in module.capabilities or not isinstance(
                module, InboxDebugProvider
            ):
                continue
            out[slug] = module.debug_for_inbox(conn, user_id, [inbox_id])
    return out


def _coerce_payload(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


async def extract_inbox(
    user_id: int,
    inbox_id: int,
    *,
    scope: str = "individual",
    force: bool = False,
    client: LLMClient | None = None,
) -> dict[str, Any]:
    """Extrae (módulos) sobre UN mensaje o su ventana. Reusa `_process_window` (que saltea lo ya
    hecho por módulo). `force` borra cursor + filas previas. Lanza Lookup/NotClassified.

    NO aplica el gate de relevancia: el click explícito por-mensaje es un bypass deliberado
    (paridad con `summarize_inbox`, que tampoco filtra en este camino).
    """
    with connection() as conn:
        active = _active_modules(conn, user_id)
    if not active:
        return {
            "status": "no_modules",
            "items": 0,
            "discarded": 0,
            "by_module": {},
            **_NO_COST,
            "done": False,
            "modules": [],
            # Una clave vacía por módulo registrado (sin hardcodear; incluye identidades).
            **{slug: [] for slug in known_modules()},
        }
    active_by_slug = {m.slug: m for m in active}

    row = _load_one_workrow(user_id, inbox_id)

    # Construir el cliente (valida DEEPSEEK_API_KEY) ANTES de borrar nada en `force`.
    owns_client = client is None
    llm: LLMClient = client if client is not None else DeepSeekClient(LLMConfig.from_env())
    stats = ExtractStats()
    try:
        if force:
            with connection() as conn:
                conn.execute(
                    text("DELETE FROM module_extractions WHERE inbox_id = :id"), {"id": inbox_id}
                )
                # Borrado de-hardcodeado: cada módulo borra sus filas por su puerta `forget_inbox`
                # (contraparte de `read_for_inbox`). Se iteran TODOS los registrados para que un
                # módulo nuevo se limpie solo, sin tocar este código.
                for slug in known_modules():
                    resolve(slug)().forget_inbox(conn, user_id, [inbox_id])

        window: Window | None = None
        if scope == "window" and row.tier in ("batch", "individual"):
            with connection() as conn:
                workset = load_module_workset(
                    conn, user_id, source_id=row.source_id, modules=active, limit=_WINDOW_SCAN_LIMIT
                )
            windows = plan_windows(workset)
            window = next((w for w in windows if any(r.inbox_id == inbox_id for r in w.rows)), None)
        if window is None:
            window = Window(
                row.tier if row.tier in ("batch", "individual") else "individual",
                row.source_id,
                (row,),
            )
        # Traza jerárquica: SOLO con un mensaje único (scope individual / window de 1) — un lote
        # multi-mensaje mis-atribuiría todo al root del target. `create_root` hace delete-then-write
        # (re-extraer reemplaza la traza). Apagada (None) → los módulos reciben NULL_TRACER.
        trace_root_id: int | None = None
        if len(window.rows) == 1:
            with connection() as conn:
                trace_root_id = create_root(
                    conn, user_id=user_id, inbox_id=inbox_id, label=f"mensaje #{inbox_id}"
                )
        await _process_window(
            user_id, llm, window, active, active_by_slug, stats, trace_root_id=trace_root_id
        )
    finally:
        if owns_client and isinstance(llm, DeepSeekClient):
            await llm.aclose()

    return {
        "status": "ok",
        "items": stats.items,
        "discarded": stats.discarded,
        "by_module": dict(stats.by_module),
        **_cost_fields(stats.cost.total),
        **read_extractions(user_id, inbox_id),
    }
