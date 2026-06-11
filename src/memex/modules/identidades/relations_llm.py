"""Handler LLM de CO-OCURRENCIA de identidades (overflow del tope de fan-out).

El paso DETERMINISTA del grafo (`relations.deterministic`) descarta un correo ENTERO cuando tiene
más de `cap` vértices: ahí la co-ocurrencia todos-contra-todos sería ruido (C(n,2) aristas). Las
identidades son el tipo que más rompe ese tope (hilos densos de gente), así que justo los enlaces
humanos más reales se perderían. Este handler toma el RELEVO solo para esos correos y SOLO para
identidad↔identidad: por cada correo con más de `cap` identidades distintas, le pregunta al LLM —con
la evidencia que ya está en `mod_identidades_mentions`, sin leer el cuerpo crudo— qué PARES están
genuinamente relacionados en ESE mensaje, e ignora las identidades de ruido.

A diferencia de la co-ocurrencia barata (que nace `pista`, sin vouchar), estas aristas nacen
`confirmed` (`producer='llm'`): el LLM las avaló. NO hay doble-emisión — `identity_count > cap`
implica `total_vertices > cap`, así que el determinista ya saltó ese correo. NO toca ningún otro
tipo de vértice.

GROUNDER: cada par debe venir con una cita textual (`quote`) de la evidencia que el LLM vio; la
verificación de contención es DETERMINISTA (normalizar + substring) y un par sin cita verificable
se DESCARTA (`stats.ungrounded`), no se propone. Esto cambia el recall a propósito (sesgo a
precisión): una mención con `evidence=''` no puede anclar ningún par.

Molde: `hierarchy.py` (estructura/observabilidad, UNA llamada por unidad) + `dedup_llm.py` (loop
best-effort por ítem). Cada llamada se registra en `llm_calls` (`purpose=identidades_cooccurrence`).
Cliente LLM inyectable (tests con fake). `LLMQuotaError` se propaga (el scheduler la captura).
Idempotente por el `ON CONFLICT` de `propose_edge`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.observability import CostAccum, record_llm_call
from memex.core.trace import attach_to_root
from memex.db import connection
from memex.llm import ChatMessage, DeepSeekClient, LLMClient, LLMConfig, LLMResult
from memex.llm.client import LLMQuotaError
from memex.llm.grounding import DEFAULT_MIN_QUOTE_NORM_LEN, grounded
from memex.logging import get_logger
from memex.modules.identidades.prompt import IDENTIDADES_COOCCURRENCE_SYSTEM_PROMPT
from memex.relations.deterministic import DEFAULT_COOCCURRENCE_CAP
from memex.relations.edges import PRODUCER_LLM, STATUS_CONFIRMED, Ref, propose_edge
from memex.relations.vertices import IDENTITY_SLUG_BY_KIND

_log = get_logger("memex.modules.identidades.relations_llm")

#: Correos over-cap procesados por corrida (cota holgada; cada uno es UNA llamada LLM).
_DEFAULT_LIMIT = 100
#: Evidencia por mención truncada para acotar los tokens de entrada de correos densos.
_EVIDENCE_MAX = 200
#: Un correo absurdamente denso (cientos de identidades) se SALTA y se loguea, no se manda al LLM.
_MAX_IDENTITIES = 80
#: La salida es una lista de pares; cota holgada para un correo denso.
_MAX_TOKENS = 4096
#: Largo mínimo del `quote` NORMALIZADO para aceptar un par (p50 del largo de evidencia real ≈ 36).
#: La perilla y su doctrina viven en `memex.llm.grounding` (grounder compartido).
_MIN_QUOTE_NORM_LEN = DEFAULT_MIN_QUOTE_NORM_LEN


@dataclass(frozen=True)
class MentionedIdentity:
    """Una identidad mencionada en un correo, con el contexto que el LLM ve."""

    id: int
    kind: str  # 'persona' | 'organizacion' | 'producto'
    display_name: str
    evidence: str


@dataclass
class CooccurrenceStats:
    """Resumen de una corrida del handler LLM de co-ocurrencia de identidades."""

    emails: int = 0  # correos over-cap considerados
    edges: int = 0  # pares confirmed propuestos (idempotente; re-correr re-propone)
    skipped: int = 0  # correos saltados por demasiadas identidades (_MAX_IDENTITIES)
    errors: int = 0
    ungrounded: int = 0  # pares del LLM descartados por el grounder (cita ausente/corta/no hallada)
    #: Costo LLM acumulado (identidades es source-less → se atribuye por `purpose` + `inbox_id`).
    cost: CostAccum = field(default_factory=CostAccum)


def _slug(kind: str) -> str:
    """Slug de grafo del vértice de identidad (mapa único en `relations.vertices`)."""
    return IDENTITY_SLUG_BY_KIND[kind]


# --- carga (sin LLM) --------------------------------------------------------------- #


def _find_overcap_emails(conn: Connection, user_id: int, cap: int, limit: int) -> list[int]:
    """Inbox ids de los correos con MÁS de `cap` identidades distintas resueltas (los que el paso
    determinista descarta por fan-out). Cuenta SOLO identidades (interno del módulo)."""
    rows = (
        conn.execute(
            text(
                """
                SELECT mid
                FROM mod_identidades_mentions m
                CROSS JOIN LATERAL unnest(m.source_inbox_ids) AS mid
                WHERE m.user_id = :u AND m.resolved_identity_id IS NOT NULL
                GROUP BY mid
                HAVING COUNT(DISTINCT m.resolved_identity_id) > :cap
                ORDER BY mid
                LIMIT :lim
                """
            ),
            {"u": user_id, "cap": cap, "lim": limit},
        )
        .scalars()
        .all()
    )
    return [int(x) for x in rows]


def _load_email_identities(conn: Connection, user_id: int, mid: int) -> list[MentionedIdentity]:
    """Identidades distintas resueltas en el correo `mid`, con su evidencia (truncada). Una fila por
    identidad (`DISTINCT ON`): si la nombran varias veces, toma la primera mención."""
    rows = (
        conn.execute(
            text(
                """
                SELECT DISTINCT ON (i.id)
                       i.id AS id, i.kind AS kind, i.display_name AS name, m.evidence AS evidence
                FROM mod_identidades_mentions m
                JOIN mod_identidades i ON i.id = m.resolved_identity_id
                WHERE m.user_id = :u AND :mid = ANY(m.source_inbox_ids)
                ORDER BY i.id, m.id
                """
            ),
            {"u": user_id, "mid": mid},
        )
        .mappings()
        .all()
    )
    return [
        MentionedIdentity(
            id=int(r["id"]),
            kind=str(r["kind"]),
            display_name=str(r["name"]),
            evidence=(str(r["evidence"] or ""))[:_EVIDENCE_MAX],
        )
        for r in rows
    ]


# --- parseo de la respuesta del LLM ------------------------------------------------ #


def _grounded(quote: str, ev_a: str, ev_b: str) -> bool:
    """¿La cita está realmente en la evidencia que el LLM vio (la de a o la de b)? Delega en el
    grounder compartido (`memex.llm.grounding`), extraído de acá sin cambio de comportamiento."""
    return grounded(quote, ev_a, ev_b, min_len=_MIN_QUOTE_NORM_LEN)


def _parse_pairs(content: str, valid_ids: set[int]) -> list[tuple[int, int, str]]:
    """Parsea `{"pairs":[{"a_id","b_id","quote"}]}`. ULTRA-DEFENSIVO (molde
    `hierarchy._parse_links`): basura → `[]`; descarta ids ∉ `valid_ids`, bool-como-int y
    self-pares; canoniza `a<b`, dedup (la primera cita gana). `quote` ausente/no-string → `""`
    (el grounder lo descartará después)."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("pairs")
    if not isinstance(raw, list):
        return []
    out: list[tuple[int, int, str]] = []
    seen: set[tuple[int, int]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        a = item.get("a_id")
        b = item.get("b_id")
        if not (isinstance(a, int) and not isinstance(a, bool) and a in valid_ids):
            continue
        if not (isinstance(b, int) and not isinstance(b, bool) and b in valid_ids):
            continue
        if a == b:
            continue
        pair = (a, b) if a < b else (b, a)
        if pair in seen:
            continue
        seen.add(pair)
        q = item.get("quote")
        out.append((pair[0], pair[1], q.strip() if isinstance(q, str) else ""))
    return out


def _serialize(identities: list[MentionedIdentity]) -> str:
    lines = ["IDENTIDADES MENCIONADAS EN EL MENSAJE (id: tipo — nombre — evidencia):"]
    for i in identities:
        ev = f" — {i.evidence}" if i.evidence else ""
        lines.append(f"{i.id}: {i.kind} — {i.display_name}{ev}")
    return "\n".join(lines)


async def propose_relations(
    llm: LLMClient, identities: list[MentionedIdentity]
) -> tuple[list[tuple[int, int, str]], LLMResult]:
    """Le pide al LLM los pares relacionados de las identidades de UN correo. Devuelve los pares
    válidos con su cita (parseados/filtrados contra los ids reales) + el LLMResult (el costo)."""
    valid_ids = {i.id for i in identities}
    result = await llm.complete(
        [
            ChatMessage("system", IDENTIDADES_COOCCURRENCE_SYSTEM_PROMPT),
            ChatMessage("user", _serialize(identities)),
        ],
        response_format="json_object",
        temperature=0.0,
        max_tokens=_MAX_TOKENS,
    )
    return _parse_pairs(result.content, valid_ids), result


# --- worker ------------------------------------------------------------------------ #


async def run_cooccurrence_llm(
    user_id: int,
    *,
    cap: int = DEFAULT_COOCCURRENCE_CAP,
    limit: int = _DEFAULT_LIMIT,
    client: LLMClient | None = None,
) -> CooccurrenceStats:
    """Resuelve la co-ocurrencia identidad↔identidad de los correos over-cap del user con el LLM.
    Una llamada por correo, best-effort (un correo fallido no frena los demás). Emite aristas
    `confirmed`/`producer='llm'`. Idempotente. `client` inyectable (tests con fake). `LLMQuotaError`
    se propaga (el scheduler la captura)."""
    stats = CooccurrenceStats()
    with connection() as conn:
        mids = _find_overcap_emails(conn, user_id, cap, limit)
    if not mids:
        _log.info("identidades.cooccurrence.empty", user_id=user_id)
        return stats

    owns_client = client is None
    llm: LLMClient = client if client is not None else DeepSeekClient(LLMConfig.from_env())
    _log.info("identidades.cooccurrence.start", user_id=user_id, emails=len(mids))
    try:
        for mid in mids:
            stats.emails += 1
            with connection() as conn:
                identities = _load_email_identities(conn, user_id, mid)
            if len(identities) < 2:
                continue
            if len(identities) > _MAX_IDENTITIES:
                stats.skipped += 1
                _log.info(
                    "identidades.cooccurrence.skip_too_many",
                    inbox_id=mid,
                    identities=len(identities),
                )
                continue
            try:
                pairs, result = await propose_relations(llm, identities)
            except LLMQuotaError:
                raise  # propaga: el scheduler corta los pasos LLM restantes
            except Exception as e:  # best-effort: un correo fallido no frena los demás
                stats.errors += 1
                _log.error(
                    "identidades.cooccurrence.email_failed",
                    inbox_id=mid,
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
                continue
            kind_by_id = {i.id: i.kind for i in identities}
            # Las MISMAS strings truncadas que vio el LLM (`_load_email_identities` trunca al
            # cargar): el grounding se verifica contra el prompt, no contra la DB completa.
            ev_by_id = {i.id: i.evidence for i in identities}
            ungrounded_email = 0
            with connection() as conn:
                for a, b, quote in pairs:
                    if not _grounded(quote, ev_by_id[a], ev_by_id[b]):
                        ungrounded_email += 1
                        stats.ungrounded += 1
                        # Largo y no el texto: la cita es payload personal.
                        _log.info(
                            "identidades.cooccurrence.ungrounded",
                            inbox_id=mid,
                            a_id=a,
                            b_id=b,
                            quote_len=len(quote),
                        )
                        continue
                    propose_edge(
                        conn,
                        user_id,
                        Ref(_slug(kind_by_id[a]), a),
                        Ref(_slug(kind_by_id[b]), b),
                        producer=PRODUCER_LLM,
                        relation_type="co-ocurrencia",
                        status=STATUS_CONFIRMED,
                        evidence=f"inbox:{mid} | {quote}",
                    )
                    stats.edges += 1
            call_id = record_llm_call(
                user_id=user_id,
                purpose="identidades_cooccurrence",
                model=result.model,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cache_hit_tokens=result.usage.cache_hit_tokens,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                status="ok",
                # `inbox_id` (columna FK) NO se setea: `source_inbox_ids` es FK-less a propósito (el
                # inbox puede purgarse y la mención sobrevive), así que el FK fallaría. El correo va
                # en metadata para la traza; el costo se atribuye por `purpose`.
                source_id=None,
                metadata={
                    "inbox_id": mid,
                    "identities": len(identities),
                    "pairs": len(pairs),
                    "ungrounded": ungrounded_email,
                },
            )
            # Traza: la co-ocurrencia es per-mensaje pero no produce fila de dominio con `entity`
            # (materializa `relation_edges`) → cuelga su costo bajo el ROOT del mensaje. No-op si
            # el correo no se extrajo por-mensaje (sin root).
            with connection() as conn:
                node = attach_to_root(conn, user_id=user_id, inbox_id=mid)
                if node is not None:
                    node.llm(
                        call_id,
                        label="co-ocurrencia",
                        status="ok",
                        detail={
                            "identities": len(identities),
                            "pairs": len(pairs),
                            "ungrounded": ungrounded_email,
                        },
                    )
            stats.cost.calls += 1
            stats.cost.prompt_tokens += result.usage.prompt_tokens
            stats.cost.completion_tokens += result.usage.completion_tokens
            stats.cost.cost_usd += result.cost_usd
    finally:
        if owns_client and isinstance(llm, DeepSeekClient):
            await llm.aclose()

    _log.info(
        "identidades.cooccurrence.end",
        user_id=user_id,
        emails=stats.emails,
        edges=stats.edges,
        ungrounded=stats.ungrounded,
        skipped=stats.skipped,
        errors=stats.errors,
        llm_calls=stats.cost.calls,
        llm_cost_usd=str(stats.cost.cost_usd),
    )
    return stats
