"""Capa OCR de memex, provider-agnóstica (visión por API OpenAI-compatible).

API pública: tipá tus callers contra el Protocol `OCRClient` y construí el concreto
(`OpenAIVisionClient`) en el borde. `OcrResult.cost_usd`/`.usage` mapean a
`memex.core.observability.record_llm_call` (`purpose="ocr"`).

Uso típico (async):

    from memex.ocr import OpenAIVisionClient, OcrConfig

    client = OpenAIVisionClient(OcrConfig.from_env())
    result = await client.ocr_image(image_bytes=blob, content_type="image/png")
    # result.text, result.cost_usd, result.usage, result.latency_ms

El worker `run_ocr` (etapa `memex-ocr`) vive en `memex.ocr.worker` y se importa directo.
"""

from memex.ocr.client import OCRClient, OcrError, OcrQuotaError, OcrResult
from memex.ocr.config import OcrConfig, OcrConfigError
from memex.ocr.openai_vision import OpenAIVisionClient
from memex.ocr.pricing import MODEL_PRICING, OcrPricing, compute_ocr_cost

__all__ = [
    "MODEL_PRICING",
    "OCRClient",
    "OcrConfig",
    "OcrConfigError",
    "OcrError",
    "OcrPricing",
    "OcrQuotaError",
    "OcrResult",
    "OpenAIVisionClient",
    "compute_ocr_cost",
]
