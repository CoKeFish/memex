"""Gemelos de asuntos: «UNA COSA DEL MUNDO, UN REGISTRO» aplicado al momento en que el asignador
propone abrir un asunto nuevo.

Por qué (2026-09-25): el asignador tenía en el catálogo «Beca Solsticio 2026 (FIB)» (abierto, con
la descripción «beca de FIB para HackSolsticio 2026, Lisboa 25-26 de octubre») y aun así abrió
«HackSolsticio Lisbon 2026» cuando llegó la confirmación del organizador. Nada lo frenaba: el
único chequeo de duplicados era el curador diario (apagado por el dueño) y su regla léxica
tampoco habría emparejado los dos nombres. Después las tareas se duplicaron en cada asunto.

Qué hace: ANTES de crear un asunto nuevo, lo contrasta con el catálogo vigente con reglas
baratas (nombre o alias igual; entidades en común; tokens distintivos en común; vecinos por
SIGNIFICADO con `memex.semantic`) y, si hay sospecha, un juez atómico (`asuntos_judge`, codex)
decide si es la misma cosa del mundo vista desde otro canal. Si lo es, el ref `nuevo:N` del lote
se redirige al asunto existente (su evidencia, hito, estado y relaciones van ahí; el nombre
propuesto queda como alias). El parecido semántico NUNCA decide solo: medido sobre los asuntos
del dueño, dos hackathons distintos puntúan 0,83 y el par duplicado real 0,81."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from memex.config import settings
from memex.core.observability import record_llm_call
from memex.db import connection
from memex.llm import ChatMessage, LLMClient, LLMError, LLMQuotaError, LLMResult
from memex.llm.config import LLMConfigError
from memex.llm.registry import aclose_llm, build_llm_client
from memex.logging import get_logger
from memex.modules.asuntos import store
from memex.modules.tareas.judge import Verdict, parse_verdict

_log = get_logger("memex.modules.asuntos.gemelos")

PURPOSE = "asuntos_judge"
#: Módulo dueño de los vectores de asuntos en `semantic_vectors` (texto: nombre + descripción).
SEMANTIC_SLUG = "asuntos"
#: Candidatas que llegan al juez por asunto propuesto (acota las llamadas).
MAX_CANDIDATES = 3
#: Piso de coseno propio de asuntos, por encima del general (`settings.semantic_min_score`, 0,62):
#: sobre los 83 asuntos del dueño (2026-09-26) el par duplicado real da 0,81 y un hackathon
#: distinto con fecha y ciudad distintas 0,68; con 0,62 ese ruido llegaba al juez primero.
SEMANTIC_MIN_SCORE = 0.70
#: Entidades en común para sospechar (dos nombres propios compartidos: el organizador y el evento).
MIN_SHARED_ENTITIES = 2
MIN_CONFIDENCE = 0.6
_MAX_TOKENS = 300
_TOKEN_RX = re.compile(r"[^\wáéíóúñü]+")
_STOP = frozenset(
    {"de", "del", "la", "el", "los", "las", "y", "en", "a", "para", "por", "con", "the", "of"}
)
#: Largo mínimo de un token para identificar un hilo («bootcamp», no «curso» ni «2026»).
_DISTINCTIVE_TOKEN_LEN = 7


@dataclass(frozen=True)
class TwinCandidate:
    """Un asunto del catálogo que podría ser la misma cosa que el propuesto."""

    asunto: store.Asunto
    reason: str
    score: float = 0.0
    #: Nombre o alias iguales: no hace falta juez.
    exact: bool = False
    #: Orden ante el juez: 2 = entidades en común (nombres propios compartidos), 1 = tokens
    #: distintivos, 0 = parecido por significado. Medido (2026-09-26): el par real compartía tres
    #: entidades y puntuaba 0,81; otro hackathon sin nada en común puntuaba 0,68 y salía primero.
    tier: int = 0


@dataclass(frozen=True)
class Proposed:
    """Lo que el asignador quiere abrir (subconjunto de `assign.NewAsunto`)."""

    ref: str
    name: str
    kind: str
    description: str
    entities: tuple[str, ...]


def asunto_text(name: str, description: str) -> str:
    """Texto que se embebe por asunto: el nombre y el principio de la descripción (el nombre solo
    no alcanza: «Beca Solsticio 2026 (FIB)» y «HackSolsticio Lisbon 2026» dan 0,52; con la
    descripción, 0,81)."""
    desc = " ".join(description.split())[:300]
    return f"{name.strip()}. {desc}" if desc else name.strip()


def _tokens(name: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN_RX.split(name.lower()) if t and t not in _STOP)


def _distinctive(tokens: frozenset[str]) -> set[str]:
    return {t for t in tokens if len(t) >= _DISTINCTIVE_TOKEN_LEN and not t.isdigit()}


def refresh_vectors(conn: Any, user_id: int, catalog: Sequence[store.Asunto]) -> int:
    """Asegura el vector de cada asunto del catálogo (salta los que no cambiaron). Cubre todas las
    vías de creación (asignador, CLI, agente, curador) sin tocarlas. Best-effort."""
    from memex.semantic.store import upsert_vectors

    try:
        return upsert_vectors(
            conn,
            user_id,
            SEMANTIC_SLUG,
            [(a.id, asunto_text(a.name, a.description)) for a in catalog],
        )
    except Exception as exc:  # pragma: no cover - defensivo
        _log.warning("asuntos.gemelos.vectors_failed", user_id=user_id, error=str(exc)[:200])
        return 0


def find_twin_candidates(
    conn: Any, user_id: int, proposed: Proposed, catalog: Sequence[store.Asunto]
) -> list[TwinCandidate]:
    """Asuntos del catálogo que podrían ser la misma cosa que `proposed`, de más a menos seguro:
    nombre/alias igual (exacto, sin juez) → entidades en común → tokens distintivos en común →
    vecinos por significado. Un `servicio` (pagos recurrentes) no se empareja con otra clase.
    Tope `MAX_CANDIDATES` para el juez."""
    from memex.semantic.store import neighbors

    wanted_names = {store.normalize_name(proposed.name)}
    wanted_tokens = _tokens(proposed.name)
    wanted_ents = {e.lower() for e in proposed.entities}
    out: list[TwinCandidate] = []
    seen: set[int] = set()
    for a in catalog:
        if a.merged_into_id is not None:
            continue
        if a.kind != proposed.kind and "servicio" in (a.kind, proposed.kind):
            continue
        names = {store.normalize_name(a.name), *(store.normalize_name(x) for x in a.aliases)}
        if names & wanted_names:
            return [TwinCandidate(a, "mismo nombre/alias", 1.0, exact=True)]
        ents = {e.lower() for e in a.entities}
        shared_ents = ents & wanted_ents
        toks = _tokens(a.name).union(*(_tokens(x) for x in a.aliases))
        shared_toks = _distinctive(toks & wanted_tokens)
        if len(shared_ents) >= MIN_SHARED_ENTITIES:
            out.append(
                TwinCandidate(
                    a,
                    f"entidades en común: {', '.join(sorted(shared_ents)[:3])}",
                    round(len(shared_ents) / max(len(wanted_ents), 1), 3),
                    tier=2,
                )
            )
            seen.add(a.id)
        elif len(shared_toks) >= 2 or (shared_toks and a.status == "candidato"):
            out.append(
                TwinCandidate(
                    a,
                    f"tokens en común: {', '.join(sorted(shared_toks)[:3])}",
                    round(len(shared_toks) / max(len(_distinctive(wanted_tokens)), 1), 3),
                    tier=1,
                )
            )
            seen.add(a.id)
    by_id = {a.id: a for a in catalog}
    scope = [a.id for a in catalog if a.id not in seen and a.merged_into_id is None]
    if scope:
        try:
            found = neighbors(
                conn,
                user_id,
                SEMANTIC_SLUG,
                asunto_text(proposed.name, proposed.description),
                scope_ids=scope,
                k=settings.semantic_top_k,
                min_score=SEMANTIC_MIN_SCORE,
            )
        except Exception as exc:  # pragma: no cover - defensivo
            _log.warning("asuntos.gemelos.neighbors_failed", user_id=user_id, error=str(exc)[:200])
            found = []
        for n in found:
            hit = by_id.get(n.ref_id)
            if hit is None or (
                hit.kind != proposed.kind and "servicio" in (hit.kind, proposed.kind)
            ):
                continue
            out.append(TwinCandidate(hit, f"parecido por significado ({n.score:.2f})", n.score))
    out.sort(key=lambda c: (-c.tier, -c.score, c.asunto.id))
    return out[:MAX_CANDIDATES]


# --- juez --------------------------------------------------------------------------------- #

JUDGE_SYSTEM_PROMPT = (
    "Sos un juez de ASUNTOS personales (hilos de la vida de una persona: un evento al que va, un\n"
    "programa que cursa, un trámite, un viaje, un proyecto). Te doy (A) un asunto que YA existe\n"
    "en su catálogo, con su descripción, entidades, fechas y una muestra de sus mensajes, y (B)\n"
    "un asunto que un lector automático propone abrir a partir de mensajes nuevos.\n\n"
    "Decidí si B es LA MISMA COSA DEL MUNDO que A, vista desde otro canal, otro remitente, otro\n"
    "idioma u otro nombre (la confirmación del organizador de algo a lo que la persona ya se\n"
    "postuló; el evento y la beca, el viaje o la postulación PARA ese mismo evento; la misma\n"
    "edición de un programa nombrada por su sigla), o una cosa DISTINTA (otra edición u otra\n"
    "fecha del mismo programa, otro evento del mismo organizador, otro trámite parecido).\n"
    "Pistas: mismas fechas y mismo lugar = misma; fechas o ciudad distintas = distinta; el\n"
    "organizador o la comunidad en común NO alcanza; que uno sea un paso o requisito del otro\n"
    "(la beca para ir al evento) SÍ es la misma cosa: un solo hilo.\n\n"
    'Respondé SOLO con JSON: {"same": true | false, "confidence": 0.0-1.0, '
    '"reason": "<corta>"}.'
)


def render_pair(existing: store.Asunto, sample: Sequence[str], proposed: Proposed) -> str:
    first = existing.first_signal_at.date().isoformat() if existing.first_signal_at else "?"
    last = existing.last_signal_at.date().isoformat() if existing.last_signal_at else "?"
    alias = f" (alias: {', '.join(existing.aliases[:3])})" if existing.aliases else ""
    lines = [
        "(A) Asunto del catálogo:",
        f"  nombre: {existing.name}{alias}",
        f"  tipo: {existing.kind} · estado del ciclo: {existing.status}",
        f"  descripción: {existing.description[:300] or '—'}",
        f"  entidades: {', '.join(existing.entities[:8]) or '—'}",
        f"  señal: {existing.evidence_count} mensajes, {first} → {last}",
        f"  estado vigente: {existing.state_text[:200] or '—'}",
        f"  muestra de mensajes: {'; '.join(sample[:5]) or '—'}",
        "",
        "(B) Asunto propuesto:",
        f"  nombre: {proposed.name}",
        f"  tipo: {proposed.kind}",
        f"  descripción: {proposed.description[:300] or '—'}",
        f"  entidades: {', '.join(proposed.entities[:8]) or '—'}",
    ]
    return "\n".join(lines)


async def judge_same_asunto(
    llm: LLMClient, *, existing: store.Asunto, sample: Sequence[str], proposed: Proposed
) -> tuple[Verdict, LLMResult]:
    msgs = [
        ChatMessage("system", JUDGE_SYSTEM_PROMPT),
        ChatMessage("user", render_pair(existing, sample, proposed)),
    ]
    result = await llm.complete(
        msgs, response_format="json_object", temperature=0.0, max_tokens=_MAX_TOKENS
    )
    return parse_verdict(result.content), result


def _judge_client(user_id: int) -> LLMClient:
    """Cliente del juez (`asuntos_judge`). Módulo-nivel para que los tests lo reemplacen."""
    return build_llm_client(PURPOSE, user_id=user_id)


def _sample(conn: Any, user_id: int, asunto_id: int) -> list[str]:
    ev = store.evidence_of(conn, user_id, asunto_id)
    return [
        f"[{e.occurred_at:%Y-%m-%d}] {e.sender[:30]}: {e.subject[:70]}"
        for e in ev[:5]
        if e.occurred_at
    ]


@dataclass(frozen=True)
class Twin:
    asunto_id: int
    reason: str


async def resolve_twins(
    user_id: int, proposed: Sequence[Proposed], catalog: Sequence[store.Asunto]
) -> dict[str, Twin]:
    """ref (`nuevo:N`) → asunto del catálogo que YA es esa cosa. Regla barata sobre el catálogo
    (con los vectores al día) y juez atómico para la sospecha; nombre/alias igual no pasa por el
    juez. Sin juez disponible (proveedor sin configurar, error) solo valen los exactos: mejor un
    asunto de más que fundir dos hilos distintos sin mirar."""
    if not proposed:
        return {}
    out: dict[str, Twin] = {}
    with connection() as conn:
        refresh_vectors(conn, user_id, catalog)
        cands = {p.ref: find_twin_candidates(conn, user_id, p, catalog) for p in proposed}
        samples = {
            c.asunto.id: _sample(conn, user_id, c.asunto.id)
            for cs in cands.values()
            for c in cs
            if not c.exact
        }
    llm: LLMClient | None = None
    unavailable = False
    try:
        for p in proposed:
            for c in cands.get(p.ref, []):
                if c.exact:
                    out[p.ref] = Twin(c.asunto.id, c.reason)
                    break
                if llm is None and not unavailable:
                    try:
                        llm = _judge_client(user_id)
                    except LLMConfigError as exc:
                        unavailable = True
                        _log.warning(
                            "asuntos.gemelos.judge_unavailable", user_id=user_id, error=str(exc)
                        )
                if llm is None:
                    break
                started = datetime.now()
                try:
                    verdict, result = await judge_same_asunto(
                        llm, existing=c.asunto, sample=samples.get(c.asunto.id, []), proposed=p
                    )
                except LLMQuotaError:
                    raise
                except LLMError as exc:
                    _log.warning(
                        "asuntos.gemelos.judge_error",
                        user_id=user_id,
                        name=p.name[:80],
                        error=str(exc)[:200],
                    )
                    continue
                latency = int((datetime.now() - started).total_seconds() * 1000)
                record_llm_call(
                    user_id=user_id,
                    purpose=PURPOSE,
                    model=result.model,
                    prompt_tokens=result.usage.prompt_tokens,
                    completion_tokens=result.usage.completion_tokens,
                    cache_hit_tokens=result.usage.cache_hit_tokens,
                    cost_usd=result.cost_usd or Decimal("0"),
                    latency_ms=result.latency_ms or latency,
                    status="ok",
                    metadata={
                        "asunto_id": c.asunto.id,
                        "proposed": p.name[:120],
                        "reason": c.reason,
                        "same": verdict.same,
                        "confidence": verdict.confidence,
                    },
                    response_text=result.content[:500] if result.content else None,
                )
                _log.info(
                    "asuntos.gemelos.judged",
                    user_id=user_id,
                    asunto_id=c.asunto.id,
                    proposed=p.name[:80],
                    candidate_reason=c.reason,
                    same=verdict.same,
                    confidence=verdict.confidence,
                    reason=verdict.reason[:120],
                )
                if verdict.same and verdict.confidence >= MIN_CONFIDENCE:
                    out[p.ref] = Twin(c.asunto.id, f"{c.reason}; juez: {verdict.reason[:80]}")
                    break
    finally:
        if llm is not None:
            await aclose_llm(llm)
    return out


def as_proposed(items: Sequence[Any]) -> list[Proposed]:
    """`assign.NewAsunto` → `Proposed` (duck-typed para no importar assign desde acá)."""
    return [
        Proposed(
            ref=str(n.ref),
            name=str(n.name),
            kind=str(n.kind),
            description=str(n.description or ""),
            entities=tuple(str(e) for e in (n.entities or ())),
        )
        for n in items
    ]


def twins_as_ids(twins: Mapping[str, Twin]) -> dict[str, int]:
    return {ref: t.asunto_id for ref, t in twins.items()}
