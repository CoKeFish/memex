"""BridgeClient — cliente HTTP para `/bridge/plugins/<name>/*` de memex.

Subclasea `MemexClient` para reusar transport (httpx, retries, bearer auth)
pero cambia las URLs hacia el namespace del bridge. Un BridgeClient se
construye **por plugin** — el `plugin_name` se fija en el constructor y se
inyecta en cada URL.

Sigue siendo un `MemexSink` (estructuralmente compatible con el Protocol),
así que se le pasa al `run_ingestor` reusable sin cambiar el runner. La
ilusión es transparente: el runner llama `get_checkpoint(source_id)` y
`post_ingest_batch(records)` igual que con MemexClient — internamente,
BridgeClient ignora el `source_id` de los args (lo resuelve solo vía el
endpoint `/state` por primera vez y lo cachea) y elimina la columna
`source_id` de cada record antes de enviarlo (el bridge la rellena).
"""

from __future__ import annotations

from typing import Any

import httpx

from memex.ingestors.http_client import MemexAPIError, MemexClient


class BridgeClient(MemexClient):
    """Cliente para el surface bridge — uno por plugin."""

    def __init__(
        self,
        base_url: str,
        plugin_name: str,
        source_type: str,
        api_token: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ) -> None:
        super().__init__(
            base_url,
            api_token,
            client=client,
            timeout=timeout,
            max_retries=max_retries,
            backoff_base=backoff_base,
        )
        self.plugin_name = plugin_name
        self.source_type = source_type
        self._source_id: int | None = None
        self._initial_cursor: dict[str, Any] | None = None
        self._state_loaded = False

    # --- override para que el runner no toque /sources/* ni /ingest/batch ---

    def get_sources_by_type(self, source_type: str) -> list[dict[str, Any]]:
        """No aplica al bridge — devuelve lista vacía para satisfacer el Protocol."""
        return []

    def ensure_source(
        self,
        name: str,
        source_type: str,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """No usar — el bridge resuelve identidad implícitamente vía /state."""
        raise MemexAPIError(
            400,
            "BridgeClient no expone /sources/ensure; usar /bridge/plugins/<name>/state",
        )

    def get_checkpoint(self, source_id: int | None = None) -> dict[str, Any] | None:
        """Carga estado vía POST /bridge/plugins/{name}/state.

        Ignora `source_id` (el bridge lo resuelve desde el URL). Cachea el
        `source_id` real (asignado/encontrado por el servidor) para que
        callers puedan inspeccionarlo si lo necesitan.
        """
        self._load_state()
        cur = self._initial_cursor
        return cur if isinstance(cur, dict) else None

    def put_checkpoint(self, source_id: int, cursor: dict[str, Any]) -> None:
        """Persiste el cursor vía PUT /bridge/plugins/{name}/cursor."""
        if not self._state_loaded:
            self._load_state()
        self._request(
            "PUT",
            f"/bridge/plugins/{self.plugin_name}/cursor",
            json={"cursor": cursor},
        )

    def post_ingest_batch(self, records: list[dict[str, Any]]) -> dict[str, int]:
        """Envía records al endpoint del bridge — strip de `source_id` interno."""
        if not self._state_loaded:
            self._load_state()
        stripped = [{k: v for k, v in r.items() if k != "source_id"} for r in records]
        resp = self._request(
            "POST",
            f"/bridge/plugins/{self.plugin_name}/ingest",
            json={"records": stripped},
        )
        data = resp.json()
        return {
            "inserted": int(data.get("inserted", 0)),
            "duplicates": int(data.get("duplicates", 0)),
            "errors": int(data.get("errors", 0)),
        }

    # --- introspección útil para callers (ej. memex_local.run) ---

    @property
    def resolved_source_id(self) -> int | None:
        """`source_id` resuelto desde /state (None si todavía no se llamó)."""
        return self._source_id

    # --- internals ---

    def _load_state(self) -> None:
        resp = self._request(
            "POST",
            f"/bridge/plugins/{self.plugin_name}/state",
            json={"source_type": self.source_type},
        )
        data = resp.json()
        self._source_id = int(data["source_id"])
        cursor = data.get("cursor")
        self._initial_cursor = cursor if isinstance(cursor, dict) else None
        self._state_loaded = True
