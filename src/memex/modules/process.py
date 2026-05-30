"""Corrida COMBINADA: resumen + extracción en una sola invocación (ADR-015 §9).

El dueño quiere poder correr ambos a la vez O aislados; ambos leen los mismos mensajes
clasificados ORIGINALES. Acá se corren secuencialmente compartiendo un único cliente LLM. Cada
paso usa su propio cursor (`summary_inbox_links` / `module_extractions`) y es idempotente, así
que repetir es seguro y los aislados (`memex-summarize` / `memex-extract`) siguen disponibles.

Optimización futura (seam): single-pass real (una sola carga + ventaneo, ambos pasos por
ventana). No cambia resultados; acá no se construye.
"""

from __future__ import annotations

from dataclasses import dataclass

from memex.llm import DeepSeekClient, LLMClient, LLMConfig
from memex.logging import get_logger
from memex.modules.orchestrator import ExtractStats, run_extraction
from memex.summarizer.worker import SummarizeStats, run_summarization

_log = get_logger("memex.modules.process")


@dataclass
class CombinedStats:
    """Resultado de una corrida combinada."""

    summarize: SummarizeStats
    extract: ExtractStats


async def run_combined(
    user_id: int,
    *,
    source_id: int | None = None,
    limit: int = 200,
    client: LLMClient | None = None,
) -> CombinedStats:
    """Corre resumen y luego extracción sobre los mismos mensajes, compartiendo cliente LLM."""
    owns_client = client is None
    llm: LLMClient = client if client is not None else DeepSeekClient(LLMConfig.from_env())
    _log.info("process.combined.start", user_id=user_id, source_id=source_id)
    try:
        summarize = await run_summarization(user_id, source_id=source_id, limit=limit, client=llm)
        extract = await run_extraction(user_id, source_id=source_id, limit=limit, client=llm)
    finally:
        if owns_client and isinstance(llm, DeepSeekClient):
            await llm.aclose()
    _log.info(
        "process.combined.end",
        user_id=user_id,
        summaries=summarize.summaries,
        items=extract.items,
    )
    return CombinedStats(summarize=summarize, extract=extract)
