"""Capa geo de memex — geocoding + travel-time determinista + plano de ubicación + lugares.

API pública en planos:

- Servicio (geocoding/rutas): el Protocol `GeoProvider` + sus tipos, la `GeoConfig`, los errores
  tipados, el registry de proveedores y las funciones de servicio. Los proveedores concretos
  (`GoogleMapsProvider`, `OpenRouteServiceProvider`) se construyen vía `build_provider_from_env`
  o el registry, nunca se importan directo desde los callers.
- Ubicación: el almacenamiento de pings (`PingInput`/`LocationFix`), la capa de acceso que consumen
  otros módulos (`LocationDomain` + `LocationReader`) y `StoredLocationSource` (el GPS almacenado
  detrás del seam `LocationSource`).
- Lugares: resolución coordenada → dirección/POI (`reverse_geocode`, `nearby_place`, `resolve_place`
  → `ResolvedPlace`/`PlaceResult`) con caché (`resolve_place_cached`) y consciente del movimiento
  (`resolve_place_at`).
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
    PlaceResult,
    ResolvedPlace,
    TravelEstimate,
    TravelMode,
)
from memex.geo.config import GeoConfig, known_providers
from memex.geo.domain import (
    LocationDomain,
    LocationReader,
    LocationUnavailableError,
    StoredLocationSource,
    resolve_place_at,
    resolve_place_cached,
)
from memex.geo.providers import ProviderBuilder, build_provider_from_env, resolve
from memex.geo.service import (
    estimate_trip,
    estimate_trip_from_source,
    geocode_address,
    nearby_place,
    resolve_place,
    reverse_geocode,
)
from memex.geo.store import LocationFix, PingInput

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
    "LocationDomain",
    "LocationFix",
    "LocationReader",
    "LocationSource",
    "LocationUnavailableError",
    "ManualLocationSource",
    "PingInput",
    "PlaceResult",
    "ProviderBuilder",
    "ResolvedPlace",
    "StoredLocationSource",
    "TravelEstimate",
    "TravelMode",
    "build_provider_from_env",
    "estimate_trip",
    "estimate_trip_from_source",
    "geocode_address",
    "known_providers",
    "nearby_place",
    "resolve",
    "resolve_place",
    "resolve_place_at",
    "resolve_place_cached",
    "reverse_geocode",
]
