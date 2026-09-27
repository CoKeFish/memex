"""FirecrawlProvider con respx (sin red): search + scrape json, ranking de URL, retries."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from memex.webcontext import (
    WebContextConfig,
    WebContextFormatError,
    WebContextNotFoundError,
    WebContextProvider,
    WebContextProviderError,
    WebContextQuotaError,
)
from memex.webcontext.firecrawl import FirecrawlProvider, _Candidate, rank_candidates

BASE = "https://api.firecrawl.dev"
SEARCH = "/v2/search"
SCRAPE = "/v2/scrape"

_GOOD: dict[str, Any] = {
    "name": "Rappi",
    "kind": "organizacion",
    "one_liner": "multinacional colombiana",
    "sector": "tech",
    "country": "Colombia",
    "founded": "2015",
    "key_facts": ["unicornio"],
    "sources": [],
}
_THIN: dict[str, Any] = {
    "name": "RappiCuenta",
    "kind": "producto",
    "one_liner": "",
    "sector": "",
    "country": "",
    "founded": "",
    "key_facts": [],
    "sources": [],
}


def _config() -> WebContextConfig:
    return WebContextConfig(
        provider="firecrawl", api_key=SecretStr("fc-x"), base_url=BASE, backoff_base=0.001
    )


def _search_body(*urls: str) -> dict[str, Any]:
    web = [{"url": u, "title": u, "description": ""} for u in urls]
    return {"success": True, "data": {"web": web}}


def _scrape_by_url(mapping: dict[str, dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    """side_effect de respx: devuelve el perfil según un substring de la URL scrapeada."""

    def _responder(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["url"]
        for key, profile in mapping.items():
            if key in url:
                return httpx.Response(200, json={"success": True, "data": {"json": profile}})
        return httpx.Response(200, json={"success": True, "data": {"json": None}})

    return _responder


def test_satisfies_protocol() -> None:
    assert isinstance(FirecrawlProvider(_config()), WebContextProvider)


def test_rank_candidates_orders() -> None:
    cands = [
        _Candidate(url="https://www.rappipay.co/empresas/"),  # subpágina (rappi en dominio: +80)
        _Candidate(url="https://www.linkedin.com/in/alguien"),  # persona: -1000
        _Candidate(url="https://es.wikipedia.org/wiki/Rappi"),  # +100
        _Candidate(url="https://www.linkedin.com/company/rappi"),  # +60 (+80 no: dominio linkedin)
    ]
    ranked = rank_candidates(cands, name="Rappi")
    assert "wikipedia.org" in ranked[0].url  # gana wikipedia
    assert "/in/" in ranked[-1].url  # persona al fondo


@pytest.mark.asyncio
async def test_rappi_picks_canonical_not_payments() -> None:
    """Caso discriminante: top-1 ciego = subpágina de pagos; el ranking elige wikipedia."""
    with respx.mock(base_url=BASE) as router:
        router.post(SEARCH).respond(
            json=_search_body(
                "https://www.rappipay.co/empresas/", "https://es.wikipedia.org/wiki/Rappi"
            )
        )
        scrape = router.post(SCRAPE).mock(
            side_effect=_scrape_by_url({"wikipedia": _GOOD, "rappipay": _THIN})
        )
        async with FirecrawlProvider(_config()) as p:
            result = await p.search("Rappi", "organizacion")
    assert result.profile.country == "Colombia"  # vino de wikipedia (el bueno)
    assert scrape.call_count == 1  # wikipedia completo → no scrapea el segundo
    first_url = json.loads(scrape.calls[0].request.content)["url"]
    assert "wikipedia" in first_url


@pytest.mark.asyncio
async def test_incomplete_first_then_second_complete() -> None:
    """El mejor-rankeado (wikipedia) sale incompleto → scrapea el 2º (oficial) y devuelve ese."""
    with respx.mock(base_url=BASE) as router:
        router.post(SEARCH).respond(
            json=_search_body("https://es.wikipedia.org/wiki/Rappi", "https://rappi.com/about")
        )
        scrape = router.post(SCRAPE).mock(
            side_effect=_scrape_by_url({"wikipedia": _THIN, "rappi.com": _GOOD})
        )
        async with FirecrawlProvider(_config()) as p:
            result = await p.search("Rappi", "organizacion")
    assert result.profile.country == "Colombia"
    assert scrape.call_count == 2  # probó wikipedia (incompleto) y luego oficial


@pytest.mark.asyncio
async def test_sources_filled_with_scraped_url() -> None:
    with respx.mock(base_url=BASE) as router:
        router.post(SEARCH).respond(json=_search_body("https://es.wikipedia.org/wiki/Rappi"))
        router.post(SCRAPE).mock(side_effect=_scrape_by_url({"wikipedia": _GOOD}))
        async with FirecrawlProvider(_config()) as p:
            result = await p.search("Rappi", "organizacion")
    assert result.profile.sources == ("https://es.wikipedia.org/wiki/Rappi",)


@pytest.mark.asyncio
async def test_search_empty_not_found() -> None:
    with respx.mock(base_url=BASE) as router:
        router.post(SEARCH).respond(json={"success": True, "data": {"web": []}})
        async with FirecrawlProvider(_config()) as p:
            with pytest.raises(WebContextNotFoundError):
                await p.search("Inexistente", "organizacion")


@pytest.mark.asyncio
async def test_all_candidates_invalid_raises_format() -> None:
    with respx.mock(base_url=BASE) as router:
        router.post(SEARCH).respond(json=_search_body("https://es.wikipedia.org/wiki/Rappi"))
        router.post(SCRAPE).mock(side_effect=_scrape_by_url({}))  # data.json=None → no valida
        async with FirecrawlProvider(_config()) as p:
            with pytest.raises(WebContextFormatError):
                await p.search("Rappi", "organizacion")


@pytest.mark.asyncio
async def test_bearer_header_present() -> None:
    with respx.mock(base_url=BASE) as router:
        search = router.post(SEARCH).respond(
            json=_search_body("https://es.wikipedia.org/wiki/Rappi")
        )
        router.post(SCRAPE).mock(side_effect=_scrape_by_url({"wikipedia": _GOOD}))
        async with FirecrawlProvider(_config()) as p:
            await p.search("Rappi", "organizacion")
    assert search.calls[0].request.headers["authorization"] == "Bearer fc-x"


@pytest.mark.asyncio
async def test_search_429_quota() -> None:
    with respx.mock(base_url=BASE) as router:
        router.post(SEARCH).respond(429)
        async with FirecrawlProvider(_config()) as p:
            with pytest.raises(WebContextQuotaError):
                await p.search("Rappi", "organizacion")


@pytest.mark.asyncio
async def test_search_5xx_retries_then_fails() -> None:
    with respx.mock(base_url=BASE) as router:
        route = router.post(SEARCH)
        route.side_effect = [httpx.Response(500) for _ in range(4)]
        async with FirecrawlProvider(_config()) as p:
            with pytest.raises(WebContextProviderError):
                await p.search("Rappi", "organizacion")
    assert route.call_count == 4  # max_retries(3) + 1


@pytest.mark.asyncio
async def test_search_4xx_immediate() -> None:
    with respx.mock(base_url=BASE) as router:
        route = router.post(SEARCH).respond(404)
        async with FirecrawlProvider(_config()) as p:
            with pytest.raises(WebContextProviderError):
                await p.search("Rappi", "organizacion")
    assert route.call_count == 1
