"""Segundo lazo de feedback: el rechazo MANUAL afina la lista de INTERESES (LLM-asistido).

Espejo de la minería de reglas, pero para los intereses. Las marcas manuales del dueño
(`relevance_marks`: TRUE = «esto SÍ me importa», FALSE = «esto es ruido») son la señal: un LLM mira
los correos marcados + los intereses actuales y PROPONE editar la lista (agregar un interés que los
rescates revelan, o quitar uno demasiado amplio que está dejando pasar ruido). El dueño acepta con
un botón o ajusta a mano — NUNCA se auto-aplica.

A diferencia de las reglas, NO hay dry run: los intereses son contexto difuso para el juez, no
matchers deterministas. La garantía es el humano-en-el-loop (status proposed → accepted/rejected).
Una llamada LLM por corrida (`purpose="relevance_interests"`); umbral-gated por
`interest_suggest_min_marks` (sin marcas suficientes → no-op SIN LLM).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, text

from memex.core.observability import CostBySource, record_llm_call
from memex.db import connection
from memex.llm import AnthropicClient, ChatMessage, LLMClient
from memex.logging import get_logger
from memex.relevance.interests import create_interest, list_interests, update_interest
from memex.relevance.providers import build_gate_client
from memex.relevance.settings import get_settings
from memex.relevance.verdicts import EMAIL_TYPES

_log = get_logger("memex.relevance.interest_mining")

_DEFAULT_LIMIT = 200
_MAX_TOKENS = 1024
_SNIPPET = 200

INTEREST_SYSTEM_PROMPT = (
    "Sos el afinador de INTERESES de un archivo personal. El dueño marcó a mano algunos correos "
    "como RELEVANTES (le importan) o NO RELEVANTES (ruido). Te paso sus INTERESES actuales y esos "
    "correos marcados. Proponé ajustes a la LISTA DE INTERESES:\n"
    "- `add`: un interés NUEVO (tema concreto) que explique por qué varios correos marcados "
    "RELEVANTES le importan y que sus intereses actuales NO capturan.\n"
    "- `remove`: un interés ACTUAL demasiado amplio que está dejando pasar ruido (correos marcados "
    "NO RELEVANTES que ese interés explicaría). El `text` debe COINCIDIR con un interés actual.\n"
    "Proponé SOLO con apoyo claro (varios correos), texto conciso. Respondé SOLO con un "
    'objeto JSON con esta forma exacta:\n{"suggestions": [{"action": "add" | "remove", "text": '
    '"<interés>", '
    '"rationale": "<por qué, max 200 chars>"}]}\nSi no hay patrón claro, devolvé '
    '{"suggestions": []}.'
)


@dataclass
class InterestMiningStats:
    """Resultado de una corrida del lazo de intereses."""

    marks: int = 0
    proposed: int = 0
    inserted: int = 0
    cost: CostBySource = field(default_factory=CostBySource)


def _aggregate_marks(conn: Connection, user_id: int, limit: int) -> list[dict[str, Any]]:
    """Marcas manuales recientes (solo correos) con remitente/asunto/snippet y la dirección."""
    rows = (
        conn.execute(
            text(
                f"""
                SELECT rm.is_relevant,
                       lower(i.payload->'from'->>'email') AS sender,
                       COALESCE(i.payload->>'subject', '') AS subject,
                       left(COALESCE(i.payload->>'body_text', ''), {_SNIPPET}) AS snippet
                FROM relevance_marks rm
                JOIN inbox i ON i.id = rm.inbox_id
                JOIN sources s ON s.id = i.source_id
                WHERE rm.user_id = :uid AND s.type = ANY(:email_types)
                ORDER BY rm.updated_at DESC
                LIMIT :limit
                """
            ),
            {"uid": user_id, "email_types": EMAIL_TYPES, "limit": limit},
        )
        .mappings()
        .all()
    )
    return [
        {
            "marca": "relevante" if r["is_relevant"] else "no_relevante",
            "remitente": r["sender"],
            "asunto": r["subject"],
            "extracto": r["snippet"],
        }
        for r in rows
    ]


def parse_interest_suggestions(content: str) -> list[dict[str, str]] | None:
    """Parsea {"suggestions": [{action, text, rationale}]}. None si el JSON no parsea.

    Descarta propuestas inválidas (acción fuera de add/remove o texto vacío) sin fallar.
    """
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    raw = data.get("suggestions") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return None
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        text_value = str(item.get("text", "")).strip()
        if action not in ("add", "remove") or not text_value:
            continue
        out.append(
            {
                "action": action,
                "text": text_value,
                "rationale": str(item.get("rationale", ""))[:200],
            }
        )
    return out


def _insert_suggestion(
    conn: Connection,
    user_id: int,
    *,
    action: str,
    text_value: str,
    rationale: str,
    model: str,
    interests: list[dict[str, Any]],
) -> bool:
    """Inserta una sugerencia (dedupe de pendientes vía índice parcial). True si insertó."""
    interest_id: int | None = None
    if action == "remove":
        match = next((i for i in interests if i["text"].lower() == text_value.lower()), None)
        interest_id = int(match["id"]) if match else None
    n = conn.execute(
        text(
            """
            INSERT INTO interest_suggestions (user_id, action, text, interest_id, rationale, model)
            VALUES (:uid, :action, :text, :iid, :rationale, :model)
            ON CONFLICT (user_id, action, lower(text)) WHERE status = 'proposed' DO NOTHING
            """
        ),
        {
            "uid": user_id,
            "action": action,
            "text": text_value,
            "iid": interest_id,
            "rationale": rationale,
            "model": model,
        },
    ).rowcount
    return bool(n)


async def run_interest_mining(
    user_id: int, *, min_marks: int | None = None, client: LLMClient | None = None
) -> InterestMiningStats:
    """Propone ediciones a los intereses a partir de las marcas manuales. Gate apagado → no-op.

    Umbral `interest_suggest_min_marks` (default del gate): sin suficientes marcas no llama al LLM.
    Persiste propuestas en `interest_suggestions` (proposed); NO auto-aplica.
    """
    stats = InterestMiningStats()
    with connection() as conn:
        settings = get_settings(conn, user_id)
        if not settings.enabled:
            _log.info("relevance.interest_mining.disabled", user_id=user_id)
            return stats
        threshold = min_marks if min_marks is not None else settings.interest_suggest_min_marks
        marks = _aggregate_marks(conn, user_id, _DEFAULT_LIMIT)
        interests = list_interests(conn, user_id)
    stats.marks = len(marks)
    if len(marks) < threshold:
        _log.info("relevance.interest_mining.below_threshold", user_id=user_id, marks=len(marks))
        return stats

    interest_texts = [i["text"] for i in interests]
    user_content = (
        f"Intereses actuales:\n{json.dumps(interest_texts, ensure_ascii=False)}\n\n"
        f"Correos marcados a mano:\n{json.dumps(marks, ensure_ascii=False)}"
    )
    messages = [
        ChatMessage("system", INTEREST_SYSTEM_PROMPT),
        ChatMessage("user", user_content),
    ]
    owns_client = client is None
    active: LLMClient = client if client is not None else build_gate_client(settings)
    try:
        result = await active.complete(
            messages, model=settings.model, response_format="json_object", max_tokens=_MAX_TOKENS
        )
    finally:
        if owns_client and isinstance(active, AnthropicClient):
            await active.aclose()

    suggestions = parse_interest_suggestions(result.content) if result.content.strip() else None
    record_llm_call(
        user_id=user_id,
        purpose="relevance_interests",
        model=result.model,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cache_hit_tokens=result.usage.cache_hit_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        status="ok" if suggestions is not None else "error",
        error_message=None if suggestions is not None else "unparseable interest suggestions",
        metadata={"marks": stats.marks, "suggestions": len(suggestions or [])},
        response_text=result.content or None,
    )
    stats.cost.record(
        None,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cost_usd=result.cost_usd,
    )
    if suggestions is None:
        _log.warning("relevance.interest_mining.unparseable", user_id=user_id)
        return stats
    stats.proposed = len(suggestions)
    with connection() as conn:
        for s in suggestions:
            if _insert_suggestion(
                conn,
                user_id,
                action=s["action"],
                text_value=s["text"],
                rationale=s["rationale"],
                model=result.model,
                interests=interests,
            ):
                stats.inserted += 1
    _log.info(
        "relevance.interest_mining.end",
        user_id=user_id,
        marks=stats.marks,
        proposed=stats.proposed,
        inserted=stats.inserted,
        **stats.cost.log_fields(),
    )
    return stats


_SUGGESTION_COLS = (
    "id, action, text, interest_id, rationale, status, model, created_at, resolved_at"
)


def list_suggestions(
    conn: Connection, *, user_id: int, status: str | None = "proposed", limit: int = 100
) -> list[dict[str, Any]]:
    """Sugerencias de interés del usuario (pendientes por default), más nuevas primero."""
    where = ["user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if status is not None:
        where.append("status = :st")
        params["st"] = status
    rows = (
        conn.execute(
            text(
                f"SELECT {_SUGGESTION_COLS} FROM interest_suggestions "
                f"WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC LIMIT :limit"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def resolve_suggestion(
    conn: Connection, *, user_id: int, suggestion_id: int, accept: bool
) -> dict[str, Any] | None:
    """Aplica (o descarta) una sugerencia pendiente. None si no existe / ya resuelta.

    `accept` + `add` → alta del interés (o re-habilita si estaba apagado); `accept` + `remove` →
    apaga el interés (reversible, no borra). Rechazar solo marca el estado.
    """
    row = (
        conn.execute(
            text(
                "SELECT action, text, interest_id FROM interest_suggestions "
                "WHERE id = :id AND user_id = :uid AND status = 'proposed'"
            ),
            {"id": suggestion_id, "uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    if accept and row["action"] == "add":
        existing = (
            conn.execute(
                text(
                    "SELECT id, enabled FROM personal_interests "
                    "WHERE user_id = :uid AND lower(text) = lower(:t)"
                ),
                {"uid": user_id, "t": row["text"]},
            )
            .mappings()
            .first()
        )
        if existing is None:
            create_interest(conn, user_id, row["text"])
        elif not existing["enabled"]:
            update_interest(conn, int(existing["id"]), user_id, enabled=True)
    elif accept and row["action"] == "remove":
        iid = row["interest_id"]
        if iid is None:
            iid = conn.execute(
                text(
                    "SELECT id FROM personal_interests "
                    "WHERE user_id = :uid AND lower(text) = lower(:t)"
                ),
                {"uid": user_id, "t": row["text"]},
            ).scalar()
        if iid is not None:
            update_interest(conn, int(iid), user_id, enabled=False)
    updated = (
        conn.execute(
            text(
                f"UPDATE interest_suggestions SET status = :s, resolved_at = NOW() "
                f"WHERE id = :id AND user_id = :uid RETURNING {_SUGGESTION_COLS}"
            ),
            {"s": "accepted" if accept else "rejected", "id": suggestion_id, "uid": user_id},
        )
        .mappings()
        .first()
    )
    return dict(updated) if updated else None
