"""Zona gris del resolver par-por-par: una llamada LLM POR MENSAJE decide todos sus pares.

El prefiltro determinista (`resolve.py`) ya separó lo decidible por regla; acá el LLM ABRE el
mensaje (render completo + OCR, lo que el partidor nunca hace) y juzga cada par de co-ocurrencia
gris: ¿este mensaje evidencia una relación real o una co-aparición casual? Agrupar POR MENSAJE
amortiza el prompt (patrón BATCHER) y da el grounding natural: un `confirm` exige cita textual
del MISMO texto renderizado/truncado que vio el LLM, verificada determinista por el grounder
compartido (`memex.llm.grounding`); sin cita verificable se degrada a `dejar` (sesgo a precisión,
ODKE+).

Si el summarizer ya pagó un resumen del mensaje, entra al prompt como CONTEXTO AUXILIAR en un
bloque delimitado como DERIVADO (`_summary_block`, truncado a `resolve_summary_max_chars`). El
resumen jamás se concatena al render: el grounding sigue verificando solo contra el original,
así que una cita sacada del resumen no groundea y degrada a `dejar` por construcción. Un mensaje
purgado con resumen vivo queda igual que hoy (render vacío): el resumen no lo sustituye.

Agregación por par multi-mensaje: algún confirm grounded con confianza ≥ umbral → `confirmed`
(la mejor cita gana); TODOS sus mensajes evaluados y todos `reject` ≥ umbral → `rejected`;
evaluado completo sin veredicto → memo `dejar` (por `evidence_sig`). Un par que el presupuesto
dejó a medias NO escribe nada: queda pendiente real y se reintenta (memoizarlo lo congelaría).

Molde: `clusters_llm` (loop best-effort, cliente inyectable, parseo ultra-defensivo) +
`identidades/relations_llm` (registro per-mensaje: `inbox_id` va en metadata —la columna FK no se
setea, el inbox puede purgarse— y el costo cuelga del ROOT de traza del mensaje si existe).
`LLMQuotaError`: aplica lo ya pagado y PROPAGA.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.config import settings
from memex.core.observability import record_llm_call
from memex.core.trace import attach_to_root
from memex.db import connection
from memex.llm import ChatMessage, LLMClient, LLMResult, aclose_llm, build_llm_client
from memex.llm.client import LLMQuotaError
from memex.llm.grounding import grounded
from memex.logging import get_logger
from memex.processing.render import render_payload
from memex.relations.decisions import (
    METHOD_LLM,
    VERDICT_CONFIRM,
    VERDICT_DEJAR,
    VERDICT_REJECT,
    record_decision,
)
from memex.relations.edges import STATUS_CONFIRMED, STATUS_REJECTED, Ref, resolve_edge
from memex.relations.prompt import GRAPH_RESOLVE_SYSTEM_PROMPT
from memex.relations.resolve import LABEL_BULK, MessageLabeler, ResolvePair, ResolveStats
from memex.relations.vertices import Vertex, list_vertices
from memex.summarizer.lookup import InboxSummary

_log = get_logger("memex.relations.resolve_llm")

#: Cota de salida: una lista de veredictos cortos por par.
_MAX_TOKENS = 4096

_VALID_LLM_VERDICTS = frozenset({"confirm", "reject", "dejar"})

#: Nota neutra al LLM cuando el mensaje declara marcadores de correo masivo: es CONTEXTO (prior
#: del prefiltro), nunca un veredicto sembrado — el dueño advirtió el sesgo.
_BULK_NOTE = (
    "[Señal determinista: el mensaje declara encabezados de correo masivo (lista/newsletter). "
    "Es contexto, no un veredicto: un correo masivo igual puede contener una relación real.]"
)


@dataclass(frozen=True)
class PairVerdict:
    """El veredicto del LLM para UN par (id local 1..n) en UN mensaje."""

    pair: int
    verdict: str
    quote: str
    confidence: float


def parse_verdicts(content: str, n_pairs: int) -> dict[int, PairVerdict]:
    """Parsea `{"verdicts":[{pair, verdict, quote, confidence}]}`. ULTRA-DEFENSIVO (molde
    `parse_partition`/`_parse_pairs`): basura → `{}`; ids fuera de 1..n o bool-como-int fuera;
    verdict fuera del vocabulario fuera; dedup (el primero gana); quote no-string → `""`;
    confianza clampeada 0..1."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), list):
        return {}
    out: dict[int, PairVerdict] = {}
    for item in data["verdicts"]:
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
        quote = item.get("quote")
        conf = item.get("confidence")
        confidence = (
            float(conf) if isinstance(conf, int | float) and not isinstance(conf, bool) else 0.0
        )
        out[pid] = PairVerdict(
            pair=pid,
            verdict=verdict,
            quote=quote.strip() if isinstance(quote, str) else "",
            confidence=max(0.0, min(1.0, confidence)),
        )
    return out


