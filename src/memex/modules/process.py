"""Corrida COMBINADA: resumen + extracción en una sola invocación (ADR-015 §9).

El dueño quiere poder correr ambos a la vez O aislados; ambos leen los mismos mensajes
clasificados ORIGINALES. Acá se corren secuencialmente compartiendo un único cliente LLM. Cada
paso usa su propio cursor (`summary_inbox_links` / `module_extractions`) y es idempotente, así
que repetir es seguro. El resumen (`run_summaries`) es la misma pieza que usa la fase de
co-ocurrencia (`relations.summary`); el aislado `memex-extract` sigue disponible.

Optimización futura (seam): single-pass real (una sola carga + ventaneo, ambos pasos por
ventana). No cambia resultados; acá no se construye.
"""

from __future__ import annotations

from dataclasses import dataclass

from memex.llm import LLMClient, aclose_llm, build_llm_client
from memex.logging import get_logger
from memex.modules.orchestrator import _GROUP_SIZE_DEFAULT, ExtractStats, run_extraction
from memex.processing.windows import EXTRACT_WINDOW_SIZE
from memex.relations.summary import SummarizeStats, run_summaries
from memex.relevance.gate import GateStats, run_relevance_gate

_log = get_logger("memex.modules.process")


@dataclass
class CombinedStats:
    """Resultado de una corrida combinada."""

    summarize: SummarizeStats
    extract: ExtractStats
    #: Stats del gate de relevancia (corre ANTES de resumir; apagado → stats vacíos).
    gate: GateStats | None = None


async def run_combined(
    user_id: int,
    *,
    source_id: int | None = None,
    limit: int = 200,
    extract_window_size: int = EXTRACT_WINDOW_SIZE,
    route_chunk_size: int = 0,  # 0 = sin split (mirror del default de run_extraction)
    batching_policy: str = "grouped",  # mirror de run_extraction: una llamada para todos
    group_size: int = _GROUP_SIZE_DEFAULT,
    client: LLMClient | None = None,
    gate_client: LLMClient | None = None,
) -> CombinedStats:
    """Corre el gate de relevancia, luego resumen y extracción sobre los mismos mensajes.

    Cada fase fija su PROPIO tope de ventana (relevancia/resumen usan su default; ver
    `windows.py`); `extract_window_size` solo overridea la EXTRACCIÓN (la que más sufre ventanas
    grandes). Las de ruteo/batching (`route_chunk_size`, `batching_policy`, `group_size`) solo
    aplican a la extracción. El gate usa su PROPIO cliente LLM (Anthropic/Opus por default, NO el
    del resto —configurable por consumidor); `gate_client` es inyectable para tests. Gate apagado →
    no-op (worksets sin filtro).
    """
    owns_client = client is None
    llm: LLMClient = client or build_llm_client("process", user_id=user_id)
    _log.info("process.combined.start", user_id=user_id, source_id=source_id)
    try:
        gate = await run_relevance_gate(
            user_id,
            source_id=source_id,
            limit=limit,
            client=gate_client,
        )
        summarize = await run_summaries(
            user_id,
            source_id=source_id,
            limit=limit,
            client=llm,
        )
        extract = await run_extraction(
            user_id,
            source_id=source_id,
            limit=limit,
            max_window_size=extract_window_size,
            route_chunk_size=route_chunk_size,
            batching_policy=batching_policy,
            group_size=group_size,
            client=llm,
        )
    finally:
        if owns_client:
            await aclose_llm(llm)
    _log.info(
        "process.combined.end",
        user_id=user_id,
        gated=gate.messages,
        summaries=summarize.summaries,
        items=extract.items,
    )
    return CombinedStats(summarize=summarize, extract=extract, gate=gate)
