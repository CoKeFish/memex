"""Resolución DETERMINISTA de una mención a una identidad canónica (persona u organización).

Señales FUERTES de la MENCIÓN MISMA, en orden de prioridad (sin LLM; el difuso y el desempate LLM
viven en `fuzzy.py` / `dedup_llm.py`). El remitente del mensaje NO se usa como señal: que el correo
venga de una identidad conocida no implica que las otras entidades mencionadas sean ese remitente.

  1. email exacto del item                → identidad
  2. dominio del email                     → org
  3. handle exacto ACOTADO POR PLATAFORMA  → identidad   (el handle de X ≠ Instagram ≠ ...)
  4. nombre normalizado exacto             → identidad
  5. alias normalizado                     → identidad
  6. nada matchea                          → unresolved

`KnownIndex` es puro (se arma desde una lista de `KnownIdentity` en memoria) → testeable sin DB. El
módulo lo alimenta con lo que lee de `mod_identidades` + `mod_identidades_identifiers`. La
normalización de nombre/alias usa `normalize_match` (espejo de `memex_norm`); los identificadores
ya vienen normalizados (`value_norm`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from memex.modules.identidades.normalize import is_role_email, norm_identifier, normalize_match
from memex.modules.identidades.schema import IdentityItem

#: Discriminador de identidad (espejo del CHECK `kind` en mod_identidades).
KIND_PERSONA = "persona"
KIND_ORG = "organizacion"


@dataclass(frozen=True)
class KnownIdentifier:
    """Un identificador por-fuente de una identidad, reducido a las llaves de match."""

    platform: str
    kind: str  # 'email'|'phone'|'handle'|'domain'|'url'
    value_norm: str


@dataclass(frozen=True)
class KnownIdentity:
    """Una identidad conocida, reducida a las llaves de match (nombre, alias, identificadores)."""

    id: int
    kind: str  # 'persona' | 'organizacion'
    display_name: str
    aliases: Sequence[str] = ()
    identifiers: Sequence[KnownIdentifier] = field(default_factory=tuple)


@dataclass(frozen=True)
class Resolution:
    """Resultado de atar una mención a una identidad canónica (o no)."""

    kind: str | None  # 'persona' | 'organizacion' | None
    identity_id: int | None
    #: email/domain/handle/exact_name/alias/sender_email/fuzzy/llm/created/unresolved
    method: str

    @classmethod
    def unresolved(cls) -> Resolution:
        return cls(kind=None, identity_id=None, method="unresolved")


class KnownIndex:
    """Índices en memoria para resolución determinista O(1). El primer match gana (`setdefault`),
    así el orden de inserción es estable. Los handles se indexan ACOTADOS por plataforma (y también
    sin plataforma, para resolver solo cuando el valor es único entre plataformas)."""

    def __init__(self, identities: Sequence[KnownIdentity] = ()) -> None:
        self._kind: dict[int, str] = {}
        self._email: dict[str, int] = {}
        self._domain: dict[str, int] = {}
        self._handle_by_platform: dict[tuple[str, str], int] = {}
        self._handle_any: dict[str, set[int]] = {}
        self._name: dict[str, int] = {}
        self._alias: dict[str, int] = {}
        for ident in identities:
            self.add(ident)

    def add(self, ident: KnownIdentity) -> None:
        """Registra una identidad (primer match gana). Permite que el dedup vea identidades creadas
        dentro de la MISMA corrida de extracción."""
        self._kind.setdefault(ident.id, ident.kind)
        name_key = normalize_match(ident.display_name)
        if name_key:
            self._name.setdefault(name_key, ident.id)
        for a in ident.aliases:
            ak = normalize_match(a)
            if ak:
                self._alias.setdefault(ak, ident.id)
        for idf in ident.identifiers:
            if not idf.value_norm:
                continue
            if idf.kind == "email":
                self._email.setdefault(idf.value_norm, ident.id)
            elif idf.kind == "domain":
                self._domain.setdefault(idf.value_norm, ident.id)
            elif idf.kind == "handle":
                self._handle_by_platform.setdefault((idf.platform, idf.value_norm), ident.id)
                self._handle_any.setdefault(idf.value_norm, set()).add(ident.id)

    def add_alias(self, alias: str, identity_id: int) -> None:
        """Registra un alias nuevo (p. ej. tras un auto-merge que suma el nombre variante)."""
        ak = normalize_match(alias)
        if ak:
            self._alias.setdefault(ak, identity_id)

    def _res(self, identity_id: int, method: str) -> Resolution:
        return Resolution(self._kind.get(identity_id), identity_id, method)

    def _by_email(self, raw: str, method: str) -> Resolution | None:
        """email exacto → identidad; si no, dominio → org. Una dirección ROLE/RELAY (noreply,
        notifications, …) NO es clave de identidad → no matchea (se resuelve por nombre)."""
        key = norm_identifier("email", raw)
        if not key or is_role_email(key):
            return None
        iid = self._email.get(key)
        if iid is not None:
            return self._res(iid, method)
        domain = key.rpartition("@")[2]
        if domain:
            oid = self._domain.get(domain)
            if oid is not None:
                # el método del dominio es 'domain' salvo que venga del remitente.
                return self._res(oid, "domain" if method == "email" else method)
        return None

    def resolve(
        self,
        item: IdentityItem,
        *,
        source_platform: str | None = None,
    ) -> Resolution:
        # Cada mención se resuelve por SUS PROPIOS identificadores. El remitente del mensaje NO se
        # usa: que el correo venga de una identidad conocida (un banco, Nequi) no implica que las
        # OTRAS entidades mencionadas (el comercio donde se pagó) sean ese remitente. El remitente,
        # si importa, se extrae como su propia mención (con su email) y resuelve por email abajo.
        # 1/2. email del item → identidad; dominio → org.
        if item.email:
            res = self._by_email(item.email, "email")
            if res is not None:
                return res
        # 4. handle exacto acotado por plataforma (o único entre plataformas si no se conoce).
        if item.handle:
            hk = norm_identifier("handle", item.handle)
            if hk:
                if source_platform is not None:
                    iid = self._handle_by_platform.get((source_platform, hk))
                    if iid is not None:
                        return self._res(iid, "handle")
                else:
                    ids = self._handle_any.get(hk)
                    if ids and len(ids) == 1:
                        return self._res(next(iter(ids)), "handle")
        # 5/6. nombre exacto / alias.
        name_key = normalize_match(item.name)
        if name_key:
            iid = self._name.get(name_key)
            if iid is not None:
                return self._res(iid, "exact_name")
            iid = self._alias.get(name_key)
            if iid is not None:
                return self._res(iid, "alias")
        return Resolution.unresolved()
