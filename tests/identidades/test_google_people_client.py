"""GooglePeopleClient con respx (sin red). Espeja tests/calendar/test_google_client.py.

Cubre: que cumple el Protocol, el parseo de un contacto (primary vs first, emails lowercased,
org/photo), el flag `deleted` del delta, la captura de `nextSyncToken`/`nextPageToken`, el bearer
fuera de la URL, los params (personFields/requestSyncToken/syncToken/pageToken), el 410 y el 400 con
EXPIRED_SYNC_TOKEN → token expirado, y la lógica de retry (4xx inmediato / 5xx retry / red retry).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from memex.modules.identidades.providers.base import (
    ContactsProvider,
    ContactsProviderError,
    ContactsSyncTokenExpired,
)
from memex.modules.identidades.providers.config import ContactsSyncConfig
from memex.modules.identidades.providers.google import GooglePeopleClient

BASE_URL = "https://people.example.com/v1"
CONNECTIONS = "/people/me/connections"
PROFILE = "/people/me"


def _client() -> GooglePeopleClient:
    cfg = ContactsSyncConfig(base_url=BASE_URL, backoff_base=0.001, max_retries=3)
    return GooglePeopleClient(cfg, "TKN")


def _connections_body() -> dict[str, Any]:
    return {
        "connections": [
            {
                "resourceName": "people/c1",
                "etag": '"abc"',
                "metadata": {"objectType": "PERSON"},
                "names": [
                    {"metadata": {"primary": False}, "displayName": "Nombre Viejo"},
                    {
                        "metadata": {"primary": True},
                        "displayName": "Ada Lovelace",
                        "givenName": "Ada",
                        "familyName": "Lovelace",
                    },
                ],
                "emailAddresses": [
                    {"value": "ada.work@unity.com"},
                    {"metadata": {"primary": True}, "value": "Ada@Example.com"},
                ],
                "phoneNumbers": [{"value": "+1 555 0100"}],
                "organizations": [
                    {"metadata": {"primary": True}, "name": "Unity", "title": "Engineer"}
                ],
                "photos": [{"metadata": {"primary": True}, "url": "https://photo/ada.jpg"}],
            },
            {
                "resourceName": "people/c2",
                "metadata": {"deleted": True},  # borrado en el delta: conserva resourceName
            },
        ],
        "nextSyncToken": "SYNC2",
    }


def test_satisfies_protocol() -> None:
    # `name` es un ClassVar del Protocol → isinstance aplica (issubclass no: miembro no-método).
    assert isinstance(_client(), ContactsProvider)


@pytest.mark.asyncio
async def test_list_delta_parses_contact_and_captures_sync_token() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(CONNECTIONS).respond(json=_connections_body())
        async with _client() as c:
            page = await c.list_delta(sync_token=None)

    assert route.called
    assert page.next_sync_token == "SYNC2"
    assert page.next_page_token is None
    assert [c.resource_name for c in page.contacts] == ["people/c1", "people/c2"]

    c1 = page.contacts[0]
    assert c1.display_name == "Ada Lovelace"  # primary, no el primero
    assert c1.given_name == "Ada"
    assert c1.family_name == "Lovelace"
    assert c1.emails == ("ada@example.com", "ada.work@unity.com")  # primary primero, lowercased
    assert c1.phones == ("+1 555 0100",)
    assert c1.org_name == "Unity"
    assert c1.role == "Engineer"
    assert c1.photo_url == "https://photo/ada.jpg"
    assert c1.etag == '"abc"'
    assert c1.deleted is False

    c2 = page.contacts[1]
    assert c2.deleted is True
    assert c2.display_name == ""  # borrado: sin names

    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer TKN"  # bearer en header, no en URL
    url = str(req.url)
    assert "personFields=" in url
    assert "requestSyncToken=true" in url  # full sync (sync_token=None)
    assert "pageSize=1000" in url
    assert "syncToken" not in url


@pytest.mark.asyncio
async def test_incremental_sends_sync_token_not_request_token() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(CONNECTIONS).respond(json={"connections": [], "nextPageToken": "P2"})
        async with _client() as c:
            page = await c.list_delta(sync_token="TOK1")
        assert page.next_page_token == "P2"
        assert page.next_sync_token is None
        url = str(route.calls[0].request.url)
        assert "syncToken=TOK1" in url
        assert "requestSyncToken" not in url  # incremental no pide token nuevo


@pytest.mark.asyncio
async def test_pagination_sends_page_token_and_resends_fields() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(CONNECTIONS).respond(json={"connections": [], "nextSyncToken": "Z"})
        async with _client() as c:
            await c.list_delta(sync_token="TOK1", page_token="PAGE")
        url = str(route.calls[0].request.url)
        assert "pageToken=PAGE" in url
        assert "syncToken=TOK1" in url  # params deben coincidir con la 1ª llamada
        assert "personFields=" in url


@pytest.mark.asyncio
async def test_410_raises_sync_token_expired() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get(CONNECTIONS).respond(410, text="gone")
        async with _client() as c:
            with pytest.raises(ContactsSyncTokenExpired) as exc:
                await c.list_delta(sync_token="OLD")
        assert exc.value.status_code == 410
        assert router.calls.call_count == 1  # no se reintenta


@pytest.mark.asyncio
async def test_400_expired_sync_token_raises_expired() -> None:
    body = '{"error":{"status":"FAILED_PRECONDITION","details":[{"reason":"EXPIRED_SYNC_TOKEN"}]}}'
    with respx.mock(base_url=BASE_URL) as router:
        router.get(CONNECTIONS).respond(400, text=body)
        async with _client() as c:
            with pytest.raises(ContactsSyncTokenExpired):
                await c.list_delta(sync_token="OLD")
        assert router.calls.call_count == 1


@pytest.mark.asyncio
async def test_4xx_raises_immediately() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get(CONNECTIONS).respond(403, text="forbidden")
        async with _client() as c:
            with pytest.raises(ContactsProviderError) as exc:
                await c.list_delta(sync_token=None)
        assert exc.value.status_code == 403
        assert router.calls.call_count == 1  # sin retries


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get(CONNECTIONS).mock(
            side_effect=[
                httpx.Response(503, text="busy"),
                httpx.Response(200, json={"connections": [], "nextSyncToken": "T"}),
            ]
        )
        async with _client() as c:
            page = await c.list_delta(sync_token=None)
        assert page.next_sync_token == "T"
        assert router.calls.call_count == 2


@pytest.mark.asyncio
async def test_network_error_retries_then_raises() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(CONNECTIONS).mock(side_effect=httpx.ConnectError("boom"))
        async with _client() as c:
            with pytest.raises(ContactsProviderError):
                await c.list_delta(sync_token=None)
        assert route.call_count == 4  # max_retries=3 → 4 intentos


@pytest.mark.asyncio
async def test_health_check_ok_and_unhealthy() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get(PROFILE).respond(json={"resourceName": "people/me"})
        async with _client() as c:
            res = await c.health_check()
        assert res.status == "healthy"

    with respx.mock(base_url=BASE_URL) as router:
        router.get(PROFILE).respond(500, text="boom")
        async with _client() as c:
            res = await c.health_check()
        assert res.status == "unhealthy"
