"""Juez LLM de relevancia para la ZONA GRIS (opcional, default-off, último recurso).

Cuando las señales deterministas no alcanzan o se contradicen, un LLM barato lee una MUESTRA de los
mensajes de un candidato y emite un veredicto ADVISORY de relevancia: NO acciona nada (la decisión
sigue siendo del humano), solo informa la cola (`relevance_candidates.llm_verdict`). Gateado por
`MEMEX_QUALITY_LLM` (default False) y on-demand (cuesta). Cliente LLM inyectable (tests sin red).
Regla de oro: si el determinismo decide, no se llama al LLM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from memex.core.observability import record_llm_call
from memex.db import connection
from memex.llm import ChatMessage, LLMClient, aclose_llm, build_llm_client
from memex.logging import get_logger
from memex.processing.render import render_payload

_log = get_logger("memex.quality.judge_llm")

_MAX_TOKENS = 200
_SAMPLE_CHARS = 1500  # tope por mensaje de muestra (acota el costo)

_SYSTEM_PROMPT = (
    "Sos un filtro de relevancia para una memoria personal. Te paso una MUESTRA de mensajes de un "
    "MISMO remitente. Decidí si vale la pena PROCESAR (extraer datos, resumir) los mensajes "
    "de este remitente o si son ruido (promociones, notificaciones automáticas, newsletters). "
    'Respondé SOLO JSON: {"is_relevant": bool, "confidence": 0..1, "reason": "breve"}.'
)


class JudgeUnavailableError(RuntimeError):
    """El juez LLM está apagado (`MEMEX_QUALITY_LLM=false`)."""


@dataclass(frozen=True)
class Verdict:
    """Veredicto ADVISORY del LLM sobre un remitente."""

    is_relevant: bool
    confidence: float
    reason: str


def _sample_text(conn: Any, user_id: int, inbox_ids: list[int]) -> str:
    """Render concatenado de los mensajes de muestra (lo que ve el LLM)."""
    if not inbox_ids:
        return ""
    rows = conn.execute(
        text("SELECT payload FROM inbox WHERE user_id = :uid AND id = ANY(:ids) ORDER BY id"),
        {"uid": user_id, "ids": inbox_ids},
    ).all()
    parts: list[str] = []
    for r in rows:
        payload = r[0] if isinstance(r[0], dict) else {}
        parts.append(render_payload(payload, "")[:_SAMPLE_CHARS])
    return "\n\n---\n\n".join(p for p in parts if p.strip())


def _parse(content: str) -> Verdict | None:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "is_relevant" not in data:
        return None
    try:
        return Verdict(
            is_relevant=bool(data["is_relevant"]),
            confidence=float(data.get("confidence", 0.0)),
            reason=str(data.get("reason", "")),
        )
    except (TypeError, ValueError):
        return None


async def judge_sender(
    user_id: int, sender_key: str, *, client: LLMClient | None = None
) -> Verdict | None:
    """Juzga un candidato con el LLM y persiste el veredicto. None si no existe o no hay muestra.

    Lanza `JudgeUnavailableError` si el juez está apagado. ADVISORY: NO acciona ni cambia el estado
    del candidato — solo guarda `llm_verdict` para informar la decisión del humano.
    """
    from memex.config import settings

    if not settings.quality_llm:
        raise JudgeUnavailableError("MEMEX_QUALITY_LLM está apagado")

    with connection() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT snapshot FROM relevance_candidates "
                    "WHERE user_id = :uid AND sender_key = :k"
                ),
                {"uid": user_id, "k": sender_key},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        snapshot = row["snapshot"] or {}
        sample_ids = [int(i) for i in snapshot.get("sample_inbox_ids", [])]
        sample = _sample_text(conn, user_id, sample_ids)
    if not sample.strip():
        return None

    owns = client is None
    llm: LLMClient = client or build_llm_client("quality_judge", user_id=user_id)
    try:
        result = await llm.complete(
            [ChatMessage("system", _SYSTEM_PROMPT), ChatMessage("user", sample)],
            response_format="json_object",
            temperature=0.0,
            max_tokens=_MAX_TOKENS,
        )
    finally:
        if owns:
            await aclose_llm(llm)

    verdict = _parse(result.content)
    record_llm_call(
        user_id=user_id,
        purpose="relevance_judge",
        model=result.model,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cache_hit_tokens=result.usage.cache_hit_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        status="ok" if verdict is not None else "error",
        metadata={"sender_key": sender_key},
        response_text=result.content,
    )
    if verdict is None:
        _log.warning("quality.judge.parse_failed", user_id=user_id, sender_key=sender_key)
        return None

    payload = {
        "is_relevant": verdict.is_relevant,
        "confidence": verdict.confidence,
        "reason": verdict.reason,
        "model": result.model,
    }
    with connection() as conn:
        conn.execute(
            text(
                "UPDATE relevance_candidates SET llm_verdict = CAST(:v AS JSONB), "
                "updated_at = NOW() WHERE user_id = :uid AND sender_key = :k"
            ),
            {"v": json.dumps(payload), "uid": user_id, "k": sender_key},
        )
    _log.info("quality.judge", user_id=user_id, relevant=verdict.is_relevant)
    return verdict
