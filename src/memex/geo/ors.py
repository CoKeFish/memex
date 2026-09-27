"""OpenRouteServiceProvider — el ÚNICO lugar que habla HTTP con OpenRouteService (Pelias + Matrix).

Aísla al vendor detrás del Protocol `GeoProvider`. Dos diferencias clave con Google, ambas
ENCAPSULADAS acá:
  1. Orden de coordenadas: ORS es GeoJSON → `[lng, lat]`. El swap a/desde el `GeoPoint(lat, lng)`
     común vive SOLO en este módulo (entrada del geocode y body de la matrix).
  2. Auth: la key va en el header `Authorization` (sin "Bearer"), no en la URL.

ORS no modela tráfico ni horario de salida: `departure_time` se IGNORA (con warning) y
`duration_in_traffic_s` siempre es None. El modo TRANSIT no existe en ORS → `GeoProviderError`.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, ClassVar

import httpx

from memex.geo.client import (
    GeocodeResult,
    GeoNotFoundError,
    GeoPoint,
    GeoProviderError,
    GeoQuotaError,
    PlaceResult,
    TravelEstimate,
    TravelMode,
)
from memex.geo.config import GeoConfig
from memex.logging import get_logger

_BODY_PREVIEW_MAX = 500
_GEOCODE_PATH = "/geocode/search"

#: TravelMode → profile de ORS. TRANSIT no está: ORS no rutea transporte público.
_PROFILE: dict[TravelMode, str] = {
    TravelMode.DRIVING: "driving-car",
    TravelMode.WALKING: "foot-walking",
    TravelMode.CYCLING: "cycling-regular",
}


def _matrix_cell(matrix: Any) -> float | None:
    """Extrae `matrix[0][0]` (la única celda origen→destino) como float, defensivo ante null."""
    row = matrix[0] if isinstance(matrix, list) and matrix else None
    cell = row[0] if isinstance(row, list) and row else None
    return float(cell) if isinstance(cell, int | float) else None


class OpenRouteServiceProvider:
    """Cliente HTTP async para OpenRouteService (geocode Pelias + matrix v2).

    Implementa el Protocol `GeoProvider`. Construir con `client` inyectado para tests (respx).
    """

    name: ClassVar[str] = "ors"

    def __init__(self, config: GeoConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._log = get_logger("memex.geo.ors")
        headers = {
            "Authorization": config.api_key.get_secret_value(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(config.timeout_s, connect=config.connect_timeout_s),
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> OpenRouteServiceProvider:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def geocode(self, address: str) -> GeocodeResult:
        # Pelias usa `text` (no `q`). `size=1` = solo el mejor match.
        data = await self._get(_GEOCODE_PATH, {"text": address, "size": "1"}, op="geocode")

        features = data.get("features")
        if not isinstance(features, list) or not features:
            raise GeoNotFoundError(address)
        first = features[0]
        geometry = first.get("geometry") if isinstance(first, dict) else None
        coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if not isinstance(coords, list) or len(coords) < 2:
            raise GeoProviderError(0, "ors geocode feature missing coordinates")
        lng, lat = coords[0], coords[1]
        if not isinstance(lat, int | float) or not isinstance(lng, int | float):
            raise GeoProviderError(0, "ors geocode coordinates not numeric")

        props = first.get("properties") if isinstance(first, dict) else None
        label = props.get("label") or props.get("name") if isinstance(props, dict) else None
        confidence = props.get("confidence") if isinstance(props, dict) else None
        gid = props.get("gid") if isinstance(props, dict) else None
        return GeocodeResult(
            point=GeoPoint(float(lat), float(lng)),  # swap [lng,lat] → (lat,lng)
            formatted_address=str(label or address),
            provider_place_id=gid if isinstance(gid, str) else None,
            confidence=float(confidence) if isinstance(confidence, int | float) else None,
        )

    async def travel_estimate(
        self,
        origin: GeoPoint,
        destination: GeoPoint,
        *,
        mode: TravelMode = TravelMode.DRIVING,
        departure_time: datetime | None = None,
    ) -> TravelEstimate:
        profile = _PROFILE.get(mode)
        if profile is None:
            raise GeoProviderError(0, f"OpenRouteService no soporta el modo {mode.value!r}")
        if departure_time is not None:
            self._log.warning("geo.ors.departure_ignored", requested=departure_time.isoformat())

        body: dict[str, Any] = {
            "locations": [[origin.lng, origin.lat], [destination.lng, destination.lat]],  # lng,lat
            "metrics": ["duration", "distance"],
            "sources": [0],
            "destinations": [1],
        }
        query = f"{origin.lat},{origin.lng} -> {destination.lat},{destination.lng}"
        data = await self._post(f"/v2/matrix/{profile}", body, op="matrix")

        duration = _matrix_cell(data.get("durations"))
        distance = _matrix_cell(data.get("distances"))
        if duration is None or distance is None:
            # ORS devuelve null cuando no hay ruta entre los puntos.
            raise GeoNotFoundError(query)
        return TravelEstimate(
            duration_s=int(duration),
            distance_m=int(distance),
            duration_in_traffic_s=None,  # ORS no modela tráfico
            mode=mode,
        )

    async def reverse_geocode(self, point: GeoPoint) -> GeocodeResult:
        # Pelias reverse usa `point.lat`/`point.lon` (no `.lng`); `size=1` = solo el mejor match.
        params = {"point.lat": str(point.lat), "point.lon": str(point.lng), "size": "1"}
        data = await self._get("/geocode/reverse", params, op="reverse_geocode")

        features = data.get("features")
        if not isinstance(features, list) or not features:
            raise GeoNotFoundError(point.as_latlng())
        first = features[0]
        props = first.get("properties") if isinstance(first, dict) else None
        label = (props.get("label") or props.get("name")) if isinstance(props, dict) else None
        gid = props.get("gid") if isinstance(props, dict) else None
        confidence = props.get("confidence") if isinstance(props, dict) else None
        return GeocodeResult(
            point=point,  # echo del punto consultado (la dirección es lo que importa)
            formatted_address=str(label or point.as_latlng()),
            provider_place_id=gid if isinstance(gid, str) else None,
            confidence=float(confidence) if isinstance(confidence, int | float) else None,
        )

    async def nearby_place(
        self,
        point: GeoPoint,
        *,
        radius_m: float = 50.0,
        included_types: tuple[str, ...] | None = None,
    ) -> PlaceResult:
        # ORS/Pelias no hace búsqueda de negocios/POIs. Se levanta igual que el modo TRANSIT.
        del point, radius_m, included_types
        raise GeoProviderError(
            0,
            "OpenRouteService no busca POIs/negocios; usá Google (Places) para el nombre del lugar",
        )

    async def _get(self, path: str, params: dict[str, str], *, op: str) -> dict[str, Any]:
        return await self._call("GET", path, op=op, params=params)

    async def _post(self, path: str, json: dict[str, Any], *, op: str) -> dict[str, Any]:
        return await self._call("POST", path, op=op, json=json)

    async def _call(
        self,
        method: str,
        path: str,
        *,
        op: str,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        resp = await self._request(method, path, params=params, json=json)
        latency_ms = int((time.monotonic() - started) * 1000)
        self._log.info(
            "geo.ors.request", op=op, path=path, http_status=resp.status_code, latency_ms=latency_ms
        )
        data = resp.json()
        if not isinstance(data, dict):
            raise GeoProviderError(resp.status_code, "unexpected non-object response from ors")
        return data

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None,
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        """HTTP con retry de 5xx/red + backoff; 429 → GeoQuotaError; otro 4xx inmediato."""
        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                resp = await self._client.request(method, path, params=params, json=json)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                self._log.warning(
                    "geo.ors.request.network_error", path=path, exc=str(e), attempt=attempt
                )
            else:
                if resp.status_code == 429:
                    raise GeoQuotaError(
                        429,
                        "rate limit / quota exceeded",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                if 500 <= resp.status_code < 600:
                    last_exc = GeoProviderError(
                        resp.status_code,
                        f"server error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                    self._log.warning(
                        "geo.ors.request.retryable", status=resp.status_code, attempt=attempt
                    )
                elif 400 <= resp.status_code < 500:
                    raise GeoProviderError(
                        resp.status_code,
                        f"client error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                else:
                    return resp

            if attempt < self._config.max_retries:
                await asyncio.sleep(self._config.backoff_base * (2**attempt))

        if isinstance(last_exc, GeoProviderError):
            raise last_exc
        raise GeoProviderError(0, f"network error on {method} {path}") from last_exc
