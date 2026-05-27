from __future__ import annotations

import json

import httpx
import pytest
import respx

from memex.ingestors.http_client import MemexAPIError, MemexClient

BASE_URL = "http://localhost:8787"


def test_get_sources_by_type_filters_enabled_and_matches_type() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/sources").respond(
            json=[
                {"id": 1, "type": "imap", "enabled": True, "config": {}, "name": "a"},
                {"id": 2, "type": "imap", "enabled": False, "config": {}, "name": "b"},
                {"id": 3, "type": "telegram", "enabled": True, "config": {}, "name": "c"},
            ]
        )
        with MemexClient(base_url=BASE_URL) as client:
            sources = client.get_sources_by_type("imap")
        assert [s["id"] for s in sources] == [1]


def test_get_checkpoint_returns_cursor_dict() -> None:
    cursor = {"folders": {"INBOX": {"uidvalidity": 5, "last_uid": 42}}}
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/sources/1/checkpoint").respond(json={"cursor": cursor})
        with MemexClient(base_url=BASE_URL) as client:
            assert client.get_checkpoint(1) == cursor


def test_get_checkpoint_returns_none_for_null_cursor() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/sources/2/checkpoint").respond(json={"cursor": None})
        with MemexClient(base_url=BASE_URL) as client:
            assert client.get_checkpoint(2) is None


def test_put_checkpoint_sends_cursor_body() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.put("/sources/1/checkpoint").respond(json={"cursor": {"x": 1}})
        with MemexClient(base_url=BASE_URL) as client:
            client.put_checkpoint(1, {"x": 1})
        assert route.called
        body = json.loads(router.calls[0].request.read())
        assert body == {"cursor": {"x": 1}}


def test_post_ingest_batch_returns_counts() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post("/ingest/batch").respond(json={"inserted": 3, "duplicates": 1, "errors": 0})
        with MemexClient(base_url=BASE_URL) as client:
            result = client.post_ingest_batch([{"source_id": 1, "external_id": "e1"}])
        assert result == {"inserted": 3, "duplicates": 1, "errors": 0}


def test_retries_on_5xx_then_succeeds() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/sources").mock(
            side_effect=[
                httpx.Response(503, text="busy"),
                httpx.Response(503, text="busy"),
                httpx.Response(200, json=[]),
            ]
        )
        with MemexClient(base_url=BASE_URL, backoff_base=0.001) as client:
            sources = client.get_sources_by_type("imap")
        assert sources == []
        assert router.calls.call_count == 3


def test_retries_exhausted_raises_memex_api_error() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/sources").respond(503)
        with (
            MemexClient(base_url=BASE_URL, backoff_base=0.001, max_retries=2) as client,
            pytest.raises(MemexAPIError) as exc_info,
        ):
            client.get_sources_by_type("imap")
        assert exc_info.value.status_code == 503
        assert router.calls.call_count == 3  # 1 inicial + 2 retries


def test_4xx_is_not_retried_and_raises_immediately() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/sources").respond(401)
        with (
            MemexClient(base_url=BASE_URL, backoff_base=0.001) as client,
            pytest.raises(MemexAPIError) as exc_info,
        ):
            client.get_sources_by_type("imap")
        assert exc_info.value.status_code == 401
        assert router.calls.call_count == 1


def test_bearer_token_attached_when_provided() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/sources").respond(json=[])
        with MemexClient(base_url=BASE_URL, api_token="secret") as client:
            client.get_sources_by_type("imap")
        assert router.calls[0].request.headers["authorization"] == "Bearer secret"


def test_network_error_retries() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/sources").mock(
            side_effect=[
                httpx.ConnectError("boom"),
                httpx.Response(200, json=[]),
            ]
        )
        with MemexClient(base_url=BASE_URL, backoff_base=0.001) as client:
            sources = client.get_sources_by_type("imap")
        assert sources == []
        assert router.calls.call_count == 2
