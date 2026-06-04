"""Seam `provide_domain` de identidades (ADR-015 §4): handle tipado del directorio de identidades.

`identidades` es el hogar del átomo "agente" (Design Doc "Relaciones entre dominios"). Un módulo
dependiente NO lee las tablas con SQL crudo — recibe este handle vía `ctx.deps["identidades"]` y
resuelve referencias (nombre/email/handle) a la identidad canónica con un método tipado. Este slice
construye el seam de LECTURA (`resolve`, reusando la resolución determinista del módulo); no hay
`contribute` (identidades no recibe aportes de otros módulos). El orquestador todavía NO inyecta
esto (`ctx.deps={}`); el wiring es trabajo de un slice posterior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy.engine import Connection

from memex.modules.identidades.resolve import KnownIndex
from memex.modules.identidades.schema import IdentityItem


@dataclass(frozen=True)
class ResolvedIdentity:
    """Una referencia (nombre/email/handle) resuelta a una identidad canónica."""

    kind: str  # 'persona' | 'organizacion'
    id: int
    method: str  # cómo matcheó: 'email'|'domain'|'handle'|'exact_name'|'alias'|'sender_email'


@runtime_checkable
class IdentidadesDomain(Protocol):
    """Handle tipado del directorio de identidades (capacidad `provide_domain`)."""

    def resolve(
        self, *, name: str = "", email: str | None = None, handle: str | None = None
    ) -> ResolvedIdentity | None:
        """Resuelve una referencia a una persona/org conocida del user, o None si no matchea."""
        ...


class IdentidadesDomainReader:
    """Implementación del handle ligada a una conexión + user. Lectura pura sobre las tablas.

    El índice se arma una sola vez (perezoso) y se reusa entre llamadas del mismo handle."""

    def __init__(self, conn: Connection, user_id: int) -> None:
        self._conn = conn
        self._user_id = user_id
        self._index: KnownIndex | None = None

    def _ensure_index(self) -> KnownIndex:
        if self._index is None:
            # Import perezoso: el loader vive en `module` (más pesado) y no queremos acoplar el
            # import de `domain` con él.
            from memex.modules.identidades.module import load_known_index

            self._index = load_known_index(self._conn, self._user_id)
        return self._index

    def resolve(
        self, *, name: str = "", email: str | None = None, handle: str | None = None
    ) -> ResolvedIdentity | None:
        # Reusa la MISMA resolución determinista que la extracción, vía un `IdentityItem` mínimo.
        probe = IdentityItem(source_inbox_ids=(), name=name, email=email, handle=handle)
        res = self._ensure_index().resolve(probe)
        if res.kind is not None and res.identity_id is not None:
            return ResolvedIdentity(res.kind, res.identity_id, res.method)
        return None
