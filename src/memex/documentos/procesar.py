"""Leer y extraer un documento recién entregado, y dejar finanzas al día.

Las tres vías de entrega (bot, dashboard, consola) terminaban en el mismo reproceso
`ocr → classify → extract` y ahí paraban: los movimientos del extracto quedaban en CRUDO hasta que
alguien corriera el ciclo de finanzas (`memex-finance consolidate` o el job `finance`, que en dev
está apagado). Subías un extracto y /finanzas no cambiaba. Acá el reproceso va seguido de la
CONSOLIDACIÓN (determinista, idempotente, ~1 s), así lo extraído se ve en el acto.

Lo que sí queda para después es el JUEZ (FASE 2, LLM): los movimientos con un par candidato sin
resolver siguen `pending` hasta `memex-finance dedup` o el job `finance`. Eso no se corre acá a
propósito: son llamadas LLM por par y no deben colgar una subida ni una respuesta del bot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from memex.logging import get_logger

_log = get_logger("memex.documentos.procesar")

#: Etapas del reproceso para un documento entregado (sin gate de relevancia: lo entregó el usuario).
STAGES: tuple[str, ...] = ("ocr", "classify", "extract")


async def procesar_documentos(user_id: int, inbox_ids: Sequence[int]) -> dict[str, Any]:
    """Reprocesa `inbox_ids` (etapas `STAGES`) y consolida finanzas del user. Devuelve el resultado
    por etapa del reproceso (lo que muestran el bot, la CLI y el dashboard). La consolidación es
    best-effort: si falla, lo extraído ya está guardado y el ciclo siguiente la reconstruye."""
    from memex.modules.finance.consolidate import run_consolidation
    from memex.reprocess import reprocess

    result = await reprocess(user_id, stages=list(STAGES), targets=list(inbox_ids), force=True)
    try:
        stats = await asyncio.to_thread(run_consolidation, user_id)
    except Exception as e:  # best-effort: no tumbar la subida por la proyección
        _log.warning(
            "documentos.consolidacion_fallo",
            user_id=user_id,
            inbox_ids=list(inbox_ids),
            exc_type=type(e).__name__,
            exc_msg=str(e),
        )
    else:
        _log.info(
            "documentos.consolidado",
            user_id=user_id,
            inbox_ids=list(inbox_ids),
            groups=stats.groups,
            consolidated=stats.consolidated,
            merges=stats.merges,
        )
    return result
