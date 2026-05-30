"""Render de payload del summarizer — re-export del substrato compartido.

`render_payload` se movió a `memex.processing.render` para compartirlo con los módulos de
extracción (ambos renderizan el contenido ORIGINAL idéntico; ADR-015 §9). Alias estable.
"""

from __future__ import annotations

from memex.processing.render import render_payload

__all__ = ["render_payload"]
