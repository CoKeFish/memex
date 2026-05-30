"""Orquestador de extracción (ADR-015 §2): Etapa A (ruteo) + Etapa B (extracción per_module).

Parte de la etapa COMBINADA sobre los mensajes clasificados ORIGINALES (junto al summarizer,
no downstream). La unidad de trabajo es una ventana (lote batch / mensaje individual).

Etapa A — ruteo (SIEMPRE primero; dependency-aware): pre-filtro determinista por
`consumes_kinds`; con 0/1 candidato hace short-circuit (sin LLM); con ≥2 candidatos, 1 llamada
LLM barata elige los relevantes. Cierra con `depends_on` + topo-sort (`resolve_order`).

Etapa B — extracción `per_module` (default): por cada módulo elegido (en orden topológico) se
extrae sobre los mensajes de la ventana que aún no procesó. Cada item se valida contra el
`extraction_schema` y se descarta si su atribución cae fuera del lote (alucinación). Persistir
filas + cursor (`module_extractions`) es atómico por (módulo, ventana); el costo va a `llm_calls`.

Best-effort: una ventana o módulo que falla se loguea + registra error y NO frena las demás
(la idempotencia del cursor evita re-trabajo). Cliente LLM inyectable (tests sin red).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.observability import record_llm_call
from memex.db import connection
from memex.llm import ChatMessage, DeepSeekClient, LLMClient, LLMConfig
from memex.logging import get_logger
from memex.modules import resolve
from memex.modules.contract import (
    ExtractionItem,
    InterestModule,
    ModuleContext,
    parse_items,
    validate_item,
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
from memex.processing.windows import Window, WorkRow, plan_windows
from memex.sources import kind_for_type

_log = get_logger("memex.modules.orchestrator")

_DEFAULT_LIMIT = 200
_ROUTE_MAX_TOKENS = 256
_EXTRACT_MAX_TOKENS = 2048


@dataclass
class ExtractStats:
    """Resumen de una corrida de extracción."""

    windows: int = 0
    routed: int = 0  # llamadas LLM de ruteo hechas (0 con un solo módulo: short-circuit)
    items: int = 0  # filas persistidas (gastos, etc.)
    discarded: int = 0  # items descartados (schema inválido / atribución alucinada)
    errors: int = 0
    by_module: dict[str, int] = field(default_factory=dict)


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


def _insert_cursor(conn: Connection, user_id: int, slug: str, inbox_ids: list[int]) -> None:
    if not inbox_ids:
        return
    conn.execute(
        text(
            "INSERT INTO module_extractions (user_id, module_slug, inbox_id) "
            "VALUES (:uid, :slug, :iid) ON CONFLICT (module_slug, inbox_id) DO NOTHING"
        ),
        [{"uid": user_id, "slug": slug, "iid": i} for i in inbox_ids],
    )


# --- render del lote --------------------------------------------------------------- #


def _build_messages(window: Window) -> tuple[str, dict[int, str]]:
    """JSON `[{id, ts, text}]` del lote (contenido ORIGINAL) + mapa id→texto para la evidencia."""
    rendered_by_id: dict[int, str] = {}
    items: list[dict[str, object]] = []
    for row in window.rows:
        rendered = render_payload(row.payload)
        rendered_by_id[row.inbox_id] = rendered
        items.append({"id": row.inbox_id, "ts": row.occurred_at.isoformat(), "text": rendered})
    return json.dumps(items, ensure_ascii=False), rendered_by_id


# --- Etapa A: ruteo ---------------------------------------------------------------- #


async def _route(
    user_id: int,
    llm: LLMClient,
    window: Window,
    candidates: list[InterestModule],
    active_by_slug: dict[str, InterestModule],
    stats: ExtractStats,
) -> list[str]:
    """Devuelve los slugs a extraer, en orden topológico. Short-circuit con ≤1 candidato."""
    cand_slugs = {c.slug for c in candidates}
    if len(candidates) <= 1:
        chosen = [c.slug for c in candidates]
    else:
        messages_json, _ = _build_messages(window)
        catalog = [(c.slug, c.interest) for c in candidates]
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
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            status="ok",
            metadata={
                "slugs_in": sorted(cand_slugs),
                "chosen": parsed if parsed is not None else "parse_fallback",
                "n": len(window.rows),
            },
        )
        if parsed is None:
            _log.warning("route.parse_fallback", source_id=window.source_id)
            chosen = sorted(cand_slugs)
        else:
            chosen = [s for s in parsed if s in cand_slugs]

    ordered = resolve_order(chosen, active_by_slug)
    if ordered.dropped:
        _log.warning("route.dropped", dropped=list(ordered.dropped), source_id=window.source_id)
    _log.info("route.decision", source_id=window.source_id, chosen=list(ordered.order))
    return list(ordered.order)


# --- Etapa B: extracción ----------------------------------------------------------- #


def _record_cost(
    user_id: int,
    slug: str,
    *,
    status: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: Decimal,
    latency_ms: int,
    n: int,
    items: int = 0,
    discarded: int = 0,
    error_message: str | None = None,
) -> None:
    record_llm_call(
        user_id=user_id,
        purpose=f"extract_{slug}",
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        status=status,
        error_message=error_message,
        metadata={
            "slug": slug,
            "policy": "per_module",
            "n": n,
            "items": items,
            "discarded": discarded,
        },
    )


async def _extract_module(
    user_id: int,
    llm: LLMClient,
    module: InterestModule,
    rows: tuple[WorkRow, ...],
    stats: ExtractStats,
) -> None:
    """Extrae con UN módulo sobre `rows` (su subconjunto pendiente de la ventana)."""
    window = Window(tier=rows[0].tier, source_id=rows[0].source_id, rows=rows)
    messages_json, rendered_by_id = _build_messages(window)
    inbox_ids = [r.inbox_id for r in rows]

    if not any(t.strip() for t in rendered_by_id.values()):
        # Lote sin texto útil → nada que extraer; se marca el cursor para no recargarlo.
        with connection() as conn:
            _insert_cursor(conn, user_id, module.slug, inbox_ids)
        _log.info("extract.module.empty_input", slug=module.slug, n=len(rows))
        return

    msgs = [
        ChatMessage("system", module.extraction_prompt),
        ChatMessage("user", "Mensajes (JSON):\n" + messages_json),
    ]
    result = await llm.complete(
        msgs, response_format="json_object", temperature=0.0, max_tokens=_EXTRACT_MAX_TOKENS
    )

    if not result.content.strip():
        stats.errors += 1
        _record_cost(
            user_id,
            module.slug,
            status="error",
            model=result.model,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            n=len(rows),
            error_message="empty content",
        )
        _log.warning("extract.module.empty_content", slug=module.slug, n=len(rows))
        return  # sin cursor → reintentable

    raw_items = parse_items(result.content)
    lote = frozenset(inbox_ids)
    valid: list[ExtractionItem] = []
    for raw in raw_items:
        item = validate_item(
            module.extraction_schema, raw, lote=lote, rendered_by_id=rendered_by_id
        )
        if item is not None:
            valid.append(item)
    discarded = len(raw_items) - len(valid)

    # Persistir filas + cursor en UNA tx (atomicidad por modulo/ventana). Persistir ANTES del costo.
    with connection() as conn:
        ctx = ModuleContext(
            user_id=user_id,
            conn=conn,
            llm=llm,
            deps={},
            summary_id=None,
            inbox_ids=tuple(inbox_ids),
        )
        persisted = await module.persist(ctx, valid)
        _insert_cursor(conn, user_id, module.slug, inbox_ids)

    _record_cost(
        user_id,
        module.slug,
        status="ok",
        model=result.model,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        n=len(rows),
        items=persisted,
        discarded=discarded,
    )
    stats.items += persisted
    stats.discarded += discarded
    stats.by_module[module.slug] = stats.by_module.get(module.slug, 0) + persisted
    _log.info(
        "extract.module.done",
        slug=module.slug,
        n=len(rows),
        items=persisted,
        discarded=discarded,
    )


async def _process_window(
    user_id: int,
    llm: LLMClient,
    window: Window,
    active: list[InterestModule],
    active_by_slug: dict[str, InterestModule],
    stats: ExtractStats,
) -> None:
    kind = kind_for_type(window.rows[0].source_type)
    candidates = candidates_for_kind(kind, active)
    if not candidates:
        return  # el workset no debería traer kinds no-consumidos; defensivo

    with connection() as conn:
        done = _done_by_id(conn, [r.inbox_id for r in window.rows])

    chosen = await _route(user_id, llm, window, candidates, active_by_slug, stats)
    chosen_set = set(chosen)

    for slug in chosen:
        module = active_by_slug[slug]
        pending = tuple(r for r in window.rows if slug not in done.get(r.inbox_id, set()))
        if pending:
            await _extract_module(user_id, llm, module, pending, stats)

    # Candidatos ruteados-fuera: marcar "considerado" para no re-rutearlos eternamente.
    for module in candidates:
        if module.slug in chosen_set:
            continue
        pending_ids = [
            r.inbox_id for r in window.rows if module.slug not in done.get(r.inbox_id, set())
        ]
        if pending_ids:
            with connection() as conn:
                _insert_cursor(conn, user_id, module.slug, pending_ids)
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
    client: LLMClient | None = None,
) -> ExtractStats:
    """Corre la extracción sobre el work-set clasificado no-extraído del user.

    `client` inyectable (tests con fake). Best-effort por ventana.
    """
    stats = ExtractStats()

    with connection() as conn:
        active = _active_modules(conn, user_id)
    if not active:
        _log.info("extract.run.no_modules", user_id=user_id)
        return stats
    active_by_slug = {m.slug: m for m in active}

    with connection() as conn:
        workset = load_module_workset(
            conn, user_id, source_id=source_id, modules=active, limit=limit
        )
    windows = plan_windows(workset)
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
                await _process_window(user_id, llm, window, active, active_by_slug, stats)
            except Exception as e:  # best-effort: una ventana fallida no frena las demás
                stats.errors += 1
                _log.error(
                    "extract.window.failed",
                    source_id=window.source_id,
                    tier=window.tier,
                    n=len(window.rows),
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
                _record_cost(
                    user_id,
                    "unknown",
                    status="error",
                    model="unknown",
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_usd=Decimal("0"),
                    latency_ms=0,
                    n=len(window.rows),
                    error_message=str(e)[:500],
                )
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
    )
    return stats
