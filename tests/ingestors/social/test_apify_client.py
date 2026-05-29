"""ApifyClient — run+poll flow, dataset fetch, retries, error mapping (respx)."""

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


def test_run_actor_succeeds_immediately_returns_items_and_usage() -> None:
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
        with _client() as c:
            result = c.run_actor("apify/instagram-scraper", {"resultsLimit": 2})
        assert run.called
        assert [i["id"] for i in result.items] == ["p1", "p2"]
        assert result.usage_usd == 0.012
        assert result.run_id == "RUN1"


def test_run_actor_polls_until_terminal() -> None:
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
        with _client() as c:
            result = c.run_actor("apify/instagram-scraper", {})
        assert result.usage_usd == 0.5
        assert [i["id"] for i in result.items] == ["x"]


def test_run_actor_raises_when_run_fails() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post("/v2/acts/apify~instagram-scraper/runs").respond(
            json={"data": {"id": "RUNF", "status": "FAILED", "defaultDatasetId": "DSF"}}
        )
        with _client() as c, pytest.raises(ApifyError):
            c.run_actor("apify/instagram-scraper", {})


def test_actor_id_slash_becomes_tilde_in_url() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post("/v2/acts/apidojo~tweet-scraper/runs").respond(
            json={"data": {"id": "R", "status": "SUCCEEDED", "defaultDatasetId": "D"}}
        )
        router.get("/v2/datasets/D/items").respond(json=[])
        with _client() as c:
            c.run_actor("apidojo/tweet-scraper", {})
        assert route.called


def test_bearer_token_attached_in_header_not_url() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/v2/users/me").respond(json={"data": {"username": "tester"}})
        with _client() as c:
            c.whoami()
        req = router.calls[0].request
        assert req.headers["authorization"] == "Bearer TKN"
        assert "TKN" not in str(req.url)


def test_whoami_unwraps_data() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/v2/users/me").respond(json={"data": {"username": "tester", "id": "u1"}})
        with _client() as c:
            me = c.whoami()
        assert me["username"] == "tester"


def test_4xx_raises_immediately() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/v2/users/me").respond(401)
        with _client() as c, pytest.raises(ApifyError) as exc:
            c.whoami()
        assert exc.value.status_code == 401
        assert router.calls.call_count == 1


def test_5xx_retries_then_succeeds() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/v2/users/me").mock(
            side_effect=[
                httpx.Response(503, text="busy"),
                httpx.Response(503, text="busy"),
                httpx.Response(200, json={"data": {"username": "ok"}}),
            ]
        )
        with _client() as c:
            me = c.whoami()
        assert me["username"] == "ok"
        assert router.calls.call_count == 3


def test_run_start_post_not_retried_on_5xx() -> None:
    """Arrancar un run NO es idempotente: un 5xx no se reintenta (evita lanzar un
    segundo run pago) → ApifyError inmediato, UNA sola llamada."""
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post("/v2/acts/apify~instagram-scraper/runs").respond(503, text="busy")
        with _client() as c, pytest.raises(ApifyError):
            c.run_actor("apify/instagram-scraper", {})
        assert route.call_count == 1


def test_run_start_post_not_retried_on_network_error() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post("/v2/acts/apify~instagram-scraper/runs").mock(
            side_effect=httpx.ConnectError("boom")
        )
        with _client() as c, pytest.raises(ApifyError):
            c.run_actor("apify/instagram-scraper", {})
        assert route.call_count == 1
