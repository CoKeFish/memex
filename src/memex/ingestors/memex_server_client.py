from __future__ import annotations

import time
from typing import Any

import httpx

from memex.logging import get_logger


class MemexAPIError(Exception):
    """Raised when memex API returns an error after exhausting retries (or on 4xx)."""

    def __init__(self, status_code: int, message: str, body: str | None = None):
        super().__init__(f"memex API error {status_code}: {message}")
        self.status_code = status_code
        self.body = body


class MemexServerClient:
    """HTTP client for the memex server API.

    The only channel by which ingestors (server-side or local) communicate with
    memex (ADR-001). Retries 5xx and network errors with exponential backoff;
    4xx are non-retryable and surface immediately as MemexAPIError.
    """

    def __init__(
        self,
        base_url: str,
        api_token: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        ingest_path: str = "/ingest/batch",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._ingest_path = ingest_path
        self._log = get_logger("memex.ingestors.memex_server_client")

        headers: dict[str, str] = {}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"

        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout),
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> MemexServerClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def get_sources_by_type(self, source_type: str) -> list[dict[str, Any]]:
        data = self._request("GET", "/sources").json()
        return [s for s in data if s.get("type") == source_type and s.get("enabled", True)]

    def ensure_source(
        self,
        name: str,
        source_type: str,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Get-or-create idempotente. Devuelve la fila completa de la source.

        Útil para clientes que conocen el `name` lógico pero no el id en
        memex. El servidor decide si existía o se acaba de crear.
        """
        body = {"name": name, "type": source_type, "config": config or {}}
        result: dict[str, Any] = self._request("POST", "/sources/ensure", json=body).json()
        return result

    def add_social_account(
        self, source_id: int, handle: str, *, priority: bool = False
    ) -> dict[str, Any]:
        """POST /sources/{id}/social/accounts — agrega una cuenta seguida. Devuelve la source."""
        resp = self._request(
            "POST",
            f"/sources/{source_id}/social/accounts",
            json={"handle": handle, "priority": priority},
        )
        result: dict[str, Any] = resp.json()
        return result

    def remove_social_account(self, source_id: int, handle: str) -> dict[str, Any]:
        """DELETE /sources/{id}/social/accounts/{handle} — quita una cuenta seguida."""
        resp = self._request("DELETE", f"/sources/{source_id}/social/accounts/{handle}")
        result: dict[str, Any] = resp.json()
        return result

    def get_checkpoint(self, source_id: int) -> dict[str, Any] | None:
        data = self._request("GET", f"/sources/{source_id}/checkpoint").json()
        cursor = data.get("cursor")
        return cursor if isinstance(cursor, dict) else None

    def put_checkpoint(self, source_id: int, cursor: dict[str, Any]) -> None:
        self._request("PUT", f"/sources/{source_id}/checkpoint", json={"cursor": cursor})

    def post_ingest_batch(self, records: list[dict[str, Any]]) -> dict[str, int]:
        resp = self._request("POST", self._ingest_path, json={"records": records})
        result: dict[str, int] = resp.json()
        return result

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.request(method, path, json=json)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                self._log.warning(
                    "memex_request_network_error",
                    method=method,
                    path=path,
                    exc=str(e),
                    attempt=attempt,
                )
            else:
                if 500 <= resp.status_code < 600:
                    last_exc = MemexAPIError(
                        resp.status_code,
                        f"server error {resp.status_code}",
                        body=resp.text[:500] if resp.text else None,
                    )
                    self._log.warning(
                        "memex_request_5xx",
                        method=method,
                        path=path,
                        status=resp.status_code,
                        attempt=attempt,
                    )
                elif 400 <= resp.status_code < 500:
                    raise MemexAPIError(
                        resp.status_code,
                        f"client error {resp.status_code}",
                        body=resp.text[:500] if resp.text else None,
                    )
                else:
                    return resp

            if attempt < self.max_retries:
                sleep_s = self.backoff_base * (2**attempt)
                time.sleep(sleep_s)

        assert last_exc is not None
        raise last_exc
