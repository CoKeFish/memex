"""Tests del GatewayClient — la cara HTTP del cliente local hacia /gateway/plugins/<name>/."""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx

from memex.ingestors.gateway_client import GatewayClient

BASE = "http://localhost:8787"


def _state_response(
    source_id: int = 42,
    cursor: dict[str, Any] | None = None,
    created: bool = False,
) -> dict[str, Any]:
    return {"source_id": source_id, "cursor": cursor, "created": created}


def test_get_checkpoint_calls_state_and_returns_cursor() -> None:
    cursor = {"last": "x"}
    with respx.mock(base_url=BASE) as router:
        route = router.post("/gateway/plugins/p/state").respond(
            json=_state_response(source_id=7, cursor=cursor)
        )
        with GatewayClient(BASE, "p", "outlook") as c:
            assert c.get_checkpoint() == cursor
            assert c.resolved_source_id == 7
        assert route.called
        body = json.loads(router.calls[0].request.read())
        assert body == {"source_type": "outlook"}


def test_state_called_only_once_across_methods() -> None:
    with respx.mock(base_url=BASE) as router:
        router.post("/gateway/plugins/p/state").respond(json=_state_response(source_id=7))
        router.post("/gateway/plugins/p/ingest").respond(
            json={"source_id": 7, "inserted": 1, "duplicates": 0, "errors": 0}
        )
        router.put("/gateway/plugins/p/cursor").respond(json=_state_response(source_id=7))
        with GatewayClient(BASE, "p", "outlook") as c:
            c.get_checkpoint()
            c.post_ingest_batch(
                [
                    {
                        "external_id": "e",
                        "occurred_at": "2026-01-01T00:00:00Z",
                        "payload": {},
                        "dedupe_keys": [],
                    }
                ]
            )
            c.put_checkpoint(0, {"x": 1})
        state_calls = [call for call in router.calls if "/state" in str(call.request.url)]
        assert len(state_calls) == 1


def test_post_ingest_strips_source_id_field() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read()))
        return httpx.Response(
            200, json={"source_id": 7, "inserted": 1, "duplicates": 0, "errors": 0}
        )

    with respx.mock(base_url=BASE) as router:
        router.post("/gateway/plugins/p/state").respond(json=_state_response(source_id=7))
        router.post("/gateway/plugins/p/ingest").mock(side_effect=handler)
        with GatewayClient(BASE, "p", "outlook") as c:
            c.post_ingest_batch(
                [
                    {
                        "source_id": 999,
                        "external_id": "e",
                        "occurred_at": "2026-01-01T00:00:00Z",
                        "payload": {"k": 1},
                        "dedupe_keys": [],
                    }
                ]
            )
        sent = captured[0]
        assert sent == {
            "records": [
                {
                    "external_id": "e",
                    "occurred_at": "2026-01-01T00:00:00Z",
                    "payload": {"k": 1},
                    "dedupe_keys": [],
                }
            ]
        }


def test_post_ingest_extracts_filtered_counter() -> None:
    with respx.mock(base_url=BASE) as router:
        router.post("/gateway/plugins/p/state").respond(json=_state_response(source_id=7))
        router.post("/gateway/plugins/p/ingest").respond(
            json={"source_id": 7, "inserted": 1, "duplicates": 0, "errors": 0, "filtered": 2}
        )
        with GatewayClient(BASE, "p", "outlook") as c:
            result = c.post_ingest_batch(
                [{"external_id": "e", "occurred_at": "2026-01-01T00:00:00Z", "payload": {}}]
            )
        assert result == {"inserted": 1, "duplicates": 0, "errors": 0, "filtered": 2}


def test_post_ingest_defaults_filtered_to_zero_when_absent() -> None:
    """Servidor viejo sin `filtered` en el response → default 0, no rompe."""
    with respx.mock(base_url=BASE) as router:
        router.post("/gateway/plugins/p/state").respond(json=_state_response(source_id=7))
        router.post("/gateway/plugins/p/ingest").respond(
            json={"source_id": 7, "inserted": 1, "duplicates": 0, "errors": 0}
        )
        with GatewayClient(BASE, "p", "outlook") as c:
            result = c.post_ingest_batch(
                [{"external_id": "e", "occurred_at": "2026-01-01T00:00:00Z", "payload": {}}]
            )
        assert result["filtered"] == 0


def test_put_checkpoint_sends_cursor() -> None:
    with respx.mock(base_url=BASE) as router:
        router.post("/gateway/plugins/p/state").respond(json=_state_response(source_id=7))
        put_route = router.put("/gateway/plugins/p/cursor").respond(
            json=_state_response(source_id=7, cursor={"x": 1})
        )
        with GatewayClient(BASE, "p", "outlook") as c:
            c.put_checkpoint(0, {"x": 1})
        assert put_route.called
        body = json.loads(router.calls[-1].request.read())
        assert body == {"cursor": {"x": 1}}


def test_bearer_auth_propagates() -> None:
    with respx.mock(base_url=BASE) as router:
        router.post("/gateway/plugins/p/state").respond(json=_state_response(source_id=7))
        with GatewayClient(BASE, "p", "outlook", api_token="tok") as c:
            c.get_checkpoint()
        assert router.calls[0].request.headers["authorization"] == "Bearer tok"


def test_get_sources_by_type_returns_empty() -> None:
    """No aplica al gateway — solo verifica que la firma MemexSink no rompa."""
    with GatewayClient(BASE, "p", "outlook") as c:
        assert c.get_sources_by_type("imap") == []


def test_ensure_source_is_disabled() -> None:
    import pytest

    from memex.ingestors.memex_server_client import MemexAPIError

    with GatewayClient(BASE, "p", "outlook") as c, pytest.raises(MemexAPIError):
        c.ensure_source("x", "imap")
