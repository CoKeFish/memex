"""Contrato provider-agnóstico de la capa OCR.

Define el Protocol `OCRClient` (la abstracción contra la que tipan los callers) y los tipos
que viajan por él (`OcrResult`, `OcrError`). Un proveedor concreto (`OpenAIVisionClient`)
implementa este Protocol; los callers (el worker `memex-ocr`) NUNCA tipan contra la clase
concreta, igual que relations/summary.py tipa contra `LLMClient`.

A diferencia de `LLMClient.complete` (texto → texto), acá la entrada es UNA imagen (bytes +
content-type) y la salida es su transcripción. `OcrResult` reusa `LLMUsage` y mapea 1:1 a
`memex.core.observability.record_llm_call` (`purpose="ocr"`), igual que `LLMResult`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from memex.llm.client import LLMUsage


class OcrError(Exception):
    """Base de todos los errores de la capa OCR — los callers la atrapan genérica.

    `status_code` es el HTTP status cuando aplica, o 0 para errores lógicos / de
    configuración. Mismo shape que `LLMError`.
    """

    def __init__(self, status_code: int, message: str, body: str | None = None) -> None:
        super().__init__(f"ocr error {status_code}: {message}")
        self.status_code = status_code
        self.body = body


class OcrQuotaError(OcrError):
    """Saldo/cuota del proveedor de visión agotada (HTTP 402) — NO reintentable.

    Espeja `LLMQuotaError`: el cliente la levanta ante un 402 y `run_ocr` la DEJA propagar para
    ABORTAR la corrida (no la trata como un asset best-effort, así no consume intentos en vano).
    """


@dataclass(frozen=True)
class OcrResult:
    """Resultado de OCR-ear una imagen: texto transcripto + usage + costo + latencia."""

    text: str
    model: str
    usage: LLMUsage
    cost_usd: Decimal
    latency_ms: int
    finish_reason: str | None = None


@runtime_checkable
class OCRClient(Protocol):
    """Interfaz de OCR agnóstica del proveedor.

    Una implementación concreta aísla a su vendor (HTTP, auth, shapes) detrás de este único
    método. `model=None` usa el default de la config; pasarlo explícito permite cambiar de
    modelo por llamada (override `--model` del CLI).
    """

    async def ocr_image(
        self,
        *,
        image_bytes: bytes,
        content_type: str,
        model: str | None = None,
    ) -> OcrResult: ...
