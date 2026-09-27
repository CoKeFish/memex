"""Orquestador de extracción (ADR-015 §2): Etapa A (ruteo) + Etapa B (extracción agrupada).

Parte de la etapa de extracción sobre los mensajes clasificados ORIGINALES (el resumen se
produce aparte, en la fase de co-ocurrencia: relations/summary.py; no downstream). La unidad de
trabajo es una ventana (lote batch / mensaje individual).

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
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.deadletter import STAGE_EXTRACT, clear_failures, record_failures
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
from memex.core.source import SourceKind
from memex.core.trace import NULL_TRACER, ModuleTracer, Tracer, attach_to_root, create_root
from memex.db import connection
from memex.llm import (
    ChatMessage,
    LLMClient,
    LLMQuotaError,
    LLMResult,
    LLMUsage,
    aclose_llm,
    build_llm_client,
)
from memex.logging import bound_log_context, get_logger
from memex.modules import known_modules, resolve
from memex.modules.attribution import attributed_counts
from memex.modules.contract import (
    CAP_PROVIDE_DOMAIN,
    CAP_VERIFY,
    DomainProvider,
    ExtractionItem,
    InterestModule,
    ModuleContext,
    VerifyCall,
    VerifyContext,
    VerifyOutcome,
    parse_items,
    validate_item,
)
from memex.modules.extraction_settings import get_extraction_settings
from memex.modules.grouping import (
    GROUPED_SYSTEM_PROMPT,
    build_grouped_user_content,
    own_consumer,
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
    EXTRACT_WINDOW_SIZE,
    Window,
    WorkRow,
    plan_windows,
)
from memex.sources import kind_for_type
from memex.version import code_version

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
#: Caracteres de texto por TRAMO al extraer un documento largo. Un extracto bancario es UNA unidad
#: de trabajo con decenas de movimientos: la respuesta no cabe en `max_tokens` y se corta, y como
#: una respuesta truncada es error reintentable, el documento no daba NINGÚN dato (medido: 33
#: movimientos ya truncan). Un movimiento ocupa ~75 caracteres de texto y ~70 tokens de JSON, así
#: que un tramo de este tamaño entra holgado en `_GROUPED_MAX_TOKENS_CAP`.
_DOC_CHUNK_CHARS = 3500
#: Techo de tramos por documento: un PDF enorme no puede disparar una corrida infinita de llamadas.
#: Pasarse NO recorta el texto (eso perdería movimientos en silencio): la unidad queda en error con
#: el motivo, que es reintentable y visible.
_DOC_MAX_CHUNKS = 40
#: finish_reasons de respuesta COMPLETA. Otro valor (p. ej. "length" por max_tokens) = truncada →
#: el JSON queda inválido y `parse_items` da []; se trata como error reintentable (sin cursor), no
#: como "0 items extraídos". Espejo de `relations/summary.py`.
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
    ATRIBUYE a cada mensaje (`attributed_counts`), sea cual sea el camino (persist / empty_input /
    ruteado-fuera) y aunque el
    dedup haya unido los hechos a filas pre-existentes — un reproceso o un ruteo distinto no
    degrada la señal de relevancia. ON CONFLICT DO NOTHING preserva el count previo — re-extraer
    sin `force` no lo pisa (con `force` se borró antes y se reescribe)."""
    if not inbox_ids:
        return
    by_id = attributed_counts(module, conn, user_id, inbox_ids)
    ver = code_version()  # con qué prompts/contratos se extrajo (sello, ver memex.version)
    conn.execute(
        text(
            "INSERT INTO module_extractions "
            "(user_id, module_slug, inbox_id, item_count, code_version) "
            "VALUES (:uid, :slug, :iid, :cnt, :ver) "
            "ON CONFLICT (module_slug, inbox_id) DO NOTHING"
        ),
        [
            {"uid": user_id, "slug": module.slug, "iid": i, "cnt": by_id.get(i, 0), "ver": ver}
            for i in inbox_ids
        ],
    )


# --- render del lote --------------------------------------------------------------- #


#: Dos mensajes seguidos del mismo remitente, en el mismo chat y tema, a menos de esto son UNA
#: ráfaga: quien anuncia una dinámica la escribe en cuatro mensajes en dos minutos («Por 70
#: puntos:», el acertijo, las reglas, «solo el primero»). Separados, el extractor veía fragmentos.
_BURST_MAX_GAP_S = 120
_BURST_MAX_MSGS = 8
_REPLY_QUOTE_CHARS = 160


