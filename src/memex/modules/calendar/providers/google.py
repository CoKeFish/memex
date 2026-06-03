"""GoogleCalendarClient — el ÚNICO lugar que habla HTTP con la API de Google Calendar v3.

Aísla al vendor detrás del Protocol `CalendarProvider`: el worker consume `ProviderEvent`/
`ProviderPage`, nunca URLs ni shapes de Google. Usa httpx **async** (NO `google-api-python-client`:
arrastra httplib2 sync + discovery dinámico no-tipado que choca con `mypy --strict`; el patrón de
retry/`aclose`/test-respx ya existe en `ocr/openai_vision.py` y se calca 1:1). Se reusa solo
`google-auth` (vía `memex.google_oauth`) para mintear/refrescar el access token, que va en
`Authorization: Bearer` (nunca en la URL).

Retry de 429/5xx/red con backoff exponencial; 4xx → error inmediato; **410 GONE** (el `syncToken`
incremental caducó) → `CalendarSyncTokenExpired` (el worker hace full resync).

Fecha/hora del proveedor → NAIVE (decisión de la migración 0010): se descarta el timezone a la
fecha/hora del calendario. `updated` SÍ se conserva TZ-aware (timestamp de modificación).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from datetime import time as time_cls
from typing import Any, ClassVar

import httpx

from memex.core.source import HealthResult
from memex.logging import get_logger
from memex.modules.calendar.providers.base import (
    CalendarProviderError,
    CalendarSyncTokenExpired,
    ProviderEvent,
    ProviderEventRef,
    ProviderEventStatus,
    ProviderEventWrite,
    ProviderPage,
)
from memex.modules.calendar.providers.config import CalendarSyncConfig

_BODY_PREVIEW_MAX = 500


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_when(
    start: dict[str, Any], end: dict[str, Any]
) -> tuple[date | None, date | None, time_cls | None, time_cls | None]:
    """Mapea los bloques `start`/`end` de Google a (starts_on, ends_on, start_time, end_time)
    naive. All-day (`date`) ⇒ sin hora; el `end.date` de Google es EXCLUSIVO (día siguiente al
    último), así que un evento de un día queda con `ends_on=None`."""
    if isinstance(start.get("date"), str):
        starts_on = _parse_date(start["date"])
        if starts_on is None:
            return None, None, None, None
        ends_on: date | None = None
        if isinstance(end.get("date"), str):
            end_date = _parse_date(end["date"])
            if end_date is not None:
                last_day = end_date - timedelta(days=1)
                if last_day > starts_on:
                    ends_on = last_day
        return starts_on, ends_on, None, None

    if isinstance(start.get("dateTime"), str):
        sdt = _parse_dt(start["dateTime"])
        if sdt is None:
            return None, None, None, None
        end_time: time_cls | None = None
        ends_on = None
        if isinstance(end.get("dateTime"), str):
            edt = _parse_dt(end["dateTime"])
            if edt is not None:
                end_time = edt.time()
                if edt.date() > sdt.date():
                    ends_on = edt.date()
        return sdt.date(), ends_on, sdt.time(), end_time

    return None, None, None, None


def _parse_event(item: dict[str, Any], provider_event_id: str) -> ProviderEvent | None:
    """Construye un `ProviderEvent` de un item de la API. None si no tiene fecha mapeable."""
    starts_on, ends_on, start_time, end_time = _parse_when(
        item.get("start") or {}, item.get("end") or {}
    )
    if starts_on is None:
        return None

    raw_status = item.get("status")
    status: ProviderEventStatus = (
        raw_status if raw_status in ("confirmed", "tentative", "cancelled") else "confirmed"
    )
    updated = _parse_dt(item["updated"]) if isinstance(item.get("updated"), str) else None
    etag = item.get("etag") if isinstance(item.get("etag"), str) else None

    ext = item.get("extendedProperties")
    private = ext.get("private") if isinstance(ext, dict) else None
    mcid = private.get("memex_consolidated_id") if isinstance(private, dict) else None

    # `recurringEventId`: id de la serie a la que pertenece esta instancia (solo en instancias de
    # eventos recurrentes; ausente en los no-recurrentes). Señal autoritativa de la API, NO se
    # deriva parseando el `provider_event_id`.
    rec = item.get("recurringEventId")

    return ProviderEvent(
        provider_event_id=provider_event_id,
        title=str(item.get("summary") or ""),
        starts_on=starts_on,
        ends_on=ends_on,
        start_time=start_time,
        end_time=end_time,
        location=str(item.get("location") or ""),
        description=str(item.get("description") or ""),
        status=status,
        etag=etag,
        updated=updated,
        memex_consolidated_id=mcid if isinstance(mcid, str) else None,
        recurring_event_id=rec if isinstance(rec, str) else None,
    )


class GoogleCalendarClient:
    """Cliente HTTP async mínimo para Google Calendar v3. Implementa `CalendarProvider`.

    Construir con `client` inyectado para tests (respx), o dejar que cree el suyo.
    """

    name: ClassVar[str] = "google"

    def __init__(
        self,
        config: CalendarSyncConfig,
        access_token: str,
        *,
        calendar_id: str = "primary",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._calendar_id = calendar_id
        self._log = get_logger("memex.modules.calendar.providers.google")

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(config.timeout_s),
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> GoogleCalendarClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def health_check(self) -> HealthResult:
        now = datetime.now(UTC)
        try:
            await self._request("GET", f"/calendars/{self._calendar_id}")
        except Exception as e:  # health_check NUNCA lanza (contrato Source/CalendarProvider)
            return HealthResult(status="unhealthy", detail=f"google calendar: {e}", checked_at=now)
        return HealthResult(status="healthy", detail="google calendar reachable", checked_at=now)

    def _window_params(self) -> dict[str, str]:
        """timeMin/timeMax del full sync (RFC3339), acotando la ventana de fechas (config)."""
        now = datetime.now(UTC)
        time_min = now - timedelta(days=self._config.sync_past_days)
        time_max = now + timedelta(days=self._config.sync_future_days)
        return {
            "timeMin": time_min.isoformat().replace("+00:00", "Z"),
            "timeMax": time_max.isoformat().replace("+00:00", "Z"),
        }

    async def list_delta(
        self,
        *,
        sync_token: str | None,
        page_token: str | None = None,
    ) -> ProviderPage:
        params: dict[str, str] = {
            "singleEvents": "true",
            "showDeleted": "true",
            "maxResults": str(self._config.max_results),
        }
        # Reglas de Google events.list:
        #  - full sync: timeMin/timeMax acotan (sin esto los recurrentes se expanden a ~2001-2099).
        #    Al PAGINAR hay que RE-mandar timeMin/timeMax en cada página (si se omiten, las páginas
        #    siguientes vuelven SIN acotar). Ese era el bug.
        #  - incremental: syncToken (NO admite timeMin/timeMax); al paginar va solo el pageToken.
        if page_token:
            params["pageToken"] = page_token
            if sync_token is None:  # full sync paginado → re-mandar la ventana
                params.update(self._window_params())
        elif sync_token:
            params["syncToken"] = sync_token
        else:
            params.update(self._window_params())

        resp = await self._request("GET", f"/calendars/{self._calendar_id}/events", params=params)
        data: Any = resp.json()
        raw_items = data.get("items", []) if isinstance(data, dict) else []

        events: list[ProviderEvent] = []
        deleted_ids: list[str] = []
        for item in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(item, dict):
                continue
            pid = item.get("id")
            if not isinstance(pid, str) or not pid:
                continue
            if item.get("status") == "cancelled":
                deleted_ids.append(pid)
                continue
            event = _parse_event(item, pid)
            if event is not None:
                events.append(event)

        next_page = data.get("nextPageToken") if isinstance(data, dict) else None
        next_sync = data.get("nextSyncToken") if isinstance(data, dict) else None
        return ProviderPage(
            events=tuple(events),
            deleted_ids=tuple(deleted_ids),
            next_page_token=next_page if isinstance(next_page, str) else None,
            next_sync_token=next_sync if isinstance(next_sync, str) else None,
        )

    def _event_body(self, ev: ProviderEventWrite) -> dict[str, Any]:
        """Arma el body de un evento de Google v3 desde un `ProviderEventWrite`. Marca el evento
        con `extendedProperties.private.memex_consolidated_id` (echo-suppression del write-back)."""
        body: dict[str, Any] = {"summary": ev.title}
        if ev.location:
            body["location"] = ev.location
        if ev.description:
            body["description"] = ev.description
        if ev.start_time is None:  # all-day: Google usa `date` y el `end.date` es EXCLUSIVO.
            end_date = (ev.ends_on or ev.starts_on) + timedelta(days=1)
            body["start"] = {"date": ev.starts_on.isoformat()}
            body["end"] = {"date": end_date.isoformat()}
        else:
            start_dt = datetime.combine(ev.starts_on, ev.start_time)
            end_dt = datetime.combine(ev.ends_on or ev.starts_on, ev.end_time or ev.start_time)
            if end_dt <= start_dt:  # sin end o end<=start → 1h por default (Google exige end>start)
                end_dt = start_dt + timedelta(hours=1)
            tz = self._config.time_zone
            body["start"] = {"dateTime": start_dt.isoformat(), "timeZone": tz}
            body["end"] = {"dateTime": end_dt.isoformat(), "timeZone": tz}
        if ev.memex_consolidated_id:
            body["extendedProperties"] = {
                "private": {"memex_consolidated_id": ev.memex_consolidated_id}
            }
        return body

    async def create_event(self, ev: ProviderEventWrite) -> ProviderEventRef:
        resp = await self._request(
            "POST", f"/calendars/{self._calendar_id}/events", json=self._event_body(ev)
        )
        data: Any = resp.json()
        return ProviderEventRef(provider_event_id=str(data["id"]), etag=data.get("etag"))

    async def update_event(
        self, *, provider_event_id: str, etag: str | None, ev: ProviderEventWrite
    ) -> ProviderEventRef:
        headers = {"If-Match": etag} if etag else None
        resp = await self._request(
            "PUT",
            f"/calendars/{self._calendar_id}/events/{provider_event_id}",
            json=self._event_body(ev),
            headers=headers,
        )
        data: Any = resp.json()
        return ProviderEventRef(provider_event_id=str(data["id"]), etag=data.get("etag"))

    async def delete_event(self, *, provider_event_id: str, etag: str | None) -> None:
        headers = {"If-Match": etag} if etag else None
        try:
            await self._request(
                "DELETE",
                f"/calendars/{self._calendar_id}/events/{provider_event_id}",
                headers=headers,
            )
        except CalendarProviderError as e:
            if e.status_code in (404, 410):  # ya no existe en el proveedor → idempotente
                return
            raise

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """HTTP con retry de 429/5xx/red (backoff exponencial); 410 → sync token caducado; otro
        4xx → error inmediato."""
        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                resp = await self._client.request(
                    method, path, params=params, json=json, headers=headers
                )
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                self._log.warning(
                    "calendar.google.request.network_error", path=path, exc=str(e), attempt=attempt
                )
            else:
                if resp.status_code == 410:
                    raise CalendarSyncTokenExpired(
                        410, "sync token expired", body=resp.text[:_BODY_PREVIEW_MAX] or None
                    )
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    last_exc = CalendarProviderError(
                        resp.status_code,
                        f"server/rate error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                    self._log.warning(
                        "calendar.google.request.retryable",
                        status=resp.status_code,
                        attempt=attempt,
                    )
                elif 400 <= resp.status_code < 500:
                    raise CalendarProviderError(
                        resp.status_code,
                        f"client error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                else:
                    return resp

            if attempt < self._config.max_retries:
                await asyncio.sleep(self._config.backoff_base * (2**attempt))

        if isinstance(last_exc, CalendarProviderError):
            raise last_exc
        raise CalendarProviderError(0, f"network error on {method} {path}") from last_exc
