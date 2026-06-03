"""Contrato provider-agnóstico de la capa geo (mapas).

Define el Protocol `GeoProvider` (la abstracción contra la que tipan los callers) y
los tipos que viajan por él (`GeoPoint`, `GeocodeResult`, `TravelEstimate`, `TravelMode`).
Un proveedor concreto (`GoogleMapsProvider`, `OpenRouteServiceProvider`) implementa este
Protocol; los callers NUNCA tipan contra la clase concreta — calca la convención de
`memex.llm.client.LLMClient` / `memex.modules.calendar.providers.base.CalendarProvider`.

`GeoPoint` es SIEMPRE (lat, lng) — el orden humano y el de Google. El swap a/desde el
orden GeoJSON (lng, lat) que usa OpenRouteService vive ENCAPSULADO en `ors.py`, nunca
escapa de ese módulo.

`LocationSource` es el *seam* dejado a propósito para que, en el futuro, la
geolocalización del teléfono enchufe sin tocar este contrato. v0 solo trae la fuente
manual (`ManualLocationSource`): un punto X explícito. La fuente del teléfono se
implementará después (es I/O, por eso `current_location` ya es async).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Protocol, runtime_checkable


class TravelMode(StrEnum):
    """Modo de viaje. Cada proveedor lo traduce a su vocabulario.

    TRANSIT lo soporta Google pero NO OpenRouteService (que levanta `GeoProviderError`).
    """

    DRIVING = "driving"
    WALKING = "walking"
    CYCLING = "cycling"
    TRANSIT = "transit"


@dataclass(frozen=True)
class GeoPoint:
    """Un punto geográfico, SIEMPRE en orden (lat, lng).

    El orden GeoJSON (lng, lat) de OpenRouteService se normaliza dentro de `ors.py`.
    """

    lat: float
    lng: float

    def as_latlng(self) -> str:
        """Formato `"lat,lng"` que usan los query params de Google (origins/destinations)."""
        return f"{self.lat},{self.lng}"


@dataclass(frozen=True)
class GeocodeResult:
    """Resultado de geocodificar una dirección → coordenadas + metadata."""

    point: GeoPoint
    formatted_address: str
    #: Google: `place_id`; ORS: `properties.gid`; None si el proveedor no lo da.
    provider_place_id: str | None = None
    #: ORS: `properties.confidence` (0..1); Google no expone confianza → None.
    confidence: float | None = None


@dataclass(frozen=True)
class TravelEstimate:
    """Estimación de un viaje A→B: duración + distancia (+ duración con tráfico si aplica)."""

    duration_s: int
    distance_m: int
    #: None si el proveedor no modela tráfico (ORS) o si no se pidió `departure_time`.
    duration_in_traffic_s: int | None = None
    mode: TravelMode = TravelMode.DRIVING


class GeoError(Exception):
    """Base de todos los errores de la capa geo — los callers la atrapan genérica.

    `status_code` es el HTTP status cuando aplica, o 0 para errores lógicos / de
    configuración. Mismo shape que `LLMError`/`CalendarProviderError`.
    """

    def __init__(self, status_code: int, message: str, body: str | None = None) -> None:
        super().__init__(f"geo error {status_code}: {message}")
        self.status_code = status_code
        self.body = body


class GeoConfigError(GeoError):
    """Config inválida o falta la env var de la API key. `status_code=0`."""

    def __init__(self, message: str) -> None:
        super().__init__(0, message)


class GeoProviderError(GeoError):
    """Error REAL del proveedor: REQUEST_DENIED/INVALID_REQUEST, 4xx no-cuota, o red agotada."""


class GeoQuotaError(GeoProviderError):
    """Cuota/rate-limit del proveedor agotada (OVER_QUERY_LIMIT / HTTP 429) — NO reintentable.

    Calca `LLMQuotaError`: el cliente la levanta inmediato y los callers la dejan propagar
    en vez de seguir gastando llamadas.
    """


class GeoNotFoundError(GeoError):
    """Sin resultados (ZERO_RESULTS / `features` vacío / sin ruta) — NO es un fallo de operación.

    Se distingue a propósito de `GeoProviderError`: una dirección que no existe o un par sin
    ruta es un resultado vacío LEGÍTIMO, no un error del proveedor. `status_code=0`.
    """

    def __init__(self, query: str) -> None:
        super().__init__(0, f"no geo result for {query!r}")
        self.query = query


@runtime_checkable
class GeoProvider(Protocol):
    """Interfaz de geocoding + travel-time agnóstica del proveedor.

    Una implementación concreta aísla a su vendor (HTTP, auth, shapes, orden de coords)
    detrás de estos dos métodos. `name` identifica al proveedor para logging/registry.
    """

    name: ClassVar[str]

    async def geocode(self, address: str) -> GeocodeResult:
        """Dirección/lugar → `GeocodeResult`. `GeoNotFoundError` si no hay match."""
        ...

    async def travel_estimate(
        self,
        origin: GeoPoint,
        destination: GeoPoint,
        *,
        mode: TravelMode = TravelMode.DRIVING,
        departure_time: datetime | None = None,
    ) -> TravelEstimate:
        """Estima el viaje origin→destination.

        `departure_time` (aware) pide una estimación con tráfico para esa hora; los
        proveedores que no lo soporten lo IGNORAN (con warning) y devuelven
        `duration_in_traffic_s=None`. `GeoNotFoundError` si no hay ruta.
        """
        ...

    async def aclose(self) -> None:
        """Cierra el cliente HTTP subyacente. Llamar al terminar (o usar `async with`)."""
        ...


@runtime_checkable
class LocationSource(Protocol):
    """Fuente de la ubicación "actual" del usuario — el punto de partida X de un viaje.

    SEAM dejado a propósito: hoy solo existe `ManualLocationSource`; mañana una fuente
    de geolocalización del teléfono implementará este mismo Protocol sin tocar el resto.
    `async` porque la fuente real hará I/O.
    """

    async def current_location(self) -> GeoPoint: ...


class ManualLocationSource:
    """Fuente de ubicación manual: devuelve un punto X fijo y explícito.

    Única implementación de `LocationSource` en v0. La provee el CLI (`--from-point`) o
    cualquier caller que ya conozca el origen.
    """

    def __init__(self, point: GeoPoint) -> None:
        self._point = point

    async def current_location(self) -> GeoPoint:
        return self._point
