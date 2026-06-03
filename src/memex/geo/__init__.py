"""Capa geo de memex — geocoding + travel-time determinista detrás de un Protocol.

API pública: el Protocol `GeoProvider` + sus tipos, la `GeoConfig`, los errores tipados, el
registry de proveedores y las funciones de servicio. Los proveedores concretos
(`GoogleMapsProvider`, `OpenRouteServiceProvider`) se construyen vía `build_provider_from_env`
o el registry, nunca se importan directo desde los callers.
"""

from __future__ import annotations

from memex.geo.client import (
    GeocodeResult,
    GeoConfigError,
    GeoError,
    GeoNotFoundError,
    GeoPoint,
    GeoProvider,
    GeoProviderError,
    GeoQuotaError,
    LocationSource,
    ManualLocationSource,
    TravelEstimate,
    TravelMode,
)
from memex.geo.config import GeoConfig, known_providers
from memex.geo.providers import ProviderBuilder, build_provider_from_env, resolve
from memex.geo.service import estimate_trip, estimate_trip_from_source, geocode_address

__all__ = [
    "GeoConfig",
    "GeoConfigError",
    "GeoError",
    "GeoNotFoundError",
    "GeoPoint",
    "GeoProvider",
    "GeoProviderError",
    "GeoQuotaError",
    "GeocodeResult",
    "LocationSource",
    "ManualLocationSource",
    "ProviderBuilder",
    "TravelEstimate",
    "TravelMode",
    "build_provider_from_env",
    "estimate_trip",
    "estimate_trip_from_source",
    "geocode_address",
    "known_providers",
    "resolve",
]
