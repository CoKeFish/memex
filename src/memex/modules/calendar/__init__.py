"""Módulo `calendar` — extractor de fechas/eventos + dominio consolidado (ADR-015 §11, slice 2).

Ejercita `provide_domain` (single-writer de `mod_calendar_events`, handle en `.domain`) y el
dedup determinista FASE 1 (`.dedup`).
"""

from __future__ import annotations

from memex.modules.calendar.module import CalendarModule
from memex.modules.calendar.schema import CalendarEventItem

__all__ = ["CalendarEventItem", "CalendarModule"]
