"""GoogleCalendarClient con respx (sin red). Espeja tests/ocr/test_openai_vision.py.

Cubre: que cumple el Protocol, el parseo de eventos (timed/all-day → naive, tz descartado),
la separación de cancelados a `deleted_ids`, la captura de `nextSyncToken`/`nextPageToken`, el
bearer fuera de la URL, los params (singleEvents/showDeleted/syncToken), el 410 → token expirado,
y la lógica de retry (4xx inmediato / 5xx retry / red retry).
"""

from __future__ import annotations

import json
from datetime import date, time
from typing import Any

import httpx
import pytest
import respx

from memex.modules.calendar.providers.base import (
    CalendarProvider,
    CalendarProviderError,
    CalendarSyncTokenExpired,
    ProviderEventWrite,
)
from memex.modules.calendar.providers.config import CalendarSyncConfig
from memex.modules.calendar.providers.google import GoogleCalendarClient

BASE_URL = "https://cal.example.com/v3"
EVENTS = "/calendars/primary/events"


def _client() -> GoogleCalendarClient:
    cfg = CalendarSyncConfig(base_url=BASE_URL, backoff_base=0.001, max_retries=3)
    return GoogleCalendarClient(cfg, "TKN")


def _events_body() -> dict[str, Any]:
    return {
        "items": [
            {
                "id": "ev1",
                "etag": '"123"',
                "status": "confirmed",
                "summary": "Reunión",
                "location": "Sala 2",
                "description": "sync semanal",
                "updated": "2026-05-20T10:00:00Z",
                "recurringEventId": "series-xyz",  # instancia de una serie recurrente
                "start": {"dateTime": "2026-06-03T15:30:00-03:00"},
                "end": {"dateTime": "2026-06-03T16:30:00-03:00"},
            },
            {
                "id": "ev2",
                "status": "confirmed",
                "summary": "Feriado",
                "start": {"date": "2026-06-05"},
                "end": {"date": "2026-06-06"},  # end exclusivo → evento de UN día
            },
            {"id": "ev3", "status": "cancelled"},  # borrado → deleted_ids (sin fecha)
        ],
        "nextSyncToken": "TOK2",
    }


def test_satisfies_protocol() -> None:
    # `name` es un ClassVar del Protocol → issubclass no aplica (miembro no-método); isinstance sí.
    assert isinstance(_client(), CalendarProvider)


@pytest.mark.asyncio
async def test_list_delta_parses_events_and_captures_sync_token() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(EVENTS).respond(json=_events_body())
        async with _client() as c:
            page = await c.list_delta(sync_token=None)

    assert route.called
    assert page.next_sync_token == "TOK2"
    assert page.next_page_token is None
    assert [e.provider_event_id for e in page.events] == ["ev1", "ev2"]
    assert page.deleted_ids == ("ev3",)

    ev1 = page.events[0]
    assert ev1.title == "Reunión"
    assert ev1.starts_on == date(2026, 6, 3)
    assert ev1.start_time == time(15, 30)  # tz -03:00 descartado → hora local naive
    assert ev1.end_time == time(16, 30)
    assert ev1.ends_on is None
    assert ev1.location == "Sala 2"
    assert ev1.etag == '"123"'
    assert ev1.recurring_event_id == "series-xyz"  # capturado de recurringEventId

    ev2 = page.events[1]
    assert ev2.starts_on == date(2026, 6, 5)
    assert ev2.start_time is None  # all-day
    assert ev2.ends_on is None  # un solo día (end.date es exclusivo)
    assert ev2.recurring_event_id is None  # no recurrente → sin recurringEventId

    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer TKN"  # bearer en header, no en URL
    url = str(req.url)
    assert "singleEvents=true" in url
    assert "showDeleted=true" in url
    assert "syncToken" not in url  # full sync (sync_token=None)


@pytest.mark.asyncio
async def test_list_delta_multiday_all_day_event() -> None:
    body = {
        "items": [
            {
                "id": "conf",
                "status": "confirmed",
                "summary": "Conferencia",
                "start": {"date": "2026-06-03"},
                "end": {"date": "2026-06-06"},  # 3 al 5 (end exclusivo)
            }
        ],
        "nextSyncToken": "T",
    }
    with respx.mock(base_url=BASE_URL) as router:
        router.get(EVENTS).respond(json=body)
        async with _client() as c:
            page = await c.list_delta(sync_token=None)
    ev = page.events[0]
    assert ev.starts_on == date(2026, 6, 3)
    assert ev.ends_on == date(2026, 6, 5)


@pytest.mark.asyncio
async def test_list_delta_sends_sync_token_and_page_token() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(EVENTS).respond(json={"items": [], "nextPageToken": "P2"})
        async with _client() as c:
            page = await c.list_delta(sync_token="TOK1")
        assert page.next_page_token == "P2"
        assert page.next_sync_token is None
        assert "syncToken=TOK1" in str(route.calls[0].request.url)

    # Con page_token, manda pageToken y NO syncToken (Google: no cambiar params al paginar).
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(EVENTS).respond(json={"items": [], "nextSyncToken": "Z"})
        async with _client() as c:
            await c.list_delta(sync_token="TOK1", page_token="PAGE")
        url = str(route.calls[0].request.url)
        assert "pageToken=PAGE" in url
        assert "syncToken" not in url


