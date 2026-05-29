"""ApifyClient — el ÚNICO lugar que habla HTTP con Apify.

Aísla al vendor: `source.py` / `parser.py` consumen dicts ya normalizados, nunca
URLs ni shapes de Apify. Si mañana cambiamos de provider (HikerAPI, EnsembleData)
solo cambia este módulo — el contrato `Source` no se entera.

Usa httpx **síncrono** (ya es dependencia del proyecto) — NO el SDK `apify-client`.
El patrón de retry/backoff está espejado de `MemexServerClient._request`: reintenta
5xx + errores de red con backoff exponencial; 4xx levanta `ApifyError` inmediato.

Flujo **run async + poll** (NO `run-sync-get-dataset-items`): los actores de scraping
pueden tardar más que el límite duro de `run-sync` y, sobre todo, `run-sync` no
devuelve el costo del run. Lanzamos el run, poleamos hasta estado terminal (leyendo
`usageTotalUsd` para el logging de costo) y bajamos los items del dataset.

ADR-001: este módulo vive en `ingestors/` — solo importa `httpx` + `memex.logging`,
nunca internals de memex (db/api/inbox/checkpoint).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from memex.logging import get_logger

_APIFY_BASE_URL = "https://api.apify.com"

# Estados de un actor-run de Apify. Mientras esté en uno de estos, seguimos
# poleando; cualquier otro es terminal. Solo `SUCCEEDED` es éxito.
_RUNNING_STATUSES = frozenset({"READY", "RUNNING", "CREATED"})
_SUCCESS_STATUS = "SUCCEEDED"


class ApifyError(Exception):
    """Raised cuando Apify devuelve un error (4xx, 5xx tras agotar retries) o un
    run termina en estado no exitoso.

    `status_code` es el HTTP status cuando aplica, o 0 para errores lógicos
    (ej. run terminó FAILED, dataset vacío de metadatos).
    """

    def __init__(self, status_code: int, message: str, body: str | None = None) -> None:
        super().__init__(f"apify error {status_code}: {message}")
        self.status_code = status_code
        self.body = body


@dataclass(frozen=True)
class ApifyRunResult:
    """Resultado de correr un actor: los items del dataset + metadatos de costo."""

    items: list[dict[str, Any]]
    usage_usd: float | None
    run_id: str | None


class ApifyClient:
    """Cliente HTTP mínimo para la API de Apify.

    El token va en el header `Authorization: Bearer` (nunca en la URL, para que no
    aparezca en logs de proxy/acceso). Construir con `client` inyectado para tests
    (respx), o dejar que cree el suyo.
    """

    def __init__(
        self,
        token: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = _APIFY_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        poll_interval_s: float = 2.0,
        max_wait_s: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.poll_interval_s = poll_interval_s
        self.max_wait_s = max_wait_s
        self._log = get_logger("memex.ingestors.social.apify_client")

        headers = {"Authorization": f"Bearer {token}"}
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout),
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ApifyClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def whoami(self) -> dict[str, Any]:
        """GET /v2/users/me — valida el token sin scrapear ni gastar. Para health_check."""
        return self._data(self._request("GET", "/v2/users/me"))

    def run_actor(self, actor_id: str, run_input: dict[str, Any]) -> ApifyRunResult:
        """Corre un actor de principio a fin y devuelve los items del dataset.

        `actor_id` viene en formato `username/actor`; Apify lo espera con `~` en la
        URL (`apify/instagram-scraper` → `apify~instagram-scraper`).
        """
        path_actor = actor_id.replace("/", "~")
        # idempotent=False: arrancar un run es una operación con costo y NO idempotente.
        # Si la conexión se cae tras encolar el run (o un 5xx llega después de encolarlo),
        # reintentar lanzaría un SEGUNDO run pago. Mejor fallar la cuenta (la captura
        # `social_fetch`) que duplicar el gasto.
        data = self._data(
            self._request("POST", f"/v2/acts/{path_actor}/runs", json=run_input, idempotent=False)
        )

        run_id = str(data.get("id") or "")
        status = str(data.get("status") or "")
        dataset_id = str(data.get("defaultDatasetId") or "")
        usage = _as_float(data.get("usageTotalUsd"))

        waited = 0.0
        while status in _RUNNING_STATUSES and waited < self.max_wait_s:
            time.sleep(self.poll_interval_s)
            waited += self.poll_interval_s
            data = self._data(self._request("GET", f"/v2/actor-runs/{run_id}"))
            status = str(data.get("status") or "")
            dataset_id = str(data.get("defaultDatasetId") or dataset_id)
            if data.get("usageTotalUsd") is not None:
                usage = _as_float(data.get("usageTotalUsd"))

        if status != _SUCCESS_STATUS:
            raise ApifyError(0, f"run {run_id!r} ended with status={status!r}")
        if not dataset_id:
            raise ApifyError(0, f"run {run_id!r} has no defaultDatasetId")

        items_resp = self._request(
            "GET",
            f"/v2/datasets/{dataset_id}/items",
            params={"clean": "true", "format": "json"},
        )
        items = items_resp.json()
        if not isinstance(items, list):
            raise ApifyError(0, "dataset items response is not a JSON array")

        return ApifyRunResult(
            items=[i for i in items if isinstance(i, dict)],
            usage_usd=usage,
            run_id=run_id or None,
        )

    @staticmethod
    def _data(resp: httpx.Response) -> dict[str, Any]:
        """Apify envuelve los objetos en `{"data": {...}}`. Lo desenvuelve defensivamente."""
        body = resp.json()
        if isinstance(body, dict):
            inner = body.get("data")
            if isinstance(inner, dict):
                return inner
            return body
        return {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
        idempotent: bool = True,
    ) -> httpx.Response:
        """HTTP con retry de 5xx/red. `idempotent=False` desactiva el retry para
        operaciones con efecto colateral no repetible (arrancar un run pago): un
        error de red/5xx tras encolar el run se convierte en `ApifyError` inmediato
        en vez de reintentar y lanzar un run duplicado.
        """
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.request(method, path, json=json, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                self._log.warning(
                    "apify.request.network_error",
                    method=method,
                    path=path,
                    exc=str(e),
                    attempt=attempt,
                )
                if not idempotent:
                    raise ApifyError(0, f"network error on non-idempotent {method} {path}") from e
            else:
                if 500 <= resp.status_code < 600:
                    last_exc = ApifyError(
                        resp.status_code,
                        f"server error {resp.status_code}",
                        body=resp.text[:500] if resp.text else None,
                    )
                    self._log.warning(
                        "apify.request.5xx",
                        method=method,
                        path=path,
                        status=resp.status_code,
                        attempt=attempt,
                    )
                    if not idempotent:
                        raise last_exc
                elif 400 <= resp.status_code < 500:
                    raise ApifyError(
                        resp.status_code,
                        f"client error {resp.status_code}",
                        body=resp.text[:500] if resp.text else None,
                    )
                else:
                    return resp

            if attempt < self.max_retries:
                time.sleep(self.backoff_base * (2**attempt))

        assert last_exc is not None
        raise last_exc


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
