"""Contrato provider-agnóstico del sync de contactos externos (módulo identidades).

Define el Protocol `ContactsProvider` (la abstracción contra la que tipa el worker de sync) y los
tipos que viajan por él (`ProviderContact`, `ProviderContactsPage`, errores). Un proveedor concreto
(`GooglePeopleClient`) aísla a su vendor (HTTP, auth, shapes de la People API) detrás de este
Protocol; el worker (`memex.modules.identidades.sync`) NUNCA tipa contra la clase concreta — mismo
patrón que `CalendarProvider` / `OCRClient`.

A diferencia de un `Source` (que empuja records crudos a `inbox` para que el LLM los procese), un
`ContactsProvider` devuelve personas YA ESTRUCTURADAS que van directo a `mod_identidades_persons`:
NO pasan por inbox/classifier/LLM (el sync de proveedor vive dentro del módulo, calca calendar).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar, Protocol, runtime_checkable

from memex.core.source import HealthResult


class ContactsProviderError(Exception):
    """Base de todos los errores de un proveedor de contactos — los callers la atrapan genérica.

    `status_code` es el HTTP status cuando aplica, o 0 para errores lógicos / de config.
    Mismo shape que `CalendarProviderError`/`OcrError`.
    """

    def __init__(self, status_code: int, message: str, body: str | None = None) -> None:
        super().__init__(f"contacts provider error {status_code}: {message}")
        self.status_code = status_code
        self.body = body


class ContactsSyncTokenExpired(ContactsProviderError):
    """El `sync_token` incremental caducó → hay que hacer un full resync (descartar el token y
    volver a traer todos los contactos).

    People API: los syncToken expiran a los 7 días; un token vencido devuelve HTTP 410 GONE con un
    `google.rpc.ErrorInfo` de reason `EXPIRED_SYNC_TOKEN` (mismo código que Google Calendar).
    """


@dataclass(frozen=True)
class ProviderAddress:
    """Una dirección de un contacto (persona → `metadata.addresses`; organización → sedes)."""

    label: str = ""
    address: str = ""
    country: str | None = None


@dataclass(frozen=True)
class ProviderIdentifier:
    """Un identificador por-fuente que el proveedor expone (handle de red, URL de perfil, …)."""

    platform: str  # 'x', 'instagram', 'linkedin', 'skype', 'url', ...
    kind: str  # 'handle' | 'url'
    value: str


@dataclass(frozen=True)
class ProviderContact:
    """Un contacto tal como lo devuelve un proveedor externo (ya estructurado).

    `resource_name` es el id estable del proveedor (People API: `people/c123…`) — clave de
    idempotencia. `etag` detecta cambios (si no cambió, el upsert no toca la fila). `deleted` lo
    marca el proveedor en el delta (`PersonMetadata.deleted`): el contacto se borró desde
    el último sync; conserva el `resource_name` (por eso el borrado NO va en una lista aparte, a
    diferencia de calendar, donde el borrado llega sin fecha mapeable).

    `birthday` solo se setea si el proveedor da fecha COMPLETA (año+mes+día). `nicknames` alimenta
    los alias de la identidad; `addresses` e `identifiers` (handles/urls) los identificadores/sedes.
    """

    resource_name: str
    etag: str | None = None
    display_name: str = ""
    given_name: str | None = None
    family_name: str | None = None
    emails: Sequence[str] = field(default_factory=tuple)
    phones: Sequence[str] = field(default_factory=tuple)
    org_name: str | None = None
    role: str | None = None
    photo_url: str | None = None
    birthday: date | None = None
    nicknames: Sequence[str] = field(default_factory=tuple)
    addresses: Sequence[ProviderAddress] = field(default_factory=tuple)
    identifiers: Sequence[ProviderIdentifier] = field(default_factory=tuple)
    deleted: bool = False


@dataclass(frozen=True)
class ProviderContactsPage:
    """Una página de la respuesta de listado.

    El worker pagina con `next_page_token` hasta agotarlo; en la última página el proveedor entrega
    `next_sync_token` (el cursor para el próximo delta). Solo uno de los dos viene seteado. Los
    borrados llegan dentro de `contacts` con `deleted=True`.
    """

    contacts: Sequence[ProviderContact] = field(default_factory=tuple)
    next_page_token: str | None = None
    next_sync_token: str | None = None


@runtime_checkable
class ContactsProvider(Protocol):
    """Interfaz de un proveedor de contactos externo, agnóstica del vendor.

    Slice 1 ejercita `health_check` + `list_delta` (ingress read-only)."""

    name: ClassVar[str]

    async def health_check(self) -> HealthResult:
        """Igual que `Source.health_check`: nunca lanza; error → HealthResult unhealthy."""
        ...

    async def list_delta(
        self,
        *,
        sync_token: str | None,
        page_token: str | None = None,
    ) -> ProviderContactsPage:
        """Una página del listado: incremental si hay `sync_token`, full si es None. `page_token`
        avanza dentro de la misma corrida. Lanza `ContactsSyncTokenExpired` si el token caducó."""
        ...
