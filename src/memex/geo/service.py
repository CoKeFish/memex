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
    GeoPoint,
    GeoProvider,
    LocationSource,
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