def load_rendered(conn: Connection, user_id: int, inbox_id: int) -> str:
    """El mensaje renderizado a texto plano (payload + OCR de sus imágenes, patrón
    `modules/workset.py`), truncado a `resolve_render_max_chars`. El grounding se verifica contra
    ESTE string (el mismo que ve el LLM), no contra la DB completa. Mensaje purgado → ""."""
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
    return render_payload(dict(row[0]), str(row[1] or ""))[: settings.resolve_render_max_chars]


def _vlabel(ref: Ref, vmap: dict[Ref, Vertex]) -> str:
    v = vmap.get(ref)
    return f"{v.label} ({v.kind})" if v is not None else f"{ref.slug}#{ref.id}"


def _summary_block(info: InboxSummary | None) -> str:
    """El bloque RESUMEN PREVIO pre-formateado para el prompt; `""` si no hay resumen o el knob
    está en 0 (cero cambio de prompt). El contenido se trunca a `resolve_summary_max_chars` y
    NUNCA entra al string de grounding: citarlo no verifica → degrada a `dejar`."""
    max_chars = settings.resolve_summary_max_chars
    if info is None or max_chars <= 0:
        return ""
    lines = [
        "RESUMEN PREVIO (derivado del mensaje, NO citable — la cita debe salir del MENSAJE):",
        info.content[:max_chars],
    ]
    if info.n > 1:
        lines.append(
            f"[Resumen de un LOTE de {info.n} mensajes: puede mencionar cosas que no están "
            "en ESTE mensaje.]"
        )
    return "\n".join(lines)


def _serialize(
    rendered: str,
    pairs: list[ResolvePair],
    vmap: dict[Ref, Vertex],
    note: str,
    summary_block: str = "",
) -> str:
    """El cuerpo del prompt: nota de señales (si hay) + RESUMEN PREVIO (si hay) + MENSAJE
    renderizado + PARES numerados."""
    lines: list[str] = []
    if note:
        lines.append(note)
        lines.append("")
    if summary_block:
        lines.append(summary_block)
        lines.append("")
    lines.append("MENSAJE:")
    lines.append(rendered if rendered else "(mensaje vacío o purgado)")
    lines.append("")
    lines.append("PARES A JUZGAR (id: entidad ↔ entidad):")
    for i, p in enumerate(pairs, start=1):
        lines.append(f"{i}: {_vlabel(p.edge.src, vmap)} ↔ {_vlabel(p.edge.dst, vmap)}")
    return "\n".join(lines)


async def judge_message(
    llm: LLMClient,
    rendered: str,
    pairs: list[ResolvePair],
    vmap: dict[Ref, Vertex],
    note: str = "",
    summary_block: str = "",
) -> tuple[dict[int, PairVerdict], LLMResult]:
    """UNA llamada que juzga todos los pares grises de UN mensaje. Devuelve los veredictos
    parseados (por id local 1..n) + el LLMResult (costo)."""
    result = await llm.complete(
        [
            ChatMessage("system", GRAPH_RESOLVE_SYSTEM_PROMPT),
            ChatMessage("user", _serialize(rendered, pairs, vmap, note, summary_block)),
        ],
        response_format="json_object",
        temperature=0.0,
        max_tokens=_MAX_TOKENS,
    )
    return parse_verdicts(result.content, len(pairs)), result


@dataclass(frozen=True)
class _Vote:
    """El veredicto de UN mensaje sobre UN par, ya pasado por el grounder."""

    inbox_id: int
    verdict: str
    quote: str
    confidence: float
    grounded: bool


