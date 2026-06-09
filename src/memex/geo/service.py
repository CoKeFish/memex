"""Funciones de servicio de la capa geo — orquestación pura, tipada contra `GeoProvider`.

Sin argparse ni stdout: las consume tanto el CLI (`memex-geo`) como cualquier futuro caller.
`estimate_trip_from_source` es el *seam* con el slice siguiente (calendar reachability,
"¿llego a tiempo a mi próximo evento?"): ese consumidor pasará una `LocationSource` y un
destino. Ese slice NO se implementa acá; solo se deja la función importable.
"""

from __future__ import annotations

from datetime import datetime

from memex.geo.client import (
    GeocodeResult,
    GeoNotFoundError,
    GeoPoint,
    GeoProvider,
    GeoProviderError,
    LocationSource,
    PlaceResult,
    ResolvedPlace,
    TravelEstimate,
    TravelMode,
)


async def geocode_address(provider: GeoProvider, address: str) -> GeocodeResult:
    """Geocodifica una dirección/lugar con el proveedor dado."""
    return await provider.geocode(address)


async def estimate_trip(
    provider: GeoProvider,
    *,
    origin: GeoPoint,
    destination: GeoPoint,
    mode: TravelMode = TravelMode.DRIVING,
    departure_time: datetime | None = None,
) -> TravelEstimate:
    """Estima el viaje origin→destination (ambos ya en coordenadas)."""
    return await provider.travel_estimate(
        origin, destination, mode=mode, departure_time=departure_time
    )


async def estimate_trip_from_source(
    provider: GeoProvider,
    source: LocationSource,
    destination: GeoPoint,
    *,
    mode: TravelMode = TravelMode.DRIVING,
    departure_time: datetime | None = None,
) -> TravelEstimate:
    """Estima el viaje desde la ubicación de una `LocationSource` (punto X) hasta el destino.

    En v0 la fuente es `ManualLocationSource` (punto explícito); a futuro será la
    geolocalización del teléfono, sin cambiar esta firma.
    """
    origin = await source.current_location()
    return await estimate_trip(
        provider, origin=origin, destination=destination, mode=mode, departure_time=departure_time
    )


async def reverse_geocode(provider: GeoProvider, point: GeoPoint) -> GeocodeResult:
    """Coordenadas → dirección (el inverso de `geocode_address`)."""
    return await provider.reverse_geocode(point)


async def nearby_place(
    provider: GeoProvider,
    point: GeoPoint,
    *,
    radius_m: float = 50.0,
    included_types: tuple[str, ...] | None = None,
) -> PlaceResult:
    """El POI/negocio más cercano al punto (Google Places; ORS levanta `GeoProviderError`)."""
    return await provider.nearby_place(point, radius_m=radius_m, included_types=included_types)


async def resolve_place(
    provider: GeoProvider,
    point: GeoPoint,
    *,
    want_poi: bool = True,
    radius_m: float = 50.0,
) -> ResolvedPlace:
    """Resuelve un punto a algo legible: dirección + (si la hay y se pide) el nombre del POI.

    Best-effort con el POI: si no hay negocio cerca o el proveedor no lo soporta (ORS), devuelve
    solo la dirección. Levanta `GeoNotFoundError` si no resuelve NADA (ni dirección ni POI).
    """
    address: GeocodeResult | None = None
    try:
        address = await provider.reverse_geocode(point)
    except GeoNotFoundError:
        address = None

    poi: PlaceResult | None = None
    if want_poi:
        try:
            poi = await provider.nearby_place(point, radius_m=radius_m)
        except (GeoNotFoundError, GeoProviderError):
            poi = None  # sin POI cerca, o proveedor sin búsqueda de negocios → solo dirección

    if address is None and poi is None:
        raise GeoNotFoundError(point.as_latlng())

    formatted = (address.formatted_address if address else "") or (
        poi.formatted_address if poi else ""
    )
    return ResolvedPlace(
        formatted_address=formatted,
        point=point,
        name=poi.name if poi else None,
        provider_place_id=(poi.provider_place_id if poi else None)
        or (address.provider_place_id if address else None),
        types=poi.types if poi else (),
    )
