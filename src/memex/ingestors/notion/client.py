"""Cliente mínimo de la API de Notion (httpx sync): páginas, bloques, búsqueda, usuario.

Respeta el límite de ~3 req/s (espera entre llamadas), reintenta 429 (`Retry-After`) y 5xx con
backoff corto, y pagina (`start_cursor`/`has_more`). Errores HTTP → `NotionApiError` con el código
(401/403 = token inválido o sin acceso; 404 = página no compartida o borrada).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import httpx

BASE_URL = "https://api.notion.com/v1"
_RETRIES = 3


class NotionApiError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"notion {status_code}: {message}")
        self.status_code = status_code


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        version: str,
        requests_per_second: float = 3.0,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": version,
            "Content-Type": "application/json",
        }
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._last_call = 0.0
        self._client = httpx.Client(
            base_url=BASE_URL, headers=self._headers, timeout=timeout_s, transport=transport
        )
        self.calls = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> NotionClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ transporte
    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)

    def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> Any:
        attempt = 0
        while True:
            self._throttle()
            self._last_call = time.monotonic()
            self.calls += 1
            try:
                resp = self._client.request(method, path, json=json)
            except httpx.HTTPError as e:
                attempt += 1
                if attempt > _RETRIES:
                    raise NotionApiError(0, f"red: {e}") from e
                time.sleep(0.5 * attempt)
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                attempt += 1
                if attempt > _RETRIES:
                    raise NotionApiError(resp.status_code, _message(resp))
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 0.5 * attempt
                except ValueError:
                    delay = 0.5 * attempt
                time.sleep(min(delay, 10.0))
                continue
            if resp.status_code >= 400:
                raise NotionApiError(resp.status_code, _message(resp))
            return resp.json()

    def _paginate(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Iterator[Any]:
        cursor: str | None = None
        while True:
            if method == "GET":
                qs = "?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
                data = self._request("GET", path + qs)
            else:
                payload = dict(body or {})
                payload["page_size"] = 100
                if cursor:
                    payload["start_cursor"] = cursor
                data = self._request("POST", path, json=payload)
            yield from data.get("results", [])
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")
            if not cursor:
                return

    # ------------------------------------------------------------------ recursos
    def me(self) -> dict[str, Any]:
        data: dict[str, Any] = self._request("GET", "/users/me")
        return data

    def page(self, page_id: str) -> dict[str, Any]:
        data: dict[str, Any] = self._request("GET", f"/pages/{page_id}")
        return data

    def database(self, database_id: str) -> dict[str, Any]:
        data: dict[str, Any] = self._request("GET", f"/databases/{database_id}")
        return data

    def block(self, block_id: str) -> dict[str, Any]:
        data: dict[str, Any] = self._request("GET", f"/blocks/{block_id}")
        return data

    def block_children(self, block_id: str) -> Iterator[dict[str, Any]]:
        return self._paginate("GET", f"/blocks/{block_id}/children")

    def search_pages_by_edit(self) -> Iterator[dict[str, Any]]:
        """Todas las páginas visibles para la integración, de la más reciente a la más antigua
        edición. El caller corta cuando pasa su marca de agua."""
        return self._paginate(
            "POST",
            "/search",
            {
                "filter": {"property": "object", "value": "page"},
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            },
        )


def _message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return str(data.get("message") or data.get("code") or resp.text[:200])
    except ValueError:
        return resp.text[:200]
