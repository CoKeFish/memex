"""ApifyClient — run+poll flow, dataset fetch, retries, error mapping (respx, async)."""

from __future__ import annotations

import httpx
import pytest
import respx

from memex.ingestors.social.apify_client import ApifyClient, ApifyError

BASE_URL = "https://api.apify.com"


def _client() -> ApifyClient:
    return ApifyClient(
        "TKN",
        base_url=BASE_URL,
        poll_interval_s=0.001,
        backoff_base=0.001,
        max_wait_s=5.0,
    )


@pytest.mark.asyncio
async def test_run_actor_succeeds_immediately_returns_items_and_usage() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        run = router.post("/v2/acts/apify~instagram-scraper/runs").respond(
            json={
                "data": {
                    "id": "RUN1",
                    "status": "SUCCEEDED",
                    "defaultDatasetId": "DS1",
                    "usageTotalUsd": 0.012,
                }
            }
        )
        router.get("/v2/datasets/DS1/items").respond(json=[{"id": "p1"}, {"id": "p2"}])
        async with _client() as c:
            result = await c.run_actor("apify/instagram-scraper", {"resultsLimit": 2})
        assert run.called
        assert [i["id"] for i in result.items] == ["p1", "p2"]
        assert result.usage_usd == 0.012
        assert result.run_id == "RUN1"


@pytest.mark.asyncio
async def test_run_actor_polls_until_terminal() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post("/v2/acts/apify~instagram-scraper/runs").respond(
            json={"data": {"id": "RUN9", "status": "RUNNING", "defaultDatasetId": "DS9"}}
        )
        router.get("/v2/actor-runs/RUN9").mock(
            side_effect=[
                httpx.Response(200, json={"data": {"id": "RUN9", "status": "RUNNING"}}),
                httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": "RUN9",
                            "status": "SUCCEEDED",
                            "defaultDatasetId": "DS9",
                            "usageTotalUsd": 0.5,
                        }
                    },
                ),
            ]
        )
        router.get("/v2/datasets/DS9/items").respond(json=[{"id": "x"}])
        async with _client() as c:
            result = await c.run_actor("apify/instagram-scraper", {})
        assert result.usage_usd == 0.5
        assert [i["id"] for i in result.items] == ["x"]


@pytest.mark.asyncio
async def test_run_actor_raises_when_run_fails() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post("/v2/acts/apify~instagram-scraper/runs").respond(
            json={"data": {"id": "RUNF", "status": "FAILED", "defaultDatasetId": "DSF"}}
        )
        async with _client() as c:
            with pytest.raises(ApifyError):
                await c.run_actor("apify/instagram-scraper", {})


@pytest.mark.asyncio
async def test_actor_id_slash_becomes_tilde_in_url() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post("/v2/acts/apidojo~tweet-scraper/runs").respond(
            json={"data": {"id": "R", "status": "SUCCEEDED", "defaultDatasetId": "D"}}
        )
        router.get("/v2/datasets/D/items").respond(json=[])
        async with _client() as c:
            await c.run_actor("apidojo/tweet-scraper", {})
        assert route.called


@pytest.mark.asyncio
async def test_bearer_token_attached_in_header_not_url() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/v2/users/me").respond(json={"data": {"username": "tester"}})
        async with _client() as c:
            await c.whoami()
        req = router.calls[0].request
        assert req.headers["authorization"] == "Bearer TKN"
        assert "TKN" not in str(req.url)


@pytest.mark.asyncio
async def test_whoami_unwraps_data() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/v2/users/me").respond(json={"data": {"username": "tester", "id": "u1"}})
        async with _client() as c:
            me = await c.whoami()
        assert me["username"] == "tester"


@pytest.mark.asyncio
async def test_4xx_raises_immediately() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/v2/users/me").respond(401)
        async with _client() as c:
            with pytest.raises(ApifyError) as exc:
                await c.whoami()
        assert exc.value.status_code == 401
        assert router.calls.call_count == 1


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/v2/users/me").mock(
            side_effect=[
                httpx.Response(503, text="busy"),
                httpx.Response(503, text="busy"),
                httpx.Response(200, json={"data": {"username": "ok"}}),
            ]
        )
        async with _client() as c:
            me = await c.whoami()
        assert me["username"] == "ok"
        assert router.calls.call_count == 3


@pytest.mark.asyncio
async def test_run_start_post_not_retried_on_5xx() -> None:
    """Arrancar un run NO es idempotente: un 5xx no se reintenta (evita lanzar un
    segundo run pago) → ApifyError inmediato, UNA sola llamada."""
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post("/v2/acts/apify~instagram-scraper/runs").respond(503, text="busy")
        async with _client() as c:
            with pytest.raises(ApifyError):
                await c.run_actor("apify/instagram-scraper", {})
        assert route.call_count == 1


@pytest.mark.asyncio
async def test_run_start_post_not_retried_on_network_error() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post("/v2/acts/apify~instagram-scraper/runs").mock(
            side_effect=httpx.ConnectError("boom")
        )
        async with _client() as c:
            with pytest.raises(ApifyError):
                await c.run_actor("apify/instagram-scraper", {})
        assert route.call_count == 1
