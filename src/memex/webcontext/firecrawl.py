"""FirecrawlProvider — el ÚNICO lugar que habla HTTP con Firecrawl (API v2).

Implementa el Protocol `WebContextProvider`. Aísla al vendor detrás de httpx (calca `geo/ors.py`:
POST + body JSON + auth Bearer en header + retry/backoff + logging redactado). Flujo en dos pasos
porque la calidad de Firecrawl depende de QUÉ URL se scrapea (verificado: el top-1 ciego de «Rappi»
caía en una subpágina de pagos):

  1. `POST /v2/search` → candidatos (url/title/description).
  2. `rank_candidates` los ordena (wikipedia/sitio-oficial arriba; subpáginas de pago/cuenta y
     perfiles de persona penalizados) y se scrapea hasta `scrape_attempts` con `POST /v2/scrape`
     (`formats:[{type:json, schema}]`, extracción LLM contra `EntityProfile`).

Devuelve el primer perfil COMPLETO; si ninguno lo es, el primero que al menos validó (best-effort).
La key (Bearer `fc-…`) va en el header, no en la URL → el logging de path+status no la filtra.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx
import tldextract

from memex.logging import get_logger
from memex.webcontext.client import (
    EntityKind,
    ProfileResult,
    WebContextConfigError,
    WebContextError,
    WebContextFormatError,
    WebContextNotFoundError,
    WebContextProviderError,
    WebContextQuotaError,
)
from memex.webcontext.config import WebContextConfig
from memex.webcontext.schema import EntityProfile, entity_profile_schema, validate_profile_data

_SEARCH_PATH = "/v2/search"
_SCRAPE_PATH = "/v2/scrape"
_BODY_PREVIEW_MAX = 500
_RAW_MAX = 2000
_DEFAULT_BASE_URL = "https://api.firecrawl.dev"

#: Extractor de la Public Suffix List EMBEBIDA y OFFLINE (sin red, determinista). Réplica del patrón
#: de `memex.modules.identidades.normalize` — NO se importa de ahí para no acoplar a identidades.
_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")

#: Palabras en el path que delatan una subpágina transaccional (no el perfil de la entidad).
_PAY_PATH_WORDS = (
    "cuenta",
    "account",
    "pricing",
    "precios",
    "checkout",
    "login",
    "signup",
    "help",
    "support",
)

#: Extracción del scrape: el schema ya viaja aparte; el prompt solo orienta.
_SCRAPE_PROMPT = (
    "Extraé un perfil corto y verificable de la entidad (organización o producto): name, one_liner "
    "(qué es en 1 frase), sector, country (origen/sede), founded (año), key_facts (2-4 hechos) y "
    "sources (URLs). Dejá vacío lo que no encuentres."
)


@dataclass(frozen=True)
class _Candidate:
    """Un resultado de `/v2/search`: la URL y su metadata (para rankear sin re-scrapear)."""

    url: str
    title: str = ""
    description: str = ""


def _registrable_domain(host: str) -> str:
    """eTLD+1 de un host (colapsa subdominios): `about.rappi.com` → `rappi.com`. PSL offline."""
    h = host.strip().lower()
    if not h:
        return ""
    return _TLD_EXTRACT(h).top_domain_under_public_suffix or h


def _alnum(text: str) -> str:
    """Letras/dígitos en minúscula: compara nombre vs dominio sin espacios ni puntuación."""
    return _NON_ALNUM_RE.sub("", text.lower())


def rank_candidates(candidates: Sequence[_Candidate], *, name: str) -> list[_Candidate]:
    """Ordena los candidatos por idoneidad para perfilar la entidad (determinista, testeable).

    +100 wikipedia · +80 dominio oficial (eTLD+1 contiene el nombre) · +60 linkedin.com/company ·
    +40 crunchbase / YC · -50 subpágina transaccional (pago/cuenta) · -1000 perfil de persona
    (linkedin.com/in). Desempate: orden original del buscador.
    """
    name_core = _alnum(name)
    scored: list[tuple[int, int, _Candidate]] = []
    for i, c in enumerate(candidates):
        parts = urlsplit(c.url)
        host = (parts.hostname or "").lower()
        path = parts.path.lower()
        reg_core = _alnum(_registrable_domain(host))
        score = 0
        if host == "wikipedia.org" or host.endswith(".wikipedia.org"):
            score += 100
        if name_core and reg_core and name_core in reg_core:
            score += 80
        if "linkedin.com" in host:
            if "/in/" in path:
                score -= 1000  # perfil de persona: nunca
            elif "/company" in path:
                score += 60
        if "crunchbase.com" in host or ("ycombinator.com" in host and "/companies" in path):
            score += 40
        if any(word in path for word in _PAY_PATH_WORDS):
            score -= 50
        scored.append((score, i, c))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [c for _, _, c in scored]


def _extract_results(data: dict[str, Any]) -> list[Any]:
    """Lista de resultados de `/v2/search`, tolerante a `data.web[]` o `data[]`."""
    payload = data.get("data")
    if isinstance(payload, dict):
        web = payload.get("web")
        if isinstance(web, list):
            return web
    if isinstance(payload, list):
        return payload
    return []


def _is_profile_complete(profile: EntityProfile) -> bool:
    """`one_liner` no vacío + ≥2 de {sector, country, founded, key_facts} — filtra perfiles
    escuálidos (la trampa de la subpágina de pagos) sin exigir todos los campos."""
    if not profile.one_liner.strip():
        return False
    filled = sum(
        bool(x) for x in (profile.sector, profile.country, profile.founded, profile.key_facts)
    )
    return filled >= 2


def _ensure_sources(profile: EntityProfile, url: str) -> EntityProfile:
    """Rellena `sources` con la URL scrapeada si el LLM no las pobló (procedencia siempre)."""
    if profile.sources:
        return profile
    return profile.model_copy(update={"sources": (url,)})


class FirecrawlProvider:
    """Cliente HTTP async para Firecrawl v2. Construir con `client` inyectado para tests (respx)."""

    name: ClassVar[str] = "firecrawl"

    def __init__(
        self, config: WebContextConfig, *, client: httpx.AsyncClient | None = None
    ) -> None:
        if config.api_key is None:
            raise WebContextConfigError("firecrawl requiere FIRECRAWL_API_KEY")
        self._config = config
        self._log = get_logger("memex.webcontext.firecrawl")
        headers = {
            "Authorization": f"Bearer {config.api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._client = client or httpx.AsyncClient(
            base_url=(config.base_url or _DEFAULT_BASE_URL).rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(config.timeout_s, connect=config.connect_timeout_s),
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> FirecrawlProvider:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def search(self, name: str, kind: EntityKind) -> ProfileResult:
        started = time.monotonic()
        candidates = await self._search(name, kind)
        if not candidates:
            raise WebContextNotFoundError(name)
        ranked = rank_candidates(candidates, name=name)

        first_valid: ProfileResult | None = None
        last_err: WebContextError | None = None
        for cand in ranked[: self._config.scrape_attempts]:
            try:
                extracted, raw = await self._scrape_extract(cand.url)
                profile = validate_profile_data(extracted, expected_kind=kind)
            except WebContextQuotaError:
                raise  # cuota agotada: no tiene sentido seguir probando URLs
            except (WebContextFormatError, WebContextProviderError) as e:
                last_err = e
                self._log.warning(
                    "webcontext.firecrawl.candidate_rejected", url=cand.url, error=str(e)[:150]
                )
                continue
            profile = _ensure_sources(profile, cand.url)
            result = ProfileResult(
                profile=profile,
                provider=self.name,
                latency_ms=int((time.monotonic() - started) * 1000),
                tokens=None,
                raw=raw[:_RAW_MAX],
            )
            if _is_profile_complete(profile):
                self._log.info(
                    "webcontext.firecrawl.search",
                    entity=name,
                    kind=kind,
                    url=cand.url,
                    latency_ms=result.latency_ms,
                )
                return result
            first_valid = first_valid or result  # best-effort si ninguno resulta completo

        if first_valid is not None:
            return first_valid
        if last_err is not None:
            raise last_err
        raise WebContextNotFoundError(name)

    async def _search(self, name: str, kind: EntityKind) -> list[_Candidate]:
        hint = "empresa" if kind == "organizacion" else "app producto"
        body = {"query": f"{name} {hint}", "limit": self._config.search_limit}
        data = await self._call(_SEARCH_PATH, body, op="search")
        candidates: list[_Candidate] = []
        for item in _extract_results(data):
            if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"]:
                candidates.append(
                    _Candidate(
                        url=item["url"],
                        title=str(item.get("title") or ""),
                        description=str(item.get("description") or ""),
                    )
                )
        return candidates

    async def _scrape_extract(self, url: str) -> tuple[Any, str]:
        """Scrapea `url` y extrae el perfil estructurado (`formats:[{type:json, schema}]`)."""
        body = {
            "url": url,
            "formats": [
                {"type": "json", "prompt": _SCRAPE_PROMPT, "schema": entity_profile_schema()}
            ],
        }
        data = await self._call(_SCRAPE_PATH, body, op="scrape")
        payload = data.get("data")
        extracted = payload.get("json") if isinstance(payload, dict) else None
        raw = "" if extracted is None else json.dumps(extracted, ensure_ascii=False)
        return extracted, raw

    async def _call(self, path: str, body: dict[str, Any], *, op: str) -> dict[str, Any]:
        started = time.monotonic()
        resp = await self._request(path, body=body)
        latency_ms = int((time.monotonic() - started) * 1000)
        self._log.info(
            "webcontext.firecrawl.request",
            op=op,
            path=path,
            http_status=resp.status_code,
            latency_ms=latency_ms,
        )
        data = resp.json()
        if not isinstance(data, dict):
            raise WebContextProviderError(
                resp.status_code, "respuesta no-objeto inesperada de firecrawl"
            )
        return data

    async def _request(self, path: str, *, body: dict[str, Any]) -> httpx.Response:
        """POST con retry 5xx/red + backoff; 429 → QuotaError; 4xx inmediato (calca ors.py)."""
        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                resp = await self._client.post(path, json=body)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                self._log.warning(
                    "webcontext.firecrawl.network_error", path=path, exc=str(e), attempt=attempt
                )
            else:
                if resp.status_code == 429:
                    raise WebContextQuotaError(
                        429,
                        "rate limit / quota exceeded",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                if 500 <= resp.status_code < 600:
                    last_exc = WebContextProviderError(
                        resp.status_code,
                        f"server error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                    self._log.warning(
                        "webcontext.firecrawl.retryable", status=resp.status_code, attempt=attempt
                    )
                elif 400 <= resp.status_code < 500:
                    raise WebContextProviderError(
                        resp.status_code,
                        f"client error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                else:
                    return resp

            if attempt < self._config.max_retries:
                await asyncio.sleep(self._config.backoff_base * (2**attempt))

        if isinstance(last_exc, WebContextProviderError):
            raise last_exc
        raise WebContextProviderError(0, f"network error on POST {path}") from last_exc
