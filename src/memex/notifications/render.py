"""Redacción de avisos: plantilla (la trae el borrador) o LLM a partir de los hechos.

`LLMWriter.write(draft)` le pide a un modelo (consumidor `notifications` del registro de
proveedores; DeepSeek por default) que redacte el cuerpo a partir de `draft.facts` siguiendo
`draft.llm_brief`. Reglas: español, sin inventar, corto. Si el modelo falla o devuelve vacío, el
servicio se queda con la plantilla (`None`). Cada llamada queda en `llm_calls` con propósito
`notifications_render` (costo visible en /metricas).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from memex.core.observability import record_llm_call
from memex.llm.client import ChatMessage, LLMClient, LLMError
from memex.llm.registry import aclose_llm
from memex.logging import get_logger
from memex.notifications.producers import Draft

_log = get_logger("memex.notifications.render")

PURPOSE = "notifications_render"
_MAX_TOKENS = 600

_SYSTEM = (
    "Eres el redactor de avisos de un asistente personal. Escribes en español, directo y breve, "
    "sin saludos ni despedidas, sin emojis. Solo usas los hechos que te dan: no inventas datos, "
    "fechas, montos ni nombres; si un dato no está, no lo mencionas. Horas y fechas tal como "
    "vienen. Devuelves SOLO el texto del aviso, sin título ni encabezados de markdown."
)


def _default(o: Any) -> Any:
    if isinstance(o, datetime | Decimal):
        return str(o)
    return str(o)


class Writer(Protocol):
    """Quien redacta un borrador (el `LLMWriter` real o un doble en tests)."""

    async def write(self, draft: Draft) -> str | None: ...

    async def aclose(self) -> None: ...


class LLMWriter:
    def __init__(self, client: LLMClient, *, user_id: int, model: str | None = None) -> None:
        self._client = client
        self._user_id = user_id
        self._model = model

    async def aclose(self) -> None:
        await aclose_llm(self._client)

    async def write(self, draft: Draft) -> str | None:
        if not draft.llm_brief:
            return None
        messages = [
            ChatMessage(role="system", content=_SYSTEM),
            ChatMessage(
                role="user",
                content=(
                    f"{draft.llm_brief}\n\nHECHOS (JSON):\n"
                    + json.dumps(draft.facts, ensure_ascii=False, default=_default, indent=1)
                ),
            ),
        ]
        started = datetime.now()
        try:
            result = await self._client.complete(
                messages, model=self._model, temperature=0.3, max_tokens=_MAX_TOKENS
            )
        except LLMError as e:
            latency = int((datetime.now() - started) / timedelta(milliseconds=1))
            record_llm_call(
                user_id=self._user_id,
                purpose=PURPOSE,
                model=self._model or "?",
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=Decimal("0"),
                latency_ms=latency,
                status="error",
                error_message=str(e)[:500],
                metadata={"dedup_key": draft.dedup_key, "kind": draft.kind},
            )
            _log.warning("notifications.render.llm_failed", kind=draft.kind, error=str(e)[:200])
            return None
        record_llm_call(
            user_id=self._user_id,
            purpose=PURPOSE,
            model=result.model,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            status="ok",
            cache_hit_tokens=result.usage.cache_hit_tokens,
            metadata={"dedup_key": draft.dedup_key, "kind": draft.kind},
            response_text=result.content[:4000],
        )
        text = result.content.strip()
        return text or None


def format_message(*, title: str, body: str, deep_link: str | None, dashboard_url: str) -> str:
    """Texto final para un canal de texto plano: título, cuerpo y, si hay base pública del
    dashboard, el enlace."""
    parts = [title.strip()]
    if body.strip():
        parts.append(body.strip())
    if deep_link and dashboard_url:
        parts.append(dashboard_url.rstrip("/") + "/" + deep_link.lstrip("/"))
    return "\n".join(parts)
