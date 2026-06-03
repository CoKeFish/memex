"""Módulo `hackathones` — extractor puro de hackatones (ADR-015 §11, tercer módulo)."""

from __future__ import annotations

from memex.modules.hackathones.module import HackathonModule
from memex.modules.hackathones.schema import HackathonItem

__all__ = ["HackathonItem", "HackathonModule"]
