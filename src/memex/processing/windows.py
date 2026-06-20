"""Agrupado del work-set en ventanas.

Función pura, sin DB ni LLM. `batch` se agrupa por `source_id` y se parte SOLO por tope de
cantidad; `individual` es 1 ventana por mensaje. El agrupado NO mira los timestamps de los
mensajes — la cadencia temporal (cada cuánto se procesa lo pendiente) es del daemon/scheduler,
no del ventaneo. El tope es la perilla de costo/granularidad.

Usado por relevancia, resumen y extracción sobre los mismos mensajes clasificados, pero cada
fase fija su PROPIO tope (no es una sola perilla compartida): ver `GATE_WINDOW_SIZE` /
`SUMMARY_WINDOW_SIZE` / `EXTRACT_WINDOW_SIZE` abajo.

`WorkRow.source_type` (el `sources.type`: imap/telegram/...) es opcional: relations/summary.py no lo
usa (default `""`); los módulos lo pueblan para derivar el `SourceKind` y pre-filtrar por
`consumes_kinds` sin tocar el LLM.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: Topes de mensajes por ventana batch, POR FASE — el dueño los quiere DISTINTOS (no una sola
#: perilla): la EXTRACCIÓN necesita ventanas chicas (con 40, el budget de output del LLM trunca y
#: PIERDE ítems de los correos del final); la RELEVANCIA tolera ventanas grandes (juicio
#: por-mensaje, barato). El resumen va con la extracción (alimenta co-ocurrencia por unidad).
GATE_WINDOW_SIZE = 40
EXTRACT_WINDOW_SIZE = 10
SUMMARY_WINDOW_SIZE = 20
#: Default genérico de `plan_windows` cuando un caller no fija fase (los reales pasan su constante).
MAX_WINDOW_SIZE = EXTRACT_WINDOW_SIZE


@dataclass(frozen=True)
class WorkRow:
    """Un mensaje clasificado pendiente de procesar (resumir y/o extraer)."""

    inbox_id: int
    source_id: int
    occurred_at: datetime
    payload: dict[str, Any]
    tier: str
    source_type: str = ""
    #: Texto OCR de las imágenes del mensaje (concatenado, solo `media_assets` en `ok`). Lo
    #: pueblan los loaders del work-set vía JOIN; el render lo inyecta junto al body. Default
    #: "" → mensajes sin imágenes (o con OCR pendiente) renderizan igual que antes.
    ocr_text: str = ""


@dataclass(frozen=True)
class Window:
    """Un conjunto de mensajes que se procesan juntos en una llamada."""

    tier: str
    source_id: int
    rows: tuple[WorkRow, ...]


def plan_windows(
    rows: Sequence[WorkRow],
    *,
    max_window_size: int = MAX_WINDOW_SIZE,
) -> list[Window]:
    """Agrupa el work-set en ventanas. `individual` → 1 por mensaje; todo lo demás → batch por
    source + tope de cantidad (`max_window_size`).

    Batch = «todo lo que NO es individual», no solo el tier `batch`: con el gate de relevancia
    encendido, un correo `blacklist` (bulk) puede llegar al juez o, si se rescató, a la
    extracción — se procesa agrupado (barato), igual que un batch. `WorkRow.tier` conserva el
    valor crudo (la señal de bulk no se pierde); solo el AGRUPADO lo trata como batch.

    El agrupado NO mira los timestamps: empaca consecutivos del mismo `source_id` hasta el tope.
    La cadencia temporal (cada cuánto corre el procesamiento) la decide el daemon/scheduler, no
    esto. `max_window_size` es la perilla de costo/granularidad (default `MAX_WINDOW_SIZE`)."""
    windows: list[Window] = [
        Window("individual", r.source_id, (r,)) for r in rows if r.tier == "individual"
    ]

    batch = sorted(
        (r for r in rows if r.tier != "individual"),
        key=lambda r: (r.source_id, r.occurred_at),
    )
    current: list[WorkRow] = []

    def flush() -> None:
        if current:
            windows.append(Window("batch", current[0].source_id, tuple(current)))

    for row in batch:
        if current:
            prev = current[-1]
            if row.source_id != prev.source_id or len(current) >= max_window_size:
                flush()
                current = []
        current.append(row)
    flush()

    return windows