def _chat_sender_key(payload: dict[str, Any]) -> str:
    snd = payload.get("sender")
    if isinstance(snd, dict):
        return str(snd.get("user_id") or snd.get("phone") or snd.get("display_name") or "")
    return ""


def _chat_sender_name(payload: dict[str, Any]) -> str:
    snd = payload.get("sender")
    if isinstance(snd, dict):
        if snd.get("is_self"):
            return "yo (la propia persona)"
        return str(snd.get("display_name") or snd.get("username") or "")
    return ""


def _same_burst(prev: WorkRow, row: WorkRow) -> bool:
    p, q = prev.payload, row.payload
    if "chat_id" not in p or "chat_id" not in q:
        return False
    if q.get("reply_to_message_id"):
        return False
    if (
        str(p.get("chat_id")) != str(q.get("chat_id"))
        or p.get("topic_id") != q.get("topic_id")
        or _chat_sender_key(p) != _chat_sender_key(q)
        or not _chat_sender_key(q)
    ):
        return False
    gap = (row.occurred_at - prev.occurred_at).total_seconds()
    return 0 <= gap <= _BURST_MAX_GAP_S


def _reply_context(row: WorkRow, by_message: dict[tuple[str, str], WorkRow]) -> str:
    """«↩ en respuesta a X: …» cuando el mensaje responde a otro del MISMO lote. Sin esto el
    extractor no sabía a quién le hablaban («confírmame nombre y país» era para otra persona)."""
    p = row.payload
    target = p.get("reply_to_message_id")
    if not target or "chat_id" not in p:
        return ""
    ref = by_message.get((str(p.get("chat_id")), str(target)))
    if ref is None:
        return ""
    quoted = " ".join(
        str(
            ref.payload.get("text")
            or ref.payload.get("media_caption")
            or ref.payload.get("body_text")
            or ""
        ).split()
    )
    who = _chat_sender_name(ref.payload) or "alguien"
    return f"↩ en respuesta a {who}: «{quoted[:_REPLY_QUOTE_CHARS]}»"


def _fetch_reply_originals(
    rows: Sequence[WorkRow], by_message: dict[tuple[str, str], WorkRow]
) -> None:
    """Completa `by_message` con los originales que NO están en el lote: un mensaje que responde
    a algo de otra ventana (o de un lote armado a mano, p. ej. un reproceso por ids) igual lleva
    su «↩ en respuesta a X: …». Sin esto el extractor no sabía a quién le hablaban («confírmame
    nombre, país y estrategia» era para otra persona) y lo anotaba como tarea propia (2026-09-26).
    Una sola consulta por lote; best-effort (sin tabla o sin fila, no hay cita, no se rompe)."""
    missing: dict[tuple[int, str, str], None] = {}
    for row in rows:
        p = row.payload
        target = p.get("reply_to_message_id")
        if not target or "chat_id" not in p:
            continue
        key = (str(p.get("chat_id")), str(target))
        if key not in by_message:
            missing[(row.source_id, key[0], key[1])] = None
    if not missing:
        return
    try:
        with connection() as conn:
            for source_id, chat_id, message_id in missing:
                found = conn.execute(
                    text(
                        "SELECT id, source_id, occurred_at, payload FROM inbox "
                        "WHERE source_id = :s AND payload->>'chat_id' = :c "
                        "AND payload->>'message_id' = :m ORDER BY id LIMIT 1"
                    ),
                    {"s": source_id, "c": chat_id, "m": message_id},
                ).first()
                if found is None:
                    continue
                by_message[(chat_id, message_id)] = WorkRow(
                    inbox_id=int(found[0]),
                    source_id=int(found[1]),
                    occurred_at=found[2],
                    payload=dict(found[3] or {}),
                    tier="",
                )
    except Exception as exc:  # pragma: no cover - defensivo: la cita es un extra
        _log.warning("orchestrator.reply_originals_failed", error=str(exc)[:200])