def _apply_gray_verdicts(
    user_id: int,
    pairs: list[ResolvePair],
    votes: dict[int, list[_Vote]],
    evaluated: dict[int, set[int]],
    *,
    run_id: str,
    stats: ResolveStats,
) -> None:
    """Agrega los votos por par y aplica en UNA tx. Confirm grounded ≥ umbral → confirmed (la
    mejor cita); todos los mensajes evaluados y todos reject ≥ umbral → rejected; evaluado
    COMPLETO sin veredicto → memo `dejar`; parcial (presupuesto) → nada (pendiente real)."""
    min_conf = settings.resolve_min_confidence
    with connection() as conn:
        for p in sorted(pairs, key=lambda p: p.edge.id):
            vs = votes.get(p.edge.id, [])
            if not vs:
                continue
            confirms = [
                v for v in vs if v.verdict == "confirm" and v.grounded and v.confidence >= min_conf
            ]
            if confirms:
                best = max(confirms, key=lambda v: (v.confidence, -v.inbox_id))
                if resolve_edge(
                    conn,
                    p.edge.id,
                    status=STATUS_CONFIRMED,
                    confidence=_dec(best.confidence),
                ):
                    record_decision(
                        conn,
                        user_id,
                        p.edge.id,
                        verdict=VERDICT_CONFIRM,
                        method=METHOD_LLM,
                        inbox_id=best.inbox_id,
                        quote=best.quote,
                        confidence=_dec(best.confidence),
                        evidence_sig=p.sig,
                        run_id=run_id,
                    )
                    stats.llm_confirmed += 1
                continue
            fully = evaluated.get(p.edge.id, set()) >= p.inbox_ids
            if not fully:
                continue  # presupuesto a medias: ni terminal ni memo (se reintenta)
            if vs and all(v.verdict == "reject" and v.confidence >= min_conf for v in vs):
                best = max(vs, key=lambda v: (v.confidence, -v.inbox_id))
                if resolve_edge(
                    conn,
                    p.edge.id,
                    status=STATUS_REJECTED,
                    confidence=_dec(best.confidence),
                ):
                    record_decision(
                        conn,
                        user_id,
                        p.edge.id,
                        verdict=VERDICT_REJECT,
                        method=METHOD_LLM,
                        inbox_id=best.inbox_id,
                        confidence=_dec(best.confidence),
                        evidence_sig=p.sig,
                        run_id=run_id,
                    )
                    stats.llm_rejected += 1
                continue
            record_decision(
                conn,
                user_id,
                p.edge.id,
                verdict=VERDICT_DEJAR,
                method=METHOD_LLM,
                evidence_sig=p.memo_sig,
                run_id=run_id,
            )
            stats.llm_dejar += 1


def _dec(x: float) -> Decimal:
    return Decimal(str(round(x, 3)))


