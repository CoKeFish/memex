"""Módulo `identidades` (ADR-015, slice 1: directorio + extracción).

Hogar de personas y organizaciones (el átomo "agente" del Design Doc "Relaciones entre dominios").
Tres altitudes del dato: menciones (crudo por-mensaje) → entidades canónicas (persona/org) →
(futuro) aristas. Fuentes del set canónico: sync de Google Contacts + lista manual de interés.

"""

from __future__ import annotations

from memex.modules.identidades.module import IdentidadesModule
from memex.modules.identidades.schema import IdentityItem

__all__ = ["IdentidadesModule", "IdentityItem"]