def _build_messages(window: Window) -> tuple[str, dict[int, str]]:
    """JSON `[{id, ts, text}]` del lote (contenido ORIGINAL) + mapa id→texto para la evidencia.

    En chats: una RÁFAGA (mensajes seguidos del mismo remitente, mismo tema, < 2 min) va como UN
    item con `ids` = todos sus mensajes (`id` = el primero), y un mensaje que responde a otro del
    lote lleva «↩ en respuesta a X: …» al frente. El mapa id→texto sigue siendo por mensaje."""
    rendered_by_id: dict[int, str] = {}
    items: list[dict[str, object]] = []
    by_message: dict[tuple[str, str], WorkRow] = {}
    for row in window.rows:
        p = row.payload
        if "chat_id" in p and p.get("message_id") is not None:
            by_message[(str(p.get("chat_id")), str(p.get("message_id")))] = row
    _fetch_reply_originals(window.rows, by_message)
    prev: WorkRow | None = None
    burst_len = 0
    for row in window.rows:
        rendered = render_payload(row.payload, row.ocr_text)
        rendered_by_id[row.inbox_id] = rendered
        if prev is not None and items and burst_len < _BURST_MAX_MSGS and _same_burst(prev, row):
            last = items[-1]
            ids = last.setdefault("ids", [last["id"]])
            assert isinstance(ids, list)
            ids.append(row.inbox_id)
            last["text"] = f"{last['text']}\n{rendered}"
            burst_len += 1
            prev = row
            continue
        text = rendered
        ctx = _reply_context(row, by_message)
        if ctx:
            text = f"{ctx}\n{rendered}"
        items.append({"id": row.inbox_id, "ts": row.occurred_at.isoformat(), "text": text})
        prev = row
        burst_len = 1
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
) -> tuple[list[str], int]:
    """Una llamada LLM de ruteo sobre un chunk de candidatos. Devuelve (slugs elegidos del chunk
    ∩ chunk; parse inválido → todos, fallback conservador) y el id de la `llm_call` de ruteo."""
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
    call_id = record_llm_call(
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
        return sorted(chunk_slugs), call_id
    return [s for s in parsed if s in chunk_slugs], call_id


async def _route(
    user_id: int,
    llm: LLMClient,
    window: Window,
    candidates: list[InterestModule],
    active_by_slug: dict[str, InterestModule],
    stats: ExtractStats,
    *,
    route_chunk_size: int = _ROUTE_CHUNK_DEFAULT,
    routing_enabled: bool = True,
) -> tuple[list[str], list[int]]:
    """Devuelve (slugs a extraer en orden topológico, ids de las calls de ruteo). Short-circuit con
    ≤1 candidato o `routing_enabled=False` (sin LLM → sin ids; en OFF se extraen TODOS los
    candidatos).

    Con ≥2 candidatos y la perilla ON rutea por LLM. Si `route_chunk_size>0` y hay más candidatos
    que ese tope, se rutea en SUB-PASADAS (un chunk por llamada) y se UNEN los elegidos. Después
    `resolve_order` corre UNA vez sobre el mapa completo, así que `depends_on` cross-chunk queda
    resuelto por el cierre transitivo (una dep en otro chunk se arrastra aunque su router no la
    haya elegido)."""
    route_call_ids: list[int] = []
    if not routing_enabled or len(candidates) <= 1:
        chosen: list[str] = [c.slug for c in candidates]  # sin LLM (perilla OFF o 1 candidato)
    else:
        messages_json, _ = _build_messages(window)
        if route_chunk_size <= 0 or len(candidates) <= route_chunk_size:
            chosen, cid = await _route_chunk(user_id, llm, window, messages_json, candidates, stats)
            route_call_ids.append(cid)
        else:
            ordered_c = sorted(candidates, key=lambda c: c.slug)
            chunks = [
                ordered_c[i : i + route_chunk_size]
                for i in range(0, len(ordered_c), route_chunk_size)
            ]
            union: set[str] = set()
            for idx, chunk in enumerate(chunks):
                slugs, cid = await _route_chunk(
                    user_id,
                    llm,
                    window,
                    messages_json,
                    chunk,
                    stats,
                    chunk_idx=idx,
                    n_chunks=len(chunks),
                )
                union.update(slugs)
                route_call_ids.append(cid)
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
    return list(ordered.order), route_call_ids


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
) -> int:
    call_id = record_llm_call(
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
    return call_id


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
    #: Llamadas LLM de la SEGUNDA VERIFICACIÓN (`CAP_VERIFY`) de cada módulo, hechas en FASE 1
    #: (sin efectos); la FASE 2 las registra en `llm_calls` y las cuelga de la traza.
    verify_calls: list[VerifyCall] = field(default_factory=list)


class _ConsumerClients:
    """Clientes LLM por CONSUMIDOR de una corrida: los de la segunda verificación (`CAP_VERIFY`:
    `calendar_date_verify` → codex) y los de módulos con `llm_consumer` propio (`extract_tareas`).
    Uno por consumer (perezoso, vía el registro), cerrados al final. Con un cliente INYECTADO
    (tests, `client=`) devuelve ese para todos."""

    def __init__(self, user_id: int, injected: LLMClient | None) -> None:
        self._user_id = user_id
        self._injected = injected
        self._built: dict[str, LLMClient] = {}

    def get(self, consumer: str) -> LLMClient:
        if self._injected is not None:
            return self._injected
        client = self._built.get(consumer)
        if client is None:
            client = build_llm_client(consumer, user_id=self._user_id)
            self._built[consumer] = client
        return client

    async def aclose(self) -> None:
        for client in self._built.values():
            await aclose_llm(client)
        self._built.clear()


#: Una línea que EMPIEZA con una fecha (31/07/2025, 2025-07-31, 1.7.25) abre un registro: en
#: extractos y libros cada movimiento suele ocupar varias líneas (fecha / descripción / valor /
#: saldo) y la fecha va primera.
_RECORD_START = re.compile(r"^\s*\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}\b")


def _record_boundary(lines: list[str]) -> int:
    """Dónde cortar un tramo lleno: antes del ÚLTIMO inicio de registro (línea con fecha al
    frente), para que un movimiento multilínea no quede con la fecha en un tramo y la descripción
    en el siguiente. Medido 2026-09-12: dos filas de extracto perdieron su fecha justo en el corte y
    quedaron fechadas el día de la carga. Sin inicio de registro (o solo el primero) corta al
    final, como antes."""
    for i in range(len(lines) - 1, 0, -1):
        if _RECORD_START.match(lines[i]):
            return i
    return len(lines)


def split_text_chunks(text: str, *, max_chars: int = _DOC_CHUNK_CHARS) -> list[str]:
    """Parte un texto largo en tramos de a lo sumo `max_chars`, SIEMPRE por fin de línea y, si el
    texto tiene registros que empiezan con fecha, en un INICIO de registro (`_record_boundary`).

    Cortar a la mitad de una línea —o entre la fecha y la descripción de un movimiento— partiría el
    dato y el modelo inventaría o perdería. Una línea (o un registro) más largo que el tope queda
    solo en su tramo (no se lo parte)."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    actual: list[str] = []
    largo = 0
    for linea in text.splitlines(keepends=True):
        if actual and largo + len(linea) > max_chars:
            corte = _record_boundary(actual)
            chunks.append("".join(actual[:corte]))
            actual = actual[corte:]  # el registro a medias arranca el tramo siguiente
            largo = sum(len(x) for x in actual)
        actual.append(linea)
        largo += len(linea)
    if actual:
        chunks.append("".join(actual))
    return chunks


def _merge_results(results: list[LLMResult]) -> LLMResult:
    """Funde varias llamadas en un resultado con el costo SUMADO: lo que se registra debe ser lo
    que de verdad se gastó, no el de la última llamada."""
    usage = LLMUsage(
        prompt_tokens=sum(r.usage.prompt_tokens for r in results),
        completion_tokens=sum(r.usage.completion_tokens for r in results),
        total_tokens=sum(r.usage.total_tokens for r in results),
        cache_hit_tokens=sum(r.usage.cache_hit_tokens for r in results),
    )
    return LLMResult(
        content="\n".join(r.content for r in results),
        model=results[0].model,
        usage=usage,
        cost_usd=sum((r.cost_usd for r in results), Decimal(0)),
        latency_ms=sum(r.latency_ms for r in results),
        finish_reason=results[-1].finish_reason,
    )


def _unit_client(default: LLMClient, unit: _Unit, clients: _ConsumerClients) -> LLMClient:
    """El cliente de la unidad: el del `llm_consumer` propio del módulo (unidad de un solo módulo,
    `plan_groups` lo deja solo) o el compartido (`orchestrator`)."""
    if len(unit.modules) == 1:
        consumer = own_consumer(unit.modules[0])
        if consumer:
            return clients.get(consumer)
    return default


async def _extract_unit(
    user_id: int, llm: LLMClient, unit: _Unit, verify_clients: _ConsumerClients
) -> _UnitOutcome:
    """FASE 1 (extraer): UNA llamada LLM (por módulo o agrupada) + validación. NO persiste, NO toca
    `stats` ni el costo — solo devuelve los items validados o un marcador error/empty para que la
    FASE 2 (secuencial) los procese sin carreras. Es seguro correr varias en `asyncio.gather`."""
    window = Window(tier=unit.rows[0].tier, source_id=unit.rows[0].source_id, rows=unit.rows)
    messages_json, rendered_by_id = _build_messages(window)
    inbox_ids = [r.inbox_id for r in unit.rows]
    slugs = [m.slug for m in unit.modules]
    llm = _unit_client(llm, unit, verify_clients)

    if not any(t.strip() for t in rendered_by_id.values()):
        return _UnitOutcome("empty_input", {}, {}, None)

    # Documento largo (una sola unidad con mucho texto: un extracto bancario): se extrae por
    # TRAMOS. Con una sola llamada la respuesta se corta y el documento no daba ningún dato.
    tramos = (
        split_text_chunks(rendered_by_id[unit.rows[0].inbox_id]) if len(unit.rows) == 1 else [None]
    )
    partes: list[str] = [
        json.dumps(
            [
                {
                    "id": unit.rows[0].inbox_id,
                    "ts": unit.rows[0].occurred_at.isoformat(),
                    "text": tramo,
                }
            ],
            ensure_ascii=False,
        )
        for tramo in tramos
        if tramo is not None
    ] or [messages_json]
    if len(partes) > _DOC_MAX_CHUNKS:
        # Recortar sería perder movimientos sin que nada lo diga; mejor fallar con el motivo.
        return _UnitOutcome(
            "error",
            {},
            {},
            None,
            f"documento demasiado largo: {len(partes)} tramos (tope {_DOC_MAX_CHUNKS})",
        )
    troceado = len(partes) > 1
    if troceado:
        _log.info(
            "extract.unit.chunked",
            inbox_ids=inbox_ids,
            chunks=len(partes),
            chars=len(rendered_by_id[unit.rows[0].inbox_id]),
        )

    results: list[LLMResult] = []
    for parte in partes:
        if unit.grouped:
            msgs = [
                ChatMessage("system", GROUPED_SYSTEM_PROMPT),
                ChatMessage("user", build_grouped_user_content(list(unit.modules), parte)),
            ]
            max_tokens = min(_EXTRACT_MAX_TOKENS * len(unit.modules), _GROUPED_MAX_TOKENS_CAP)
        else:
            msgs = [
                ChatMessage("system", unit.modules[0].extraction_prompt),
                ChatMessage("user", "Mensajes (JSON):\n" + parte),
            ]
            max_tokens = _EXTRACT_MAX_TOKENS
        if troceado or len(unit.rows) == 1:
            # El techo de salida NUNCA debe ser el motivo de perder datos. Un mensaje solo con
            # mucho contenido (un extracto) genera un JSON MUCHO más largo que su texto —medido:
            # 2.539 caracteres de extracto pedían más de 2.048 tokens de respuesta—, así que se le
            # da todo el techo disponible. No encarece nada: se paga por lo generado, no por el
            # tope. Los tramos van igual, cada uno pedido chico a propósito.
            max_tokens = _GROUPED_MAX_TOKENS_CAP
        results.append(
            await llm.complete(
                msgs, response_format="json_object", temperature=0.0, max_tokens=max_tokens
            )
        )
    result = _merge_results(results) if troceado else results[0]

    if not any(r.content.strip() for r in results):
        return _UnitOutcome("error", {}, {}, result, "empty content")
    # Truncada (p. ej. "length"): el JSON queda cortado → parse daría [] y cursorearíamos "0 items"
    # (pérdida silenciosa). Tratar como error reintentable: sin cursor. Con tramos basta que UNO se
    # corte para no dar el documento por bueno: faltarían movimientos sin que nada lo dijera.
    cortada = next(
        (r for r in results if r.finish_reason is not None and r.finish_reason not in _OK_FINISH),
        None,
    )
    if cortada is not None:
        return _UnitOutcome("error", {}, {}, result, f"truncated ({cortada.finish_reason})")

    lote = frozenset(inbox_ids)
    by_slug: dict[str, list[Any]] = {}
    for r in results:
        parsed = (
            parse_grouped_items(r.content, slugs)
            if unit.grouped
            else {unit.modules[0].slug: parse_items(r.content)}
        )
        for slug, items in parsed.items():
            by_slug.setdefault(slug, []).extend(items)
    valid_by_slug: dict[str, list[ExtractionItem]] = {}
    discarded_by_slug: dict[str, int] = {}
    verify_calls: list[VerifyCall] = []
    for module in unit.modules:
        raw_items = by_slug[module.slug]
        valid: list[ExtractionItem] = []
        for raw in raw_items:
            item = validate_item(
                module.extraction_schema, raw, lote=lote, rendered_by_id=rendered_by_id
            )
            if item is not None:
                valid.append(item)
        # SEGUNDA VERIFICACIÓN (`CAP_VERIFY`): el módulo revisa sus items válidos con un juez
        # atómico para los casos que su regla barata marque como sospechosos. Sigue siendo FASE 1
        # (sin efectos); las llamadas se registran en FASE 2. Un item descartado acá cuenta como
        # `discarded` (igual que uno inválido).
        if valid and CAP_VERIFY in module.capabilities:
            vctx = VerifyContext(
                user_id=user_id,
                client_for=verify_clients.get,
                occurred_at_by_id={r.inbox_id: r.occurred_at for r in unit.rows},
                rendered_by_id=rendered_by_id,
            )
            verified: VerifyOutcome = await module.verify(valid, vctx)  # type: ignore[attr-defined]
            verify_calls.extend(verified.calls)
            valid = list(verified.items)
        valid_by_slug[module.slug] = valid
        discarded_by_slug[module.slug] = len(raw_items) - len(valid)
    return _UnitOutcome("ok", valid_by_slug, discarded_by_slug, result, verify_calls=verify_calls)


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
) -> int:
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
    call_id = record_llm_call(
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
    return call_id


async def _persist_unit(
    user_id: int,
    llm: LLMClient,
    unit: _Unit,
    outcome: _UnitOutcome,
    stats: ExtractStats,
    active_by_slug: dict[str, InterestModule],
    *,
    trace_roots: Mapping[int, int] | None = None,
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
            # `ctx.trace` rutea cada entidad al span del módulo bajo el root de SU mensaje
            # (`source_inbox_ids[0]`), atómico con persist + cursor en esta tx. Sin roots (un test
            # que llama _persist_unit directo) → NULL_TRACER no-op.
            tracer: Tracer = (
                ModuleTracer(
                    conn, user_id=user_id, trace_roots=trace_roots, slug=module.slug, seq=i
                )
                if trace_roots
                else NULL_TRACER
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
        call_id = _record_grouped_cost(
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
        leaf_label = "Extracción agrupada"
    else:
        slug = unit.modules[0].slug
        call_id = _record_cost(
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
        leaf_label = f"Extracción · {slug}"
    # Hoja de costo COMPARTIDO de extracción: la call cubre los inbox de la unit → un nodo `llm`
    # bajo el root de CADA uno (tx propia, presentacional); `read_trace` reparte cost/N.
    if trace_roots:
        with connection() as conn:
            for iid in inbox_ids:
                root_node = attach_to_root(conn, user_id=user_id, inbox_id=iid)
                if root_node is not None:
                    # seq negativo: la hoja de extracción va arriba (antes de los spans de módulo).
                    root_node.llm(call_id, label=leaf_label, seq=-1500)
    # Llamadas de la SEGUNDA VERIFICACIÓN (FASE 1): una fila `llm_calls` por juicio, con su
    # `purpose` propio, y una hoja `llm` bajo el root del mensaje juzgado.
    for vc in outcome.verify_calls:
        _record_verify_call(user_id, vc, stats, source_id=source_id, trace_roots=trace_roots)
    _log.info(
        "extract.unit.done",
        slugs=slugs,
        n=n,
        inbox_ids=inbox_ids,
        items=sum(items_by_slug.values()),
    )


def _record_verify_call(
    user_id: int,
    vc: VerifyCall,
    stats: ExtractStats,
    *,
    source_id: int | None,
    trace_roots: Mapping[int, int] | None,
) -> int:
    """Registra en `llm_calls` una llamada de segunda verificación (`purpose` del módulo, p. ej.
    `calendar_date_verify`), suma su costo a `stats` y la cuelga de la traza de su mensaje."""
    r = vc.result
    call_id = record_llm_call(
        user_id=user_id,
        purpose=vc.purpose,
        model=r.model,
        prompt_tokens=r.usage.prompt_tokens,
        completion_tokens=r.usage.completion_tokens,
        cache_hit_tokens=r.usage.cache_hit_tokens,
        cost_usd=r.cost_usd,
        latency_ms=r.latency_ms,
        status=vc.status,
        inbox_id=vc.inbox_id,
        source_id=source_id,
        error_message=vc.error_message,
        metadata=vc.metadata,
        response_text=r.content,
    )
    stats.cost.record(
        source_id,
        prompt_tokens=r.usage.prompt_tokens,
        completion_tokens=r.usage.completion_tokens,
        cost_usd=r.cost_usd,
    )
    if trace_roots and vc.inbox_id is not None and vc.inbox_id in trace_roots:
        with connection() as conn:
            root_node = attach_to_root(conn, user_id=user_id, inbox_id=vc.inbox_id)
            if root_node is not None:
                root_node.llm(call_id, label=f"Verificación · {vc.purpose}", seq=-1400)
    return call_id


async def _process_window(
    user_id: int,
    llm: LLMClient,
    window: Window,
    active: list[InterestModule],
    active_by_slug: dict[str, InterestModule],
    stats: ExtractStats,
    *,
    route_chunk_size: int = _ROUTE_CHUNK_DEFAULT,
    routing_enabled: bool = True,
    batching_policy: str = "grouped",
    group_size: int = _GROUP_SIZE_DEFAULT,
    verify_clients: _ConsumerClients | None = None,
) -> None:
    kind = kind_for_type(window.rows[0].source_type)
    candidates = candidates_for_kind(kind, active)
    if kind is SourceKind.CHAT and any(m.slug in ("finance", "tareas") for m in candidates):
        # Finanzas en chats de GRUPO: lo que se dice de plata es de terceros o anuncios («recibirán
        # 5 XLM»), no movimientos del usuario. Solo un grupo íntimo (equipo, familia) cuenta.
        # Tareas: lo que hay que hacer sale de grupos donde la persona PARTICIPA (íntimo o
        # programa: el curso, la cohorte); en una comunidad de anuncios las obligaciones son de
        # otros.
        from memex.modules.chat_context import window_modes

        with connection() as conn:
            modes = window_modes(conn, window.source_id, [r.payload for r in window.rows])
        if "intimo" not in modes:
            candidates = [m for m in candidates if m.slug != "finance"]
        if not (modes & {"intimo", "programa"}):
            candidates = [m for m in candidates if m.slug != "tareas"]
    if not candidates:
        return  # el workset no debería traer kinds no-consumidos; defensivo
    if verify_clients is None:  # caller directo (tests): la verificación reusa `llm`
        verify_clients = _ConsumerClients(user_id, llm)

    # Paso 5 — REMITENTE de primera clase (+ estructura de chat) del LOTE, DETERMINISTA e
    # INDEPENDIENTE del ruteo LLM: todo lote teje a su remitente aunque el router no elija
    # identidades (el remitente —y el canal, en chat— son estructura del mensaje, no contenido a
    # extraer). Asimétrico por medio (chat→persona, email corporativo→org por dominio, social→org
    # por handle; ver `senders.weave_sender_structure`). Requiere identidades activa. Import local
    # (no acopla import-time).
    if "identidades" in active_by_slug:
        from memex.modules.identidades.senders import weave_sender_structure

        with connection() as conn:
            weave_sender_structure(conn, user_id, [r.inbox_id for r in window.rows], kind)

    with connection() as conn:
        done = _done_by_id(conn, [r.inbox_id for r in window.rows])
        # Traza jerárquica: un root POR mensaje de la ventana (delete-then-write → re-extraer
        # reemplaza). Se crean ANTES de rutear/persistir para que las hojas de costo compartido
        # (ruteo/extracción) y las entidades cuelguen de ellos. Cubre individual y lote por igual.
        trace_roots = {
            r.inbox_id: create_root(
                conn, user_id=user_id, inbox_id=r.inbox_id, label=f"mensaje #{r.inbox_id}"
            )
            for r in window.rows
        }

    chosen, route_call_ids = await _route(
        user_id,
        llm,
        window,
        candidates,
        active_by_slug,
        stats,
        route_chunk_size=route_chunk_size,
        routing_enabled=routing_enabled,
    )
    chosen_set = set(chosen)
    # Hojas de ruteo: la(s) call(s) de ruteo cubren TODA la ventana → un nodo `llm` bajo cada root;
    # `read_trace` reparte cost/N por el nº de nodos que referencian cada call.
    if route_call_ids:
        with connection() as conn:
            for r in window.rows:
                root_node = attach_to_root(conn, user_id=user_id, inbox_id=r.inbox_id)
                if root_node is not None:
                    for idx, cid in enumerate(route_call_ids):
                        # seq negativo (antes de la extracción y de los spans de módulo).
                        root_node.llm(cid, label="Ruteo", seq=-2000 + idx)

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
            return await _extract_unit(user_id, llm, u, verify_clients)

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
                user_id, llm, unit, outcome, stats, active_by_slug, trace_roots=trace_roots
            )

    # Resolvedor contextual por-correo (interno a identidades, gated por `resolver_enabled`): tras
    # extraer/persistir, dispone el remitente + funde/ubica con el contexto del correo. SIEMPRE hay
    # al menos el remitente → corre para todo correo. No-op si el gate está apagado. Patrón gemelo
    # del tejido del remitente (arriba): el orquestador gatilla la función de identidades y espera.
    if "identidades" in active_by_slug:
        from memex.modules.identidades.domain_attribution import attribute_domains_for_window
        from memex.modules.identidades.resolve_llm import run_resolver_window
        from memex.modules.identidades.verify_llm import run_hygiene_window

        # Resolvedor contextual (por mensaje, con LLM): está diseñado para CORREO (remitente,
        # dominio, asunto). En chats el remitente ya es determinista y en notas no hay remitente:
        # correrlo era una llamada por mensaje sin nada que resolver.
        if kind_for_type(window.rows[0].source_type) is SourceKind.EMAIL:
            await run_resolver_window(user_id, [r.inbox_id for r in window.rows])
        # Fallback conectado: ata a su org los dominios corporativos del remitente que la resolución
        # dejó sin dueña (gated por `resolver_enabled`; no-op si está apagado).
        await attribute_domains_for_window(user_id, [r.inbox_id for r in window.rows])
        # Higiene: re-resuelve menciones sueltas con el directorio ya actualizado (determinista)
        # y manda a un juez (codex) SOLO las fichas dudosas que esta ventana creó.
        await run_hygiene_window(user_id, [r.inbox_id for r in window.rows])

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
    max_window_size: int = EXTRACT_WINDOW_SIZE,
    route_chunk_size: int = _ROUTE_CHUNK_DEFAULT,
    routing_enabled: bool | None = None,
    batching_policy: str = "grouped",
    group_size: int = _GROUP_SIZE_DEFAULT,
    inbox_ids: list[int] | None = None,
    force: bool = False,
    client: LLMClient | None = None,
) -> ExtractStats:
    """Corre la extracción sobre el work-set clasificado no-extraído del user.

    Perillas (flags de CLI; ADR-015 §2): `max_window_size` (ventaneo por cantidad),
    `route_chunk_size` (sub-pasadas de ruteo con muchos módulos), `routing_enabled`
    (None = lee `extraction_settings`; False = sin ruteo LLM → extrae todos los candidatos),
    `batching_policy`
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

    if routing_enabled is None:  # None = resolver desde la perilla persistida (default ON)
        with connection() as conn:
            routing_enabled = get_extraction_settings(conn, user_id).routing_enabled

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
    windows = plan_windows(workset, max_window_size=max_window_size)
    if not windows:
        _log.info("extract.run.empty", user_id=user_id, source_id=source_id)
        return stats

    owns_client = client is None
    llm: LLMClient = client or build_llm_client("orchestrator", user_id=user_id)
    verify_clients = _ConsumerClients(user_id, client)
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
                    routing_enabled=routing_enabled,
                    batching_policy=batching_policy,
                    group_size=group_size,
                    verify_clients=verify_clients,
                )
                # La ventana salió bien: si alguno de sus mensajes arrastraba una falla previa
                # (`failing`/`review`), se limpia — la falla describe un intento, no al mensaje.
                clear_failures(user_id, STAGE_EXTRACT, [r.inbox_id for r in window.rows])
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
        await verify_clients.aclose()
        if owns_client:
            await aclose_llm(llm)

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
    llm: LLMClient = client or build_llm_client("orchestrator", user_id=user_id)
    verify_clients = _ConsumerClients(user_id, client)
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
        # Traza jerárquica: los roots (uno por mensaje de la ventana) se crean dentro de
        # `_process_window`, así individual y lote quedan cubiertos por igual.
        await _process_window(
            user_id, llm, window, active, active_by_slug, stats, verify_clients=verify_clients
        )
    finally:
        await verify_clients.aclose()
        if owns_client:
            await aclose_llm(llm)

    return {
        "status": "ok",
        "items": stats.items,
        "discarded": stats.discarded,
        "by_module": dict(stats.by_module),
        **_cost_fields(stats.cost.total),
        **read_extractions(user_id, inbox_id),
    }
