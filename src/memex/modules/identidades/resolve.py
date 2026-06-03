"""Resolución DETERMINISTA de una mención a una identidad canónica (persona u org conocida).

Determinismo primero (ADR / filosofía del proyecto): sin LLM en este slice. Una mención se ata a una
identidad conocida por señales fuertes, en orden de prioridad:

  1. email exacto      → persona
  2. dominio del email → org   (p. ej. `@unity.com` → la org Unity)
  3. handle exacto     → persona
  4. nombre normalizado→ persona (nombre exacto)
  5. nombre normalizado→ org     (nombre exacto u alias)
  6. nada matchea      → unresolved (coexiste; un slice futuro podría desambiguar con LLM atómico)

`KnownIndex` es puro (se arma desde listas en memoria) → testeable sin DB. El módulo lo alimenta con
lo que lee de `mod_identidades_persons` / `mod_identidades_orgs`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from memex.modules.contract import normalize
from memex.modules.identidades.schema import IdentityItem


@dataclass(frozen=True)
class KnownPerson:
    """Una persona conocida, reducida a las llaves de match."""

    id: int
    display_name: str
    emails: Sequence[str] = ()
    handles: Sequence[str] = ()


@dataclass(frozen=True)
class KnownOrg:
    """Una org conocida, reducida a las llaves de match."""

    id: int
    name: str
    aliases: Sequence[str] = ()
    domains: Sequence[str] = ()


@dataclass(frozen=True)
class Resolution:
    """Resultado de atar una mención a una entidad canónica (o no)."""

    kind: str | None  # 'person' | 'org' | None
    person_id: int | None
    org_id: int | None
    method: str  # 'email'|'domain'|'handle'|'exact_name'|'alias'|'unresolved'

    @classmethod
    def unresolved(cls) -> Resolution:
        return cls(kind=None, person_id=None, org_id=None, method="unresolved")


def _norm_handle(h: str) -> str:
    return h.strip().lower().lstrip("@")


class KnownIndex:
    """Índices en memoria para resolución determinista O(1) por mención. El primer match gana
    (`setdefault`), así el orden de inserción es estable y predecible."""

    def __init__(self, persons: Sequence[KnownPerson] = (), orgs: Sequence[KnownOrg] = ()) -> None:
        self._email_person: dict[str, int] = {}
        self._handle_person: dict[str, int] = {}
        self._name_person: dict[str, int] = {}
        self._name_org: dict[str, int] = {}
        self._alias_org: dict[str, int] = {}
        self._domain_org: dict[str, int] = {}
        for p in persons:
            self.add_person(p)
        for o in orgs:
            self.add_org(o)

    def add_person(self, p: KnownPerson) -> None:
        """Registra una persona en el índice (primer match gana). Permite que el dedup vea las
        identidades creadas dentro de la MISMA corrida de extracción."""
        for e in p.emails:
            key = e.strip().lower()
            if key:
                self._email_person.setdefault(key, p.id)
        for h in p.handles:
            key = _norm_handle(h)
            if key:
                self._handle_person.setdefault(key, p.id)
        name_key = normalize(p.display_name)
        if name_key:
            self._name_person.setdefault(name_key, p.id)

    def add_org(self, o: KnownOrg) -> None:
        """Registra una org en el índice (primer match gana)."""
        name_key = normalize(o.name)
        if name_key:
            self._name_org.setdefault(name_key, o.id)
        for a in o.aliases:
            ak = normalize(a)
            if ak:
                self._alias_org.setdefault(ak, o.id)
        for d in o.domains:
            dk = d.strip().lower()
            if dk:
                self._domain_org.setdefault(dk, o.id)

    def resolve(self, item: IdentityItem) -> Resolution:
        if item.email:
            email = item.email.strip().lower()
            pid = self._email_person.get(email)
            if pid is not None:
                return Resolution("person", pid, None, "email")
            domain = email.rpartition("@")[2]
            if domain:
                oid = self._domain_org.get(domain)
                if oid is not None:
                    return Resolution("org", None, oid, "domain")

        if item.handle:
            pid = self._handle_person.get(_norm_handle(item.handle))
            if pid is not None:
                return Resolution("person", pid, None, "handle")

        name_key = normalize(item.name)
        if name_key:
            pid = self._name_person.get(name_key)
            if pid is not None:
                return Resolution("person", pid, None, "exact_name")
            oid = self._name_org.get(name_key)
            if oid is not None:
                return Resolution("org", None, oid, "exact_name")
            oid = self._alias_org.get(name_key)
            if oid is not None:
                return Resolution("org", None, oid, "alias")

        return Resolution.unresolved()
