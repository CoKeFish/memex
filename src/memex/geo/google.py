"""GoogleMapsProvider — el ÚNICO lugar que habla HTTP con Google Maps (Geocoding + Distance Matrix).

Aísla al vendor detrás del Protocol `GeoProvider`: los callers consumen `GeocodeResult`/
`TravelEstimate`, nunca URLs ni shapes de Google. Usa httpx **async** (NO el SDK de Google),
con el mismo patrón de retry/`aclose`/test-respx de `memex.llm.deepseek.DeepSeekClient`.

Particularidad de Google: la API responde **HTTP 200** y pone el resultado lógico en el
campo `status` del body (`OK`/`ZERO_RESULTS`/`OVER_QUERY_LIMIT`/`REQUEST_DENIED`/...). Por eso
el mapeo de errores lee ESE campo, no solo el HTTP code. La key va como **query param** `key`
(nunca se loguea la URL completa — solo path + status + latencia).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
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

# SEGURIDAD: Google exige la API key como query param `key=`. El logger de httpx emite la URL
# COMPLETA (con la key) a nivel INFO → la key se filtraría a logs / a la tabla log_events. Bajamos
# ese logger a WARNING para que la key NUNCA aparezca en logs. Nuestro propio log solo registra
# path+status, nunca la URL. (ORS no necesita esto: su key va en el header Authorization.)
logging.getLogger("httpx").setLevel(logging.WARNING)

_BODY_PREVIEW_MAX = 500
_GEOCODE_PATH = "/maps/api/geocode/json"
_MATRIX_PATH = "/maps/api/distancematrix/json"
#: Places API (New): host + endpoint propios (NO maps.googleapis.com). La key va por header
#: `X-Goog-Api-Key` (no query param) y solo se pide el FieldMask que usamos.
_PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"
_PLACES_FIELD_MASK = "places.displayName,places.formattedAddress,places.types,places.id"

#: TravelMode → valor del param `mode` de Google (OJO: CYCLING ⇒ "bicycling").
_MODE_PARAM: dict[TravelMode, str] = {
    TravelMode.DRIVING: "driving",
    TravelMode.WALKING: "walking",
    TravelMode.CYCLING: "bicycling",
    TravelMode.TRANSIT: "transit",
}


def _int_value(obj: Any, key: str) -> int | None:
    """Lee `obj[key]["value"]` como int, defensivo ante shapes raros (resp.json() es Any)."""
    sub = obj.get(key) if isinstance(obj, dict) else None
    val = sub.get("value") if isinstance(sub, dict) else None
    return int(val) if isinstance(val, int | float) else None


class GoogleMapsProvider:
    """Cliente HTTP async para Google Maps Geocoding + Distance Matrix (legacy).

    Implementa el Protocol `GeoProvider`. Construir con `client` inyectado para tests (respx),
    o dejar que cree el suyo.
    """

    name: ClassVar[str] = "google"

    def __init__(self, config: GeoConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._log = get_logger("memex.geo.google")
        # La key NO va en headers: es un query param. El AsyncClient no lleva auth.
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            timeout=httpx.Timeout(config.timeout_s, connect=config.connect_timeout_s),
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> GoogleMapsProvider:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def geocode(self, address: str) -> GeocodeResult:
        params = {"address": address, "key": self._config.api_key.get_secret_value()}
        data = await self._get(_GEOCODE_PATH, params, op="geocode")

        status = data.get("status")
        if status != "OK":
            self._raise_for_api_status(status, query=address, body=data.get("error_message"))

        results = data.get("results")
        if not isinstance(results, list) or not results:
            raise GeoNotFoundError(address)
        first = results[0]
        geometry = first.get("geometry") if isinstance(first, dict) else None
        location = geometry.get("location") if isinstance(geometry, dict) else None
        lat = location.get("lat") if isinstance(location, dict) else None
        lng = location.get("lng") if isinstance(location, dict) else None
        if not isinstance(lat, int | float) or not isinstance(lng, int | float):
            raise GeoProviderError(0, "geocode result missing numeric geometry.location")

        place_id = first.get("place_id")
        return GeocodeResult(
            point=GeoPoint(float(lat), float(lng)),
            formatted_address=str(first.get("formatted_address") or ""),
            provider_place_id=place_id if isinstance(place_id, str) else None,
        )

    async def travel_estimate(
        self,
        origin: GeoPoint,
        destination: GeoPoint,
        *,
        mode: TravelMode = TravelMode.DRIVING,
        departure_time: datetime | None = None,
    ) -> TravelEstimate:
        params: dict[str, str] = {
            "origins": origin.as_latlng(),
            "destinations": destination.as_latlng(),
            "mode": _MODE_PARAM[mode],
            "key": self._config.api_key.get_secret_value(),
        }
        if departure_time is not None:
            epoch = int(departure_time.timestamp())
            if epoch >= int(datetime.now(UTC).timestamp()):
                # Google exige departure_time presente/futuro para el cómputo con tráfico.
                params["departure_time"] = str(epoch)
                params["traffic_model"] = "best_guess"
            else:
                self._log.warning(
                    "geo.google.departure_in_past", requested=departure_time.isoformat()
                )

        query = f"{origin.as_latlng()} -> {destination.as_latlng()}"
        data = await self._get(_MATRIX_PATH, params, op="distancematrix")

        status = data.get("status")
        if status != "OK":
            self._raise_for_api_status(status, query=query, body=data.get("error_message"))

        rows = data.get("rows")
        row = rows[0] if isinstance(rows, list) and rows else None
        elements = row.get("elements") if isinstance(row, dict) else None
        element = elements[0] if isinstance(elements, list) and elements else None
        if not isinstance(element, dict):
            raise GeoProviderError(0, "distancematrix response missing rows[0].elements[0]")

        el_status = element.get("status")
        if el_status in ("NOT_FOUND", "ZERO_RESULTS"):
            raise GeoNotFoundError(query)
        if el_status != "OK":
            raise GeoProviderError(0, f"distancematrix element status {el_status!r}")

        duration_s = _int_value(element, "duration")
        distance_m = _int_value(element, "distance")
        if duration_s is None or distance_m is None:
            raise GeoProviderError(0, "distancematrix element missing duration/distance")
        return TravelEstimate(
            duration_s=duration_s,
            distance_m=distance_m,
            duration_in_traffic_s=_int_value(element, "duration_in_traffic"),
            mode=mode,
        )

    async def reverse_geocode(self, point: GeoPoint) -> GeocodeResult:
        # Mismo Geocoding API que `geocode`, pero con `latlng` en vez de `address`.
        params = {"latlng": point.as_latlng(), "key": self._config.api_key.get_secret_value()}
        data = await self._get(_GEOCODE_PATH, params, op="reverse_geocode")

        status = data.get("status")
        if status != "OK":
            self._raise_for_api_status(
                status, query=point.as_latlng(), body=data.get("error_message")
            )

        results = data.get("results")
        if not isinstance(results, list) or not results:
            raise GeoNotFoundError(point.as_latlng())
        first = results[0] if isinstance(results[0], dict) else {}
        place_id = first.get("place_id")
        return GeocodeResult(
            point=point,
            formatted_address=str(first.get("formatted_address") or ""),
            provider_place_id=place_id if isinstance(place_id, str) else None,
        )

    async def nearby_place(
        self,
        point: GeoPoint,
        *,
        radius_m: float = 50.0,
        included_types: tuple[str, ...] | None = None,
    ) -> PlaceResult:
        # Places API (New): POST con la key en header, el lugar más cercano (rank DISTANCE).
        body: dict[str, Any] = {
            "maxResultCount": 1,
            "rankPreference": "DISTANCE",
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": point.lat, "longitude": point.lng},
                    "radius": radius_m,
                }
            },
        }
        if included_types:
            body["includedTypes"] = list(included_types)
        headers = {
            "X-Goog-Api-Key": self._config.api_key.get_secret_value(),
            "X-Goog-FieldMask": _PLACES_FIELD_MASK,
            "Content-Type": "application/json",
        }
        data = await self._places_post(body, headers, op="nearby_place")

        places = data.get("places")
        if not isinstance(places, list) or not places or not isinstance(places[0], dict):
            raise GeoNotFoundError(point.as_latlng())
        first = places[0]
        display = first.get("displayName")
        name = display.get("text") if isinstance(display, dict) else None
        if not isinstance(name, str) or not name:
            raise GeoProviderError(0, "places result missing displayName.text")
        place_id = first.get("id")
        types = first.get("types")
        return PlaceResult(
            name=name,
            formatted_address=str(first.get("formattedAddress") or ""),
            point=point,
            provider_place_id=place_id if isinstance(place_id, str) else None,
            types=tuple(t for t in types if isinstance(t, str)) if isinstance(types, list) else (),
        )

    def _raise_for_api_status(self, status: Any, *, query: str, body: Any = None) -> None:
        """Mapea un `status` del body (cuando != OK) al error tipado. Nunca retorna en OK."""
        body_str = body if isinstance(body, str) else None
        if status == "ZERO_RESULTS":
            raise GeoNotFoundError(query)
        if status == "OVER_QUERY_LIMIT":
            raise GeoQuotaError(0, "OVER_QUERY_LIMIT", body=body_str)
        raise GeoProviderError(0, f"google api status {status!r}", body=body_str)

    async def _get(self, path: str, params: dict[str, str], *, op: str) -> dict[str, Any]:
        return await self._send("GET", path, op=op, log_path=path, params=params)

    async def _places_post(
        self, body: dict[str, Any], headers: dict[str, str], *, op: str
    ) -> dict[str, Any]:
        return await self._send(
            "POST",
            _PLACES_SEARCH_URL,
            op=op,
            log_path="/v1/places:searchNearby",
            json=body,
            headers=headers,
        )

    async def _send(
        self,
        method: str,
        url: str,
        *,
        op: str,
        log_path: str,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        resp = await self._request(method, url, params=params, json=json, headers=headers)
        latency_ms = int((time.monotonic() - started) * 1000)
        data = resp.json()
        api_status = data.get("status") if isinstance(data, dict) else None
        self._log.info(
            "geo.google.request",
            op=op,
            path=log_path,
            http_status=resp.status_code,
            api_status=api_status,
            latency_ms=latency_ms,
        )
        if not isinstance(data, dict):
            raise GeoProviderError(resp.status_code, "unexpected non-object response from google")
        return data

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """HTTP con retry de 429/5xx/red + backoff; 4xx HTTP-level inmediato.

        Los errores LÓGICOS de Google llegan con HTTP 200 (campo `status` del body) y se
        mapean aguas arriba; acá solo se manejan los HTTP-level (red, 5xx, 429, 4xx duro).
        """
        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                resp = await self._client.request(
                    method, url, params=params, json=json, headers=headers
                )
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                self._log.warning(
                    "geo.google.request.network_error", path=url, exc=str(e), attempt=attempt
                )
            else:
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    last_exc = GeoProviderError(
                        resp.status_code,
                        f"server/rate error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                    self._log.warning(
                        "geo.google.request.retryable", status=resp.status_code, attempt=attempt
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
        raise GeoProviderError(0, f"network error on {method} {url}") from last_exc