@pytest.mark.asyncio
async def test_full_sync_bounds_time_window() -> None:
    # Full sync (sin syncToken) acota con timeMin/timeMax para no expandir recurrentes a 2001-2099.
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(EVENTS).respond(json={"items": [], "nextSyncToken": "T"})
        async with _client() as c:
            await c.list_delta(sync_token=None)
        url = str(route.calls[0].request.url)
        assert "timeMin=" in url
        assert "timeMax=" in url


@pytest.mark.asyncio
async def test_full_sync_pagination_resends_window() -> None:
    # Bug fix: al paginar el full sync (pageToken + sin syncToken) hay que RE-mandar la ventana;
    # si no, Google devuelve las páginas siguientes SIN acotar.
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(EVENTS).respond(json={"items": [], "nextSyncToken": "T"})
        async with _client() as c:
            await c.list_delta(sync_token=None, page_token="PAGE2")
        url = str(route.calls[0].request.url)
        assert "pageToken=PAGE2" in url
        assert "timeMin=" in url
        assert "timeMax=" in url


@pytest.mark.asyncio
async def test_incremental_sync_omits_time_window() -> None:
    # Con syncToken (incremental) Google NO admite timeMin/timeMax → no se mandan.
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(EVENTS).respond(json={"items": [], "nextSyncToken": "T"})
        async with _client() as c:
            await c.list_delta(sync_token="TOK1")
        url = str(route.calls[0].request.url)
        assert "timeMin" not in url
        assert "timeMax" not in url


@pytest.mark.asyncio
async def test_410_raises_sync_token_expired() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get(EVENTS).respond(410, text="gone")
        async with _client() as c:
            with pytest.raises(CalendarSyncTokenExpired) as exc:
                await c.list_delta(sync_token="OLD")
        assert exc.value.status_code == 410
        assert router.calls.call_count == 1  # 410 no se reintenta


@pytest.mark.asyncio
async def test_4xx_raises_immediately() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get(EVENTS).respond(403, text="forbidden")
        async with _client() as c:
            with pytest.raises(CalendarProviderError) as exc:
                await c.list_delta(sync_token=None)
        assert exc.value.status_code == 403
        assert router.calls.call_count == 1  # sin retries


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get(EVENTS).mock(
            side_effect=[
                httpx.Response(503, text="busy"),
                httpx.Response(200, json={"items": [], "nextSyncToken": "T"}),
            ]
        )
        async with _client() as c:
            page = await c.list_delta(sync_token=None)
        assert page.next_sync_token == "T"
        assert router.calls.call_count == 2


@pytest.mark.asyncio
async def test_network_error_retries_then_raises() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(EVENTS).mock(side_effect=httpx.ConnectError("boom"))
        async with _client() as c:
            with pytest.raises(CalendarProviderError):
                await c.list_delta(sync_token=None)
        assert route.call_count == 4  # max_retries=3 → 4 intentos


# ----- write-back (slice 5): create / update / delete ---------------------------- #


@pytest.mark.asyncio
async def test_create_event_builds_body_with_marker() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(EVENTS).respond(json={"id": "newid", "etag": '"e9"'})
        async with _client() as c:
            ref = await c.create_event(
                ProviderEventWrite(
                    title="Cita",
                    starts_on=date(2026, 6, 3),
                    start_time=time(15, 0),
                    location="Centro",
                    memex_consolidated_id="42",
                )
            )
        assert ref.provider_event_id == "newid"
        assert ref.etag == '"e9"'
        body = json.loads(route.calls[0].request.content)
        assert body["summary"] == "Cita"
        assert body["start"]["dateTime"].startswith("2026-06-03T15:00")
        assert body["location"] == "Centro"
        assert body["extendedProperties"]["private"]["memex_consolidated_id"] == "42"


@pytest.mark.asyncio
async def test_create_all_day_event_uses_exclusive_end() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(EVENTS).respond(json={"id": "x", "etag": "e"})
        async with _client() as c:
            await c.create_event(ProviderEventWrite(title="Feriado", starts_on=date(2026, 6, 5)))
        body = json.loads(route.calls[0].request.content)
        assert body["start"]["date"] == "2026-06-05"
        assert body["end"]["date"] == "2026-06-06"  # end exclusivo


@pytest.mark.asyncio
async def test_update_event_sends_if_match() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.put(f"{EVENTS}/abc").respond(json={"id": "abc", "etag": '"e10"'})
        async with _client() as c:
            ref = await c.update_event(
                provider_event_id="abc",
                etag='"e9"',
                ev=ProviderEventWrite(title="X", starts_on=date(2026, 6, 3)),
            )
        assert ref.etag == '"e10"'
        assert route.calls[0].request.headers["If-Match"] == '"e9"'


@pytest.mark.asyncio
async def test_delete_event_swallows_404() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.delete(f"{EVENTS}/ok").respond(204)
        router.delete(f"{EVENTS}/gone").respond(404, text="not found")
        async with _client() as c:
            await c.delete_event(provider_event_id="ok", etag='"e"')  # no lanza
            await c.delete_event(provider_event_id="gone", etag=None)  # 404 → idempotente, no lanza
