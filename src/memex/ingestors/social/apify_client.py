"""ApifyClient — el ÚNICO lugar que habla HTTP con Apify.

Aísla al vendor: `source.py` / `parser.py` consumen dicts ya normalizados, nunca
URLs ni shapes de Apify. Si mañana cambiamos de provider (HikerAPI, EnsembleData)
solo cambia este módulo — el contrato `Source` no se entera.

Usa httpx **asíncrono** (`AsyncClient`) — NO el SDK `apify-client`. El caller sync
(`social_fetch`, generador del contrato `Source`) lo maneja vía el puente
`run_sync` en `_common.py`, igual que el polling de Telegram. Correr varias cuentas
en paralelo (gather + semáforo) es el motivo del async: menos wall-clock.

Flujo **run async + poll** (NO `run-sync-get-dataset-items`): los actores de scraping
pueden tardar más que el límite duro de `run-sync` y, sobre todo, `run-sync` no
devuelve el costo del run. Lanzamos el run (con `waitForFinish` para que Apify
retenga la respuesta hasta 60 s), poleamos hasta estado terminal, bajamos los items
del dataset PAGINADOS y releemos el run al final: `usageTotalUsd` y
`chargedEventCounts` se asientan unos segundos después de terminar.

Si el run no termina dentro de `max_wait_s` se ABORTA antes de rendirse — un run
huérfano sigue corriendo (y cobrando) en Apify aunque memex ya no lo espere.

ADR-001: este módulo vive en `ingestors/` — solo importa `httpx` + `memex.logging`,
nunca internals de memex (db/api/inbox/checkpoint).
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from memex.logging import get_logger

_APIFY_BASE_URL = "https://api.apify.com"

# Estados de un actor-run de Apify. Mientras esté en uno de estos, seguimos
# poleando; cualquier otro es terminal. Solo `SUCCEEDED` es éxito.
_RUNNING_STATUSES = frozenset({"READY", "RUNNING", "CREATED"})
_SUCCESS_STATUS = "SUCCEEDED"

# Página de descarga del dataset (offset/limit). Apify NO trunca por default —
# devuelve TODO — así que el tope lo ponemos nosotros: `max_items` del caller o
# este fallback defensivo (un backfill grande no debe bajar millones de items).
_DATASET_PAGE_SIZE = 1000
_DEFAULT_MAX_DATASET_ITEMS = 5000

# `waitForFinish` acepta hasta 60 s (long-poll del lado de Apify).
_MAX_WAIT_FOR_FINISH_S = 60.0


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


class ApifyTimeoutError(ApifyError):
    """El run no llegó a estado terminal dentro de `max_wait_s` y fue abortado.

    Lleva lo que se alcanzó a saber del run (id, costo parcial, eventos cobrados)
    para que el caller pueda trazar el gasto: un run abortado COBRA lo consumido.
    """

    def __init__(
        self,
        message: str,
        *,
        run_id: str | None = None,
        usage_usd: float | None = None,
        charged_events: dict[str, int] | None = None,
    ) -> None:
        super().__init__(0, message)
        self.run_id = run_id
        self.usage_usd = usage_usd
        self.charged_events = charged_events


@dataclass(frozen=True)
class ApifyRunResult:
    """Resultado de correr un actor: los items del dataset + metadatos de costo."""

    items: list[dict[str, Any]]
    usage_usd: float | None
    run_id: str | None
    # Desglose PPE (evento → cantidad cobrada) y timestamps del run, para trazabilidad.
    charged_events: dict[str, int] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ApifyClient:
    """Cliente HTTP async mínimo para la API de Apify.

    El token va en el header `Authorization: Bearer` (nunca en la URL, para que no
    aparezca en logs de proxy/acceso). Construir con `client` inyectado para tests
    (respx), o dejar que cree el suyo. Una instancia se comparte entre las corutinas
    de scraping concurrente (httpx.AsyncClient es seguro para requests concurrentes).
    """

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = _APIFY_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        poll_interval_s: float = 2.0,
        max_wait_s: float = 120.0,
        usage_settle_s: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.poll_interval_s = poll_interval_s
        self.max_wait_s = max_wait_s
        # Espera única antes de reintentar la lectura del costo (se asienta ~10 s
        # después de terminar el run). 0 en tests.
        self.usage_settle_s = usage_settle_s
        self.request_timeout_s = timeout
        self._log = get_logger("memex.ingestors.social.apify_client")

        headers = {"Authorization": f"Bearer {token}"}
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout),
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> ApifyClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def whoami(self) -> dict[str, Any]:
        """GET /v2/users/me — valida el token sin scrapear ni gastar. Para health_check."""
        return self._data(await self._request("GET", "/v2/users/me"))

    async def abort_run(self, run_id: str) -> None:
        """POST /v2/actor-runs/{id}/abort — best-effort, nunca lanza.

        Se usa al rendirse por timeout: sin esto el run quedaría corriendo (y
        cobrando) en Apify sin que nadie espere su resultado.
        """
        with suppress(Exception):
            await self._request("POST", f"/v2/actor-runs/{run_id}/abort")

    async def run_actor(
        self,
        actor_id: str,
        run_input: dict[str, Any],
        *,
        max_items: int | None = None,
        max_total_charge_usd: float | None = None,
    ) -> ApifyRunResult:
        """Corre un actor de principio a fin y devuelve los items del dataset.

        `actor_id` viene en formato `username/actor`; Apify lo espera con `~` en la
        URL (`apify/instagram-scraper` → `apify~instagram-scraper`).

        `max_items` acota la DESCARGA del dataset (el tope de scraping va en el
        run_input de cada actor); `max_total_charge_usd` es el tope de gasto del
        run para actores pay-per-event (Apify lo ignora en otros modelos).
        """
        path_actor = actor_id.replace("/", "~")
        deadline = time.monotonic() + self.max_wait_s

        params: dict[str, Any] = {"waitForFinish": self._wait_budget(deadline)}
        if max_total_charge_usd is not None:
            params["maxTotalChargeUsd"] = max_total_charge_usd
        # idempotent=False: arrancar un run es una operación con costo y NO idempotente.
        # Si la conexión se cae tras encolar el run (o un 5xx llega después de encolarlo),
        # reintentar lanzaría un SEGUNDO run pago. Mejor fallar la cuenta (la captura
        # `social_fetch`) que duplicar el gasto.
        data = self._data(
            await self._request(
                "POST",
                f"/v2/acts/{path_actor}/runs",
                json=run_input,
                params=params,
                idempotent=False,
            )
        )

        run_id = str(data.get("id") or "")
        status = str(data.get("status") or "")
        dataset_id = str(data.get("defaultDatasetId") or "")

        while status in _RUNNING_STATUSES:
            if time.monotonic() >= deadline:
                # El run sigue vivo en Apify: abortarlo y capturar el gasto parcial
                # (un run abortado cobra lo consumido hasta ahí).
                self._log.warning(
                    "apify.run.timeout_abort", run_id=run_id, max_wait_s=self.max_wait_s
                )
                await self.abort_run(run_id)
                snapshot = await self._run_snapshot(run_id)
                raise ApifyTimeoutError(
                    f"run {run_id!r} timed out after {self.max_wait_s}s (aborted)",
                    run_id=run_id or None,
                    usage_usd=_as_float((snapshot or {}).get("usageTotalUsd")),
                    charged_events=_as_event_counts((snapshot or {}).get("chargedEventCounts")),
                )
            await asyncio.sleep(self.poll_interval_s)
            data = self._data(
                await self._request(
                    "GET",
                    f"/v2/actor-runs/{run_id}",
                    params={"waitForFinish": self._wait_budget(deadline)},
                )
            )
            status = str(data.get("status") or "")
            dataset_id = str(data.get("defaultDatasetId") or dataset_id)

        if status != _SUCCESS_STATUS:
            raise ApifyError(0, f"run {run_id!r} ended with status={status!r}")
        if not dataset_id:
            raise ApifyError(0, f"run {run_id!r} has no defaultDatasetId")

        items = await self._fetch_dataset_items(dataset_id, max_items=max_items)

        # El costo se asienta unos segundos después de SUCCEEDED: releer el run ahora
        # (la descarga de items ya consumió un rato) y reintentar UNA vez si aún no está.
        snapshot = await self._run_snapshot(run_id)
        if snapshot is None or snapshot.get("usageTotalUsd") is None:
            await asyncio.sleep(self.usage_settle_s)
            snapshot = await self._run_snapshot(run_id) or snapshot
        final = snapshot or data

        return ApifyRunResult(
            items=items,
            usage_usd=_as_float(final.get("usageTotalUsd")),
            run_id=run_id or None,
            charged_events=_as_event_counts(final.get("chargedEventCounts")),
            started_at=_as_datetime(final.get("startedAt")),
            finished_at=_as_datetime(final.get("finishedAt")),
        )

    async def _run_snapshot(self, run_id: str) -> dict[str, Any] | None:
        """GET del run object, best-effort (None si falla) — para costo/desglose."""
        with suppress(Exception):
            return self._data(await self._request("GET", f"/v2/actor-runs/{run_id}"))
        return None

    async def _fetch_dataset_items(
        self, dataset_id: str, *, max_items: int | None
    ) -> list[dict[str, Any]]:
        """Baja los items del dataset PAGINADOS (offset/limit) hasta `max_items`.

        Apify devuelve TODO si no se pasa limit — un backfill grande podría bajar
        una respuesta gigante de un saque. El tope efectivo es `max_items` (la
        ventana pedida) o `_DEFAULT_MAX_DATASET_ITEMS` como red de seguridad.
        """
        cap = max_items if max_items is not None and max_items > 0 else _DEFAULT_MAX_DATASET_ITEMS
        items: list[dict[str, Any]] = []
        offset = 0
        while len(items) < cap:
            page_limit = min(_DATASET_PAGE_SIZE, cap - len(items))
            resp = await self._request(
                "GET",
                f"/v2/datasets/{dataset_id}/items",
                params={"clean": "true", "format": "json", "offset": offset, "limit": page_limit},
            )
            page = resp.json()
            if not isinstance(page, list):
                raise ApifyError(0, "dataset items response is not a JSON array")
            items.extend(i for i in page if isinstance(i, dict))
            if len(page) < page_limit:
                return items
            offset += len(page)
        # Cap alcanzado con la última página llena: puede haber quedado cola sin bajar.
        self._log.warning("apify.dataset.truncated", dataset_id=dataset_id, cap=cap)
        return items

    def _wait_budget(self, deadline: float) -> int:
        """Segundos de `waitForFinish` para el próximo request: lo que quede del
        presupuesto, acotado por el máximo de Apify (60 s) y por el read-timeout
        del cliente (con margen, para que el long-poll no lo dispare)."""
        remaining = deadline - time.monotonic()
        budget = min(_MAX_WAIT_FOR_FINISH_S, remaining, max(self.request_timeout_s - 5.0, 1.0))
        return max(int(budget), 0)

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

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        idempotent: bool = True,
    ) -> httpx.Response:
        """HTTP con retry de 5xx/red. `idempotent=False` desactiva el retry para
        operaciones con efecto colateral no repetible (arrancar un run pago): un
        error de red/5xx tras encolar el run se convierte en `ApifyError` inmediato
        en vez de reintentar y lanzar un run duplicado.
        """
        # httpx acepta int/float, pero el contrato del header/query de Apify es texto:
        # convertir en el borde evita sorpresas de serialización (True → "True", etc.).
        str_params = {k: str(v) for k, v in params.items()} if params is not None else None
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.request(method, path, json=json, params=str_params)
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
                await asyncio.sleep(self.backoff_base * (2**attempt))

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


def _as_event_counts(value: Any) -> dict[str, int] | None:
    """`chargedEventCounts` del run (PPE): {evento: cantidad}. None si no viene/shape raro."""
    if not isinstance(value, dict):
        return None
    out: dict[str, int] = {}
    for k, v in value.items():
        if isinstance(v, bool):
            continue
        if isinstance(v, int | float):
            out[str(k)] = int(v)
    return out or None


def _as_datetime(value: Any) -> datetime | None:
    """Timestamps ISO del run (`startedAt`/`finishedAt`, con sufijo Z). None si no parsea."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
