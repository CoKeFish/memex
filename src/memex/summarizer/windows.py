"""Windowing del summarizer — re-export del substrato compartido `memex.processing.windows`.

El windowing se movió a `memex.processing.windows` para compartirlo con los módulos de
extracción (ambos ventanean idéntico sobre los mismos mensajes clasificados; ADR-015 §9).
Este módulo se mantiene como alias estable para los imports existentes del summarizer.
"""

from __future__ import annotations

from memex.processing.windows import (
    MAX_GAP_SECONDS,
    MAX_WINDOW_SIZE,
    Window,
    WorkRow,
    plan_windows,
)

__all__ = [
    "MAX_GAP_SECONDS",
    "MAX_WINDOW_SIZE",
    "Window",
    "WorkRow",
    "plan_windows",
]
