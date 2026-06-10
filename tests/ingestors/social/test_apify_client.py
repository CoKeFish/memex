"""ApifyClient — run+poll, dataset paginado, abort por timeout, retries (respx, async)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from memex.ingestors.social import apify_client as apify_client_mod
from memex.ingestors.social.apify_client import ApifyClient, ApifyError, ApifyTimeoutError

BASE_URL = "https://api.apify.com"


def _client(**overrides: float) -> ApifyClient:
    kwargs: dict[str, float] = {
        "poll_interval_s": 0.001,
        "backoff_base": 0.001,
        "max_wait_s": 5.0,
        "usage_settle_s": 0.0,
    }
    kwargs.update(overrides)
    return ApifyClient("TKN", base_url=BASE_URL, **kwargs)  # type: ignore[arg-type]


def _run_json(
    run_id: str, status: str, dataset_id: str | None = None, **extra: object
) -> dict[str, Any]:
    data: dict[str, object] = {"id": run_id, "status": status, **extra}
    if dataset_id is not None:
        data["defaultDatasetId"] = dataset_id
    return {"data": data}


@pytest.mark.asyncio
async def test_run_actor_succeeds_immediately_returns_items_and_usage() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        run = router.post("/v2/acts/apify~instagram-scraper/runs").respond(
            json=_run_json("RUN1", "SUCCEEDED", "DS1", usageTotalUsd=0.012)
        )
        router.get("/v2/datasets/DS1/items").respond(json=[{"id": "p1"}, {"id": "p2"}])
        # Snapshot final: el costo asentado + desglose PPE + timestamps.
        router.get("/v2/actor-runs/RUN1").respond(
            json=_run_json(
                "RUN1",
                "SUCCEEDED",
                "DS1",
                usageTotalUsd=0.012,
                chargedEventCounts={"result": 2},
                startedAt="2026-06-09T10:00:00.000Z",
                finishedAt="2026-06-09T10:00:30.000Z",
            )
        )
        async with _client() as c:
            result = await c.run_actor("apify/instagram-scraper", {"resultsLimit": 2})
        assert run.called
        # waitForFinish va en el POST inicial (long-poll del lado de Apify).
        assert "waitForFinish" in dict(run.calls[0].request.url.params)
        assert [i["id"] for i in result.items] == ["p1", "p2"]
        assert result.usage_usd == 0.012
        assert result.run_id == "RUN1"
        assert result.charged_events == {"result": 2}
        assert result.started_at is not None and result.started_at.tzinfo is not None
        assert result.finished_at is not None


@pytest.mark.asyncio
async def test_run_actor_polls_until_terminal() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post("/v2/acts/apify~instagram-scraper/runs").respond(
            json=_run_json("RUN9", "RUNNING", "DS9")
        )
        router.get("/v2/actor-runs/RUN9").mock(
            side_effect=[
                httpx.Response(200, json=_run_json("RUN9", "RUNNING")),
                httpx.Response(200, json=_run_json("RUN9", "SUCCEEDED", "DS9", usageTotalUsd=0.5)),
                # Snapshot final post-items.
                httpx.Response(200, json=_run_json("RUN9", "SUCCEEDED", "DS9", usageTotalUsd=0.5)),
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
            json=_run_json("RUNF", "FAILED", "DSF")
        )
        async with _client() as c:
            with pytest.raises(ApifyError):
                await c.run_actor("apify/instagram-scraper", {})


@pytest.mark.asyncio
async def test_actor_id_slash_becomes_tilde_in_url() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post("/v2/acts/apidojo~tweet-scraper/runs").respond(
            json=_run_json("R", "SUCCEEDED", "D", usageTotalUsd=0.001)
        )
        router.get("/v2/datasets/D/items").respond(json=[])
        router.get("/v2/actor-runs/R").respond(
            json=_run_json("R", "SUCCEEDED", "D", usageTotalUsd=0.001)
        )
        async with _client() as c:
            await c.run_actor("apidojo/tweet-scraper", {})
        assert route.called


@pytest.mark.asyncio
async def test_run_post_includes_max_total_charge_when_set() -> None:
    """El tope de gasto (PPE) viaja como query param del POST; sin tope, no viaja."""
    with respx.mock(base_url=BASE_URL) as router:
        run = router.post("/v2/acts/apify~instagram-scraper/runs").respond(
            json=_run_json("RC", "SUCCEEDED", "DC", usageTotalUsd=0.1)
        )
        router.get("/v2/datasets/DC/items").respond(json=[])
        router.get("/v2/actor-runs/RC").respond(
            json=_run_json("RC", "SUCCEEDED", "DC", usageTotalUsd=0.1)
        )
        async with _client() as c:
            await c.run_actor("apify/instagram-scraper", {}, max_total_charge_usd=1.5)
        params = dict(run.calls[0].request.url.params)
        assert params["maxTotalChargeUsd"] == "1.5"


@pytest.mark.asyncio
async def test_dataset_items_paginated_and_capped_by_max_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La descarga pagina con offset/limit y corta en max_items (Apify devuelve TODO sin limit)."""
    monkeypatch.setattr(apify_client_mod, "_DATASET_PAGE_SIZE", 2)
    with respx.mock(base_url=BASE_URL) as router:
        router.post("/v2/acts/apify~instagram-scraper/runs").respond(
            json=_run_json("RP", "SUCCEEDED", "DP", usageTotalUsd=0.2)
        )
        router.get("/v2/actor-runs/RP").respond(
            json=_run_json("RP", "SUCCEEDED", "DP", usageTotalUsd=0.2)
        )
        page1 = router.get("/v2/datasets/DP/items", params={"offset": "0", "limit": "2"}).respond(
            json=[{"id": "a"}, {"id": "b"}]
        )
        page2 = router.get("/v2/datasets/DP/items", params={"offset": "2", "limit": "1"}).respond(
            json=[{"id": "c"}]
        )
        async with _client() as c:
            result = await c.run_actor("apify/instagram-scraper", {}, max_items=3)
        assert page1.called and page2.called
        assert [i["id"] for i in result.items] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_dataset_short_page_stops_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apify_client_mod, "_DATASET_PAGE_SIZE", 2)
    with respx.mock(base_url=BASE_URL) as router:
        router.post("/v2/acts/apify~instagram-scraper/runs").respond(
            json=_run_json("RS", "SUCCEEDED", "DST", usageTotalUsd=0.2)
        )
        router.get("/v2/actor-runs/RS").respond(
            json=_run_json("RS", "SUCCEEDED", "DST", usageTotalUsd=0.2)
        )
        router.get("/v2/datasets/DST/items", params={"offset": "0", "limit": "2"}).respond(
            json=[{"id": "a"}, {"id": "b"}]
        )
        short = router.get("/v2/datasets/DST/items", params={"offset": "2", "limit": "2"}).respond(
            json=[{"id": "c"}]
        )
        async with _client() as c:
            result = await c.run_actor("apify/instagram-scraper", {}, max_items=10)
        assert short.called
        assert [i["id"] for i in result.items] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_timeout_aborts_run_and_reports_partial_usage() -> None:
    """Al vencer max_wait_s: abort (el run seguiría cobrando) + costo parcial en la excepción."""
    with respx.mock(base_url=BASE_URL) as router:
        router.post("/v2/acts/apify~instagram-scraper/runs").respond(
            json=_run_json("RUNT", "RUNNING", "DT")
        )
        router.get("/v2/actor-runs/RUNT").respond(
            json=_run_json(
                "RUNT", "RUNNING", "DT", usageTotalUsd=0.02, chargedEventCounts={"result": 7}
            )
        )
        abort = router.post("/v2/actor-runs/RUNT/abort").respond(json=_run_json("RUNT", "ABORTING"))
        async with _client(max_wait_s=0.01) as c:
            with pytest.raises(ApifyTimeoutError) as exc:
                await c.run_actor("apify/instagram-scraper", {})
        assert abort.called
        assert exc.value.run_id == "RUNT"
        assert exc.value.usage_usd == 0.02
        assert exc.value.charged_events == {"result": 7}


@pytest.mark.asyncio
async def test_usage_settles_on_single_retry() -> None:
    """usageTotalUsd se asienta tarde: la primera lectura viene sin costo, el retry lo trae."""
    with respx.mock(base_url=BASE_URL) as router:
        router.post("/v2/acts/apify~instagram-scraper/runs").respond(
            json=_run_json("RU", "SUCCEEDED", "DU")
        )
        router.get("/v2/datasets/DU/items").respond(json=[{"id": "a"}])
        router.get("/v2/actor-runs/RU").mock(
            side_effect=[
                httpx.Response(200, json=_run_json("RU", "SUCCEEDED", "DU")),
                httpx.Response(
                    200,
                    json=_run_json(
                        "RU",
                        "SUCCEEDED",
                        "DU",
                        usageTotalUsd=0.7,
                        chargedEventCounts={"result": 1},
                    ),
                ),
            ]
        )
        async with _client() as c:
            result = await c.run_actor("apify/instagram-scraper", {})
        assert result.usage_usd == 0.7
        assert result.charged_events == {"result": 1}


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
