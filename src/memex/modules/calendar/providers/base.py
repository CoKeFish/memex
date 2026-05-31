"""Contrato provider-agnóstico de la sincronización de calendarios externos (ADR-015 §4 enmend.).

Define el Protocol `CalendarProvider` (la abstracción contra la que tipa el worker de sync) y los
tipos que viajan por él (`ProviderEvent`, `ProviderPage`, `ProviderEventWrite`,
`ProviderEventRef`, errores). Un proveedor concreto (`GoogleCalendarClient`) aísla a su vendor
(HTTP, auth, shapes) detrás de este Protocol; el worker (`memex.modules.calendar.sync`) NUNCA
tipa contra la clase concreta, igual que el OCR worker tipa contra `OCRClient`.

A diferencia de un `Source` (que empuja records crudos a `inbox` para que el LLM los procese), un
`CalendarProvider` devuelve eventos YA ESTRUCTURADOS que van directo al dominio
`mod_calendar_events` y corren por el mismo dedup — NO pasan por inbox/classifier/LLM (decisión
del dueño: el sync de proveedor vive dentro del módulo, no como ingestor).

Fecha/hora NAIVE a propósito (coherente con la migración 0010): un evento de calendario se ancla
en `starts_on` (DATE) + `start_time` (TIME opcional, None ⇒ todo el día). El timezone del
proveedor se descarta a la fecha/hora del calendario; componer un instante absoluto sería falsa
precisión. `updated` SÍ es TZ-aware (es el timestamp de modificación del proveedor, se usa para
detección de cambios y, a futuro, echo-suppression del write-back).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import ClassVar, Literal, Protocol, runtime_checkable

from memex.core.source import HealthResult

#: Estado de un evento del proveedor. 'cancelled' = borrado en el proveedor (lo trae el delta con
#: showDeleted); el worker lo refleja como `provider_status` sin borrar la fila local (slice 1).
ProviderEventStatus = Literal["confirmed", "tentative", "cancelled"]


class CalendarProviderError(Exception):
    """Base de todos los errores de un proveedor de calendario — los callers la atrapan genérica.

    `status_code` es el HTTP status cuando aplica, o 0 para errores lógicos / de configuración.
    Mismo shape que `OcrError`/`LLMError`.
    """

    def __init__(self, status_code: int, message: str, body: str | None = None) -> None:
        super().__init__(f"calendar provider error {status_code}: {message}")
        self.status_code = status_code
        self.body = body


class CalendarSyncTokenExpired(CalendarProviderError):
    """El `sync_token` incremental caducó (Google: HTTP 410 GONE) → hay que hacer un full resync
    (descartar el token y volver a traer todo desde `time_min`)."""


@dataclass(frozen=True)
class ProviderEvent:
    """Un evento tal como lo devuelve un proveedor externo (ya estructurado).

    `provider_event_id` es el id estable del proveedor (Google: `eventId`) — clave de idempotencia.
    `etag` detecta cambios (si no cambió, el upsert no toca la fila). `memex_consolidated_id` viene
    de las propiedades privadas del evento: si está, ese evento lo CREÓ memex (write-back) y se
    reconoce como eco, no como un evento manual nuevo.
    """

    provider_event_id: str
    title: str
    starts_on: date
    ends_on: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    location: str = ""
    description: str = ""
    status: ProviderEventStatus = "confirmed"
    etag: str | None = None
    updated: datetime | None = None
    memex_consolidated_id: str | None = None


@dataclass(frozen=True)
class ProviderPage:
    """Una página de la respuesta de listado incremental.

    El worker pagina con `next_page_token` hasta agotarlo; en la última página el proveedor
    entrega `next_sync_token` (el cursor para el próximo delta). Solo uno de los dos viene seteado.

    `deleted_ids` son los `provider_event_id` de eventos BORRADOS en el proveedor (vienen en el
    delta con `status='cancelled'`, minimalistas y SIN fecha) — separados de `events` porque no se
    pueden mapear a un `ProviderEvent` con `starts_on`. El worker los marca como tales por id.
    """

    events: Sequence[ProviderEvent] = field(default_factory=tuple)
    deleted_ids: Sequence[str] = field(default_factory=tuple)
    next_page_token: str | None = None
    next_sync_token: str | None = None


@dataclass(frozen=True)
class ProviderEventWrite:
    """Lo que memex empuja al proveedor en el write-back (slice 5). Mínimo todavía."""

    title: str
    starts_on: date
    ends_on: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    location: str = ""
    description: str = ""
    memex_consolidated_id: str | None = None


@dataclass(frozen=True)
class ProviderEventRef:
    """Referencia devuelta por el proveedor tras crear/actualizar un evento (slice 5)."""

    provider_event_id: str
    etag: str | None = None


@runtime_checkable
class CalendarProvider(Protocol):
    """Interfaz de un proveedor de calendario externo, agnóstica del vendor.

    Slice 1 ejercita `health_check` + `list_delta` (ingress read-only). Los métodos de escritura
    están en el contrato pero el cliente concreto los deja `NotImplementedError` hasta el slice de
    write-back (igual que el provider de Microsoft de IMAP arrancó como stub).
    """

    name: ClassVar[str]

    async def health_check(self) -> HealthResult:
        """Igual que `Source.health_check`: nunca lanza; error → HealthResult unhealthy."""
        ...

    async def list_delta(
        self,
        *,
        sync_token: str | None,
        page_token: str | None = None,
    ) -> ProviderPage:
        """Una página del listado: incremental si hay `sync_token`, full si es None. `page_token`
        avanza dentro de la misma corrida. Lanza `CalendarSyncTokenExpired` si el token caducó."""
        ...

    async def create_event(self, ev: ProviderEventWrite) -> ProviderEventRef:
        """Crea un evento en el proveedor (write-back, slice 5)."""
        ...

    async def update_event(
        self, *, provider_event_id: str, etag: str | None, ev: ProviderEventWrite
    ) -> ProviderEventRef:
        """Actualiza un evento existente con `If-Match: etag` (write-back, slice 5)."""
        ...

    async def delete_event(self, *, provider_event_id: str, etag: str | None) -> None:
        """Borra un evento del proveedor (write-back, slice 5)."""
        ...
