"""Agrupado del work-set en ventanas (ADR-003: ventanas conversacionales).

Función pura, sin DB ni LLM. `batch` se agrupa por `source_id` y se parte por gap temporal
o tope de cantidad; `individual` es 1 ventana por mensaje. Los umbrales son la perilla de
costo/granularidad.

Compartido por la fase de resumen (`memex.relations.summary`) y los módulos de extracción
(`memex.modules`): ambos ventanean idéntico porque operan sobre los mismos mensajes
clasificados originales (etapa combinada, ADR-015 §9).

`WorkRow.source_type` (el `sources.type`: imap/telegram/...) es opcional: relations/summary.py no lo
usa (default `""`); los módulos lo pueblan para derivar el `SourceKind` y pre-filtrar por
`consumes_kinds` sin tocar el LLM.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: Un hueco mayor a esto entre mensajes consecutivos abre una ventana nueva (6 h).
MAX_GAP_SECONDS = 6 * 3600
#: Tope de mensajes por ventana batch (evita prompts gigantes / degradación).
MAX_WINDOW_SIZE = 40


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
    max_gap_seconds: int = MAX_GAP_SECONDS,
) -> list[Window]:
    """Agrupa el work-set en ventanas. `individual` → 1 por mensaje; todo lo demás → batch por
    source + gap (>`max_gap_seconds`) + tope (`max_window_size`).

    Batch = «todo lo que NO es individual», no solo el tier `batch`: con el gate de relevancia
    encendido, un correo `blacklist` (bulk) puede llegar al juez o, si se rescató, a la
    extracción — se procesa agrupado (barato), igual que un batch. `WorkRow.tier` conserva el
    valor crudo (la señal de bulk no se pierde); solo el AGRUPADO lo trata como batch.

    `max_window_size`/`max_gap_seconds` son las perillas de costo/granularidad (defaults =
    `MAX_WINDOW_SIZE`/`MAX_GAP_SECONDS`); se exponen como flags de CLI para experimentar. El tope
    de tamaño es un MÁXIMO: el gap puede partir una ventana antes (ventanas conversacionales)."""
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
            gap = (row.occurred_at - prev.occurred_at).total_seconds()
            if (
                row.source_id != prev.source_id
                or gap > max_gap_seconds
                or len(current) >= max_window_size
            ):
                flush()
                current = []
        current.append(row)
    flush()

    return windows
