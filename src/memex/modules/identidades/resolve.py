"""Resolución DETERMINISTA de una mención a una identidad canónica (persona, organización,
producto o desconocido).

Señales FUERTES de la MENCIÓN MISMA, en orden de prioridad (sin LLM; el difuso y el desempate LLM
viven en `fuzzy.py` / `dedup_llm.py`). El remitente del mensaje NO se usa como señal: que el correo
venga de una identidad conocida no implica que las otras entidades mencionadas sean ese remitente.

  1. email exacto del item                → identidad
  2. dominio del email                     → ORG dueña del dominio (NUNCA una persona)
  3. handle exacto ACOTADO POR PLATAFORMA  → identidad   (el handle de X ≠ Instagram ≠ ...)
  4. nombre normalizado exacto             → identidad del MISMO grupo (persona ∥ org+producto)
  5. alias normalizado                     → ídem (kind desconocido: cross-grupo solo si es único)
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
KIND_PRODUCTO = "producto"
#: «Pendiente de clasificación»: sabemos que la entidad existe (un correo/handle real) pero su tipo
#: aún no se definió (lo fija set-kind manual, un clasificador futuro, o una extracción posterior).
KIND_DESCONOCIDO = "desconocido"
#: Los kinds canónicos del directorio (sin el escape 'unknown' del extractor, que es vocabulario de
#: la MENCIÓN, no del directorio). Incluye `desconocido`.
_CANONICAL_KINDS = (KIND_PERSONA, KIND_ORG, KIND_PRODUCTO, KIND_DESCONOCIDO)


def _match_group(kind: str) -> str:
    """Grupo de match del nombre/alias exacto. PERSONA es su propio grupo; ORGANIZACIÓN y PRODUCTO
    comparten grupo ("entity"): suelen ser la MISMA entidad (Steam/Claude = org Y producto), así
    que una mención producto resuelve a la org homónima y viceversa. DESCONOCIDO es su PROPIO grupo:
    un placeholder de tipo incierto no se funde por nombre con una persona/org (sería adivinar el
    tipo) — una mención de kind definido SÍ puede RECLAMARLO por nombre (sin promover su tipo), pero
    no al revés. Lo que NUNCA se cruza es persona↔org/producto — el colapso dañino (persona con
    correo corporativo u homónima)."""
    if kind == KIND_PERSONA:
        return KIND_PERSONA
    if kind == KIND_DESCONOCIDO:
        return KIND_DESCONOCIDO
    return "entity"


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
    kind: str  # 'persona' | 'organizacion' | 'producto' | 'desconocido'
    display_name: str
    aliases: Sequence[str] = ()
    identifiers: Sequence[KnownIdentifier] = field(default_factory=tuple)


@dataclass(frozen=True)
class Resolution:
    """Resultado de atar una mención a una identidad canónica (o no)."""

    kind: str | None  # 'persona' | 'organizacion' | 'producto' | 'desconocido' | None
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
        # Nombre/alias ACOTADOS POR KIND: (kind, key) → id, para que una mención de un kind no
        # matchee un homónimo de otro. `_name_any`/`_alias_any` (key → ids) son la vista cross-kind
        # del path de kind DESCONOCIDO (escape del extractor / seam de dominio): resuelve solo si el
        # nombre es ÚNICO entre todos los kinds.
        self._name: dict[tuple[str, str], int] = {}
        self._alias: dict[tuple[str, str], int] = {}
        self._name_any: dict[str, set[int]] = {}
        self._alias_any: dict[str, set[int]] = {}
        for ident in identities:
            self.add(ident)

    def add(self, ident: KnownIdentity) -> None:
        """Registra una identidad (primer match gana). Permite que el dedup vea identidades creadas
        dentro de la MISMA corrida de extracción."""
        self._kind.setdefault(ident.id, ident.kind)
        grp = _match_group(ident.kind)
        name_key = normalize_match(ident.display_name)
        if name_key:
            self._name.setdefault((grp, name_key), ident.id)
            self._name_any.setdefault(name_key, set()).add(ident.id)
        for a in ident.aliases:
            ak = normalize_match(a)
            if ak:
                self._alias.setdefault((grp, ak), ident.id)
                self._alias_any.setdefault(ak, set()).add(ident.id)
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
            kind = self._kind.get(identity_id)
            if kind is not None:
                self._alias.setdefault((_match_group(kind), ak), identity_id)
            self._alias_any.setdefault(ak, set()).add(identity_id)

    # --- accessores por-identificador para la resolución de REMITENTE (Fase 2) -------------- #
    # La resolución del remitente (modules/identidades/senders.py) tiene una POLÍTICA propia por
    # medio (corporativo→org por dominio, free-mail→persona por email, social→handle) distinta de
    # `resolve()`/`_by_email` (que corta la cascada de menciones en los emails role a propósito).
    # Estos lookups exponen los índices ya cargados para que senders.py reutilice el `KnownIndex`
    # (una sola carga por lote) + el `add()` intra-lote, sin re-consultar la DB por mensaje. Los
    # valores van YA normalizados (`norm_identifier`).

    def email_identity(self, value_norm: str) -> int | None:
        """Identidad cuyo identifier email coincide exacto, si la hay."""
        return self._email.get(value_norm)

    def domain_identity(self, value_norm: str) -> int | None:
        """Org cuyo identifier `domain` coincide exacto, si la hay."""
        return self._domain.get(value_norm)

    def handle_identity(self, platform: str, value_norm: str) -> int | None:
        """Identidad con ese `handle` en ESA plataforma, si la hay (estricto por plataforma)."""
        return self._handle_by_platform.get((platform, value_norm))

    def kind_of(self, identity_id: int) -> str | None:
        """kind canónico (persona|organizacion|producto|desconocido) de una identidad, si está."""
        return self._kind.get(identity_id)

    def _res(self, identity_id: int, method: str) -> Resolution:
        return Resolution(self._kind.get(identity_id), identity_id, method)

    def _by_email(
        self, raw: str, method: str, *, probe_kind: str | None = None
    ) -> Resolution | None:
        """email exacto → identidad; si no, dominio → ORG. Una dirección ROLE/RELAY (noreply,
        notifications, …) NO es clave de identidad → no matchea (se resuelve por nombre).

        El fallback por DOMINIO solo resuelve a una ORGANIZACIÓN (el dueño del dominio es una org) y
        NUNCA si la mención se asume persona: un dominio no identifica a una persona. Que alguien
        use un correo corporativo no lo vuelve la org del dominio (la afiliación se modela aparte);
        una mención persona con correo institucional cae al match por NOMBRE, no a la org.
        """
        key = norm_identifier("email", raw)
        if not key or is_role_email(key):
            return None
        iid = self._email.get(key)
        if iid is not None:
            return self._res(iid, method)
        if probe_kind == KIND_PERSONA:
            return None  # un dominio nunca identifica a una persona
        # Dominio REGISTRABLE (eTLD+1) — mismo cómputo que los identifiers `domain` se guardan
        # (norm_identifier('domain')), si no el lookup no matchearía un identifier colapsado. Solo
        # vale si el dueño del dominio es una org (no una persona): el dominio es señal de org.
        domain = norm_identifier("domain", key)
        if domain:
            oid = self._domain.get(domain)
            if oid is not None and self._kind.get(oid) != KIND_PERSONA:
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
        # El kind de la mención acota el match; 'unknown' (escape del extractor o seam de dominio)
        # resuelve CROSS-KIND, pero solo si el nombre es único.
        probe_kind = item.kind if item.kind in _CANONICAL_KINDS else None
        # 1/2. email del item → identidad; dominio → org (nunca persona).
        if item.email:
            res = self._by_email(item.email, "email", probe_kind=probe_kind)
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
        # 5/6. nombre exacto / alias, ACOTADO POR GRUPO (persona ∥ org+producto ∥ desconocido): no
        # funde homónimos persona↔org. Con el escape 'unknown' del extractor (probe_kind None)
        # matchea cross-grupo solo si el nombre/alias es ÚNICO.
        name_key = normalize_match(item.name)
        if name_key:
            if probe_kind is not None:
                grp = _match_group(probe_kind)
                iid = self._name.get((grp, name_key))
                if iid is not None:
                    return self._res(iid, "exact_name")
                iid = self._alias.get((grp, name_key))
                if iid is not None:
                    return self._res(iid, "alias")
                # Reclamo de placeholder: una mención de kind DEFINIDO que no matcheó en su grupo se
                # ata a un `desconocido` homónimo si lo hay (lo ABSORBE sin promover su tipo — el
                # placeholder sigue `desconocido` hasta que un sistema lo defina; `_res` devuelve el
                # kind de la ENTIDAD, no el de la mención). Asimétrico: el desconocido NO reclama
                # hacia una persona/org (eso sería adivinar el tipo, justo lo que se evita).
                if probe_kind != KIND_DESCONOCIDO:
                    iid = self._name.get((KIND_DESCONOCIDO, name_key))
                    if iid is not None:
                        return self._res(iid, "exact_name")
                    iid = self._alias.get((KIND_DESCONOCIDO, name_key))
                    if iid is not None:
                        return self._res(iid, "alias")
            else:
                nids = self._name_any.get(name_key)
                if nids and len(nids) == 1:
                    return self._res(next(iter(nids)), "exact_name")
                aids = self._alias_any.get(name_key)
                if aids and len(aids) == 1:
                    return self._res(next(iter(aids)), "alias")
        return Resolution.unresolved()
