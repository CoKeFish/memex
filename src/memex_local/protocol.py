"""Contrato que cada plugin del cliente local debe cumplir.

Un plugin es un módulo Python que el daemon descubre por filesystem y carga
dinámicamente. Su superficie pública es deliberadamente pequeña — solo lo
que el daemon necesita para conocer, validar y ejecutar la fuente.

El plugin construye un `Source` (Protocol de `memex.core.source`) que luego
el runner ya existente sabe drenar. Por eso un plugin típico es muy fino:
toda la lógica de fetch/parse/checkpoint vive en el `Source` reusado del
módulo correspondiente (ej. `memex.ingestors.imap.ImapSource`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

from memex.core.source import Source


@dataclass(frozen=True)
class Problem:
    """Un requisito incumplido detectado por `validate_requirements`."""

    severity: str  # "error" | "warning"
    code: str
    message: str


@runtime_checkable
class LocalPlugin(Protocol):
    """Lo que un plugin debe exponer para ser cargado por el daemon.

    El plugin se identifica por `name` — slug único, validado contra el del
    directorio donde vive. Un plugin que no cumpla este contrato es rechazado
    por el discovery con un mensaje claro.
    """

    name: ClassVar[str]
    version: ClassVar[str]
    source_type: ClassVar[str]
    default_schedule: ClassVar[str]

    def build_source(self, local_config: Mapping[str, Any]) -> Source:
        """Construye un `Source` listo para ser drenado por el runner.

        `local_config` es el dict deserializado del TOML del plugin
        (ej. `~/.memex-local/plugins/<nombre>/config.toml`).
        """
        ...

    def validate_requirements(self, local_config: Mapping[str, Any]) -> list[Problem]:
        """Chequea que el entorno satisfaga los requisitos del plugin.

        Útil para que `plugin doctor <nombre>` reporte env vars faltantes,
        archivos no encontrados, etc., antes de intentar levantar el daemon.
        Devuelve una lista vacía si todo está OK.
        """
        ...
