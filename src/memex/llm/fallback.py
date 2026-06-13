"""FallbackClient — cadena de proveedores detrás del Protocol `LLMClient`.

Envuelve una lista ORDENADA de `(nombre, cliente)`. `complete()` intenta en orden; si un intento
falla con un error que justifica cambiar de proveedor (cuota agotada, red/5xx tras agotar los
retries del propio cliente, o timeout), pasa al siguiente. Un 4xx no-cuota (request/config
inválido) NO salta —falla igual en todo proveedor— y se re-levanta inmediato. El output
inparseable NUNCA llega acá: lo maneja el worker DESPUÉS de un `complete()` exitoso (es
reintentable en el mismo proveedor, no un motivo para cambiar de vendor).

Los callers NO se enteran: tipan contra `LLMClient` y reciben el `LLMResult` del proveedor que
sirvió (su `model`/`cost`/`usage` reales → el worker lo registra con `record_llm_call`, así el
costo por proveedor sigue correcto). Auditoría sin tocar la DB (mismo aislamiento que los
clientes concretos): cada intento fallido emite `llm.fallback.attempt_failed` y el éxito tras ≥1
fallo emite `llm.fallback.served`; todo a structlog → /logs.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from memex.llm.client import (
    ChatMessage,
    LLMClient,
    LLMError,
    LLMQuotaError,
    LLMResult,
    ResponseFormat,
)
from memex.logging import get_logger


def _should_fallback(e: LLMError) -> bool:
    """¿El error justifica probar el siguiente proveedor?

    Sí: cuota agotada (`LLMQuotaError`), red/timeout (los clientes lo levantan con `status_code=0`
    tras agotar sus retries) y 5xx/429/529. No: 4xx no-cuota (request/config inválido — el mismo
    request falla en cualquier proveedor).
    """
    if isinstance(e, LLMQuotaError):
        return True
    sc = e.status_code
    return sc == 0 or sc in (429, 529) or 500 <= sc < 600


class FallbackClient:
    """Cliente que satisface el Protocol `LLMClient` probando una cadena de proveedores."""

    def __init__(self, clients: Sequence[tuple[str, LLMClient]]) -> None:
        if not clients:
            raise ValueError("FallbackClient necesita al menos un cliente")
        self._clients = list(clients)
        self._log = get_logger("memex.llm.fallback")

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        failures: list[str] = []
        last_exc: LLMError | None = None
        for provider, client in self._clients:
            started = time.monotonic()
            try:
                result = await client.complete(
                    messages,
                    model=model,
                    response_format=response_format,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except LLMError as e:
                if not _should_fallback(e):
                    raise
                latency_ms = int((time.monotonic() - started) * 1000)
                self._log.warning(
                    "llm.fallback.attempt_failed",
                    provider=provider,
                    error_class=type(e).__name__,
                    status_code=e.status_code,
                    error=str(e)[:200],
                    latency_ms=latency_ms,
                    attempt=len(failures) + 1,
                )
                failures.append(provider)
                last_exc = e
                continue
            if failures:
                self._log.info(
                    "llm.fallback.served",
                    provider=provider,
                    model=result.model,
                    prior_failures=failures,
                    attempts=len(failures) + 1,
                )
            return result

        # Cadena agotada: re-levanta el último error (todos dispararon fallback).
        self._log.error(
            "llm.fallback.exhausted",
            providers=[p for p, _ in self._clients],
            failures=failures,
        )
        assert last_exc is not None  # la cadena no es vacía → hubo al menos un intento
        raise last_exc

    async def aclose(self) -> None:
        """Cierra cada cliente envuelto que exponga `aclose` (Codex no tiene → no-op)."""
        for _, client in self._clients:
            closer = getattr(client, "aclose", None)
            if closer is not None:
                await closer()