async def resolve_gray_zone(
    user_id: int,
    pairs: list[ResolvePair],
    *,
    labeler: MessageLabeler,
    budget: int,
    run_id: str,
    stats: ResolveStats,
    summaries: Mapping[int, InboxSummary] | None = None,
    client: LLMClient | None = None,
) -> None:
    """El loop de la zona gris: mensajes ordenados por #pares DESC (máxima amortización), tope
    `budget` llamadas; cada llamada juzga los pares PENDIENTES del mensaje (los ya confirmados en
    esta corrida se saltan). `summaries` es el snapshot por inbox que cargó `_attach_evidence`
    (la MISMA foto que firmó las `memo_sig`): el resumen entra al prompt como contexto auxiliar.
    Aplica la agregación al final — también si el presupuesto o la cuota cortaron antes (lo
    pagado no se tira). Muta `stats` en el caller."""
    by_msg: dict[int, list[ResolvePair]] = defaultdict(list)
    for p in pairs:
        for mid in sorted(p.inbox_ids):
            by_msg[mid].append(p)
    order = sorted(by_msg, key=lambda m: (-len(by_msg[m]), m))

    with connection() as conn:
        vmap = {v.ref: v for v in list_vertices(conn, user_id)}

    votes: dict[int, list[_Vote]] = defaultdict(list)
    evaluated: dict[int, set[int]] = defaultdict(set)
    confirmed_ids: set[int] = set()
    min_conf = settings.resolve_min_confidence
    calls = 0
    quota: LLMQuotaError | None = None

    owns_client = client is None
    llm: LLMClient = client or build_llm_client("relations_resolve", user_id=user_id)
    _log.info(
        "relation.resolve.gray.start",
        user_id=user_id,
        pairs=len(pairs),
        messages=len(by_msg),
        budget=budget,
    )
    try:
        for mid in order:
            pend = [p for p in by_msg[mid] if p.edge.id not in confirmed_ids]
            if not pend:
                continue
            if calls >= budget:
                stats.budget_exhausted = True
                _log.info("relation.resolve.gray.budget_exhausted", calls=calls, budget=budget)
                break
            pend = pend[: settings.resolve_max_pairs_per_call]
            with connection() as conn:
                rendered = load_rendered(conn, user_id, mid)
            note = _BULK_NOTE if labeler.label(mid) == LABEL_BULK else ""
            info = (summaries or {}).get(mid)
            try:
                verdicts, result = await judge_message(
                    llm, rendered, pend, vmap, note, _summary_block(info)
                )
            except LLMQuotaError as e:
                quota = e  # aplica lo ya pagado y después propaga
                break
            except Exception as e:  # best-effort: un mensaje fallido no frena los demás
                stats.errors += 1
                _log.error(
                    "relation.resolve.judge_failed",
                    inbox_id=mid,
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
                continue
            calls += 1
            ungrounded_msg = 0
            for i, p in enumerate(pend, start=1):
                v = verdicts.get(i)
                if v is None:  # el LLM omitió el par: no decidió sobre él
                    vote = _Vote(mid, "dejar", "", 0.0, False)
                elif v.verdict == "confirm" and not grounded(v.quote, rendered):
                    ungrounded_msg += 1
                    stats.ungrounded += 1
                    # Largo y no el texto: la cita es payload personal.
                    _log.info(
                        "relation.resolve.ungrounded",
                        inbox_id=mid,
                        edge_id=p.edge.id,
                        quote_len=len(v.quote),
                    )
                    vote = _Vote(mid, "dejar", "", v.confidence, False)
                else:
                    vote = _Vote(mid, v.verdict, v.quote, v.confidence, v.verdict == "confirm")
                votes[p.edge.id].append(vote)
                evaluated[p.edge.id].add(mid)
                if vote.verdict == "confirm" and vote.grounded and vote.confidence >= min_conf:
                    confirmed_ids.add(p.edge.id)
            call_id = record_llm_call(
                user_id=user_id,
                purpose="graph_resolve",
                model=result.model,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cache_hit_tokens=result.usage.cache_hit_tokens,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                status="ok",
                # La columna FK `inbox_id` NO se setea (el inbox puede purgarse); el mensaje va en
                # metadata y el costo se atribuye por `purpose` (molde identidades_cooccurrence).
                source_id=None,
                metadata={
                    "inbox_id": mid,
                    "pairs": len(pend),
                    "ungrounded": ungrounded_msg,
                    "summary_id": info.summary_id if info is not None else None,
                },
            )
            # Traza: el veredicto es per-mensaje pero no produce fila de dominio → cuelga su costo
            # bajo el ROOT del mensaje si existe (no-op si el correo no se extrajo por-mensaje).
            with connection() as conn:
                node = attach_to_root(conn, user_id=user_id, inbox_id=mid)
                if node is not None:
                    node.llm(
                        call_id,
                        label="veredicto co-ocurrencia",
                        status="ok",
                        detail={"pairs": len(pend), "ungrounded": ungrounded_msg},
                    )
            stats.cost.calls += 1
            stats.cost.prompt_tokens += result.usage.prompt_tokens
            stats.cost.completion_tokens += result.usage.completion_tokens
            stats.cost.cost_usd += result.cost_usd
    finally:
        if owns_client:
            await aclose_llm(llm)

    _apply_gray_verdicts(user_id, pairs, votes, evaluated, run_id=run_id, stats=stats)
    _log.info(
        "relation.resolve.gray.end",
        user_id=user_id,
        calls=calls,
        confirmed=stats.llm_confirmed,
        rejected=stats.llm_rejected,
        dejar=stats.llm_dejar,
        ungrounded=stats.ungrounded,
        errors=stats.errors,
    )
    if quota is not None:
        raise quota
