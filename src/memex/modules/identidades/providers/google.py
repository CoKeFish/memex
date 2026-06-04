"""GooglePeopleClient — el ÚNICO lugar que habla HTTP con la API de Google People (Contacts) v1.

Aísla al vendor detrás del Protocol `ContactsProvider`: el worker consume `ProviderContact`/
`ProviderContactsPage`, nunca URLs ni shapes de Google. Usa httpx **async** (NO
`google-api-python-client`: arrastra httplib2 sync + discovery dinámico no-tipado que choca con
`mypy --strict`; el patrón retry/`aclose`/test-respx ya existe en `calendar/providers/google.py` y
se calca 1:1). Se reusa solo `google-auth` (vía `memex.google_oauth`) para mintear/refrescar el
access token, que va en `Authorization: Bearer` (nunca en la URL).

Sync incremental por `syncToken` (People API): full sync con `requestSyncToken=true` → devuelve
`nextSyncToken`; luego se pasa `syncToken` para traer solo cambios. Reglas de la People API:
  - al paginar (`pageToken`) o en incremental (`syncToken`), TODOS los demás params deben coincidir
    con la primera llamada → se re-mandan `personFields` + (`requestSyncToken`|`syncToken`) en cada
    página;
  - los borrados llegan en el delta como persona con `metadata.deleted=true` (conserva
    `resourceName`);
  - el `syncToken` caduca a los 7 días → HTTP **410** con `ErrorInfo` reason `EXPIRED_SYNC_TOKEN`
    (igual que Calendar) → `ContactsSyncTokenExpired` (el worker hace full resync).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any, ClassVar

import httpx

from memex.core.source import HealthResult
from memex.logging import get_logger
from memex.modules.identidades.providers.base import (
    ContactsProviderError,
    ContactsSyncTokenExpired,
    ProviderAddress,
    ProviderContact,
    ProviderContactsPage,
    ProviderIdentifier,
)
from memex.modules.identidades.providers.config import ContactsSyncConfig

_BODY_PREVIEW_MAX = 500
#: Campos pedidos por contacto. `metadata` trae `deleted` (delta) + el etag; el resto alimenta el
#: directorio unificado (nombre/contacto/cumpleaños/identificadores por-fuente/direcciones).
_PERSON_FIELDS = (
    "metadata,names,nicknames,emailAddresses,phoneNumbers,organizations,photos,"
    "birthdays,addresses,urls,imClients"
)


def _primary_or_first(items: Any) -> dict[str, Any] | None:
    """Elige el sub-record marcado `metadata.primary`, o el primero si ninguno lo está."""
    if not isinstance(items, list):
        return None
    dicts = [i for i in items if isinstance(i, dict)]
    if not dicts:
        return None
    for d in dicts:
        md = d.get("metadata")
        if isinstance(md, dict) and md.get("primary"):
            return d
    return dicts[0]


def _all_values(items: Any, key: str, *, lower: bool = False) -> tuple[str, ...]:
    """Junta los valores `key` de una lista (p.ej. emails), primary primero, sin duplicar."""
    if not isinstance(items, list):
        return ()
    dicts = [i for i in items if isinstance(i, dict)]
    dicts.sort(
        key=lambda d: (
            0 if isinstance(d.get("metadata"), dict) and d["metadata"].get("primary") else 1
        )
    )
    out: list[str] = []
    for d in dicts:
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            val = v.strip().lower() if lower else v.strip()
            if val not in out:
                out.append(val)
    return tuple(out)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _parse_birthday(item: dict[str, Any]) -> date | None:
    """Solo fecha COMPLETA (año+mes+día); parcial (sin año) → None (no rompe el DATE)."""
    cand = _primary_or_first(item.get("birthdays"))
    d = cand.get("date") if cand else None
    if not isinstance(d, dict):
        return None
    y, m, day = d.get("year"), d.get("month"), d.get("day")
    if isinstance(y, int) and isinstance(m, int) and isinstance(day, int):
        try:
            return date(y, m, day)
        except ValueError:
            return None
    return None


def _parse_addresses(item: dict[str, Any]) -> tuple[ProviderAddress, ...]:
    raw = item.get("addresses")
    if not isinstance(raw, list):
        return ()
    out: list[ProviderAddress] = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        formatted = _str_or_none(a.get("formattedValue"))
        country = _str_or_none(a.get("country"))
        label = _str_or_none(a.get("formattedType")) or _str_or_none(a.get("type")) or ""
        if formatted or country:
            out.append(ProviderAddress(label=label, address=formatted or "", country=country))
    return tuple(out)


def _parse_identifiers(item: dict[str, Any]) -> tuple[ProviderIdentifier, ...]:
    """URLs de perfil (`urls`) + cuentas de mensajería (`imClients`) como identificadores."""
    out: list[ProviderIdentifier] = []
    urls = item.get("urls")
    if isinstance(urls, list):
        for u in urls:
            if isinstance(u, dict):
                v = _str_or_none(u.get("value"))
                if v:
                    label = _str_or_none(u.get("type")) or "url"
                    out.append(ProviderIdentifier(platform=label.lower(), kind="url", value=v))
    ims = item.get("imClients")
    if isinstance(ims, list):
        for im in ims:
            if isinstance(im, dict):
                user = _str_or_none(im.get("username"))
                if user:
                    proto = _str_or_none(im.get("protocol")) or "im"
                    out.append(
                        ProviderIdentifier(platform=proto.lower(), kind="handle", value=user)
                    )
    return tuple(out)


def _parse_contact(item: dict[str, Any]) -> ProviderContact | None:
    """Construye un `ProviderContact` desde un `Person` de la People API. None si no tiene
    `resourceName` (sin él no hay clave de idempotencia)."""
    resource_name = item.get("resourceName")
    if not isinstance(resource_name, str) or not resource_name:
        return None

    meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    deleted = bool(meta.get("deleted")) if isinstance(meta, dict) else False
    etag = _str_or_none(item.get("etag"))

    name = _primary_or_first(item.get("names"))
    display_name = str(name.get("displayName") or "") if name else ""
    given_name = _str_or_none(name.get("givenName")) if name else None
    family_name = _str_or_none(name.get("familyName")) if name else None

    org = _primary_or_first(item.get("organizations"))
    org_name = _str_or_none(org.get("name")) if org else None
    role = _str_or_none(org.get("title")) if org else None

    photo = _primary_or_first(item.get("photos"))
    photo_url = _str_or_none(photo.get("url")) if photo else None

    return ProviderContact(
        resource_name=resource_name,
        etag=etag,
        display_name=display_name,
        given_name=given_name,
        family_name=family_name,
        emails=_all_values(item.get("emailAddresses"), "value", lower=True),
        phones=_all_values(item.get("phoneNumbers"), "value"),
        org_name=org_name,
        role=role,
        photo_url=photo_url,
        birthday=_parse_birthday(item),
        nicknames=_all_values(item.get("nicknames"), "value"),
        addresses=_parse_addresses(item),
        identifiers=_parse_identifiers(item),
        deleted=deleted,
    )


class GooglePeopleClient:
    """Cliente HTTP async mínimo para Google People v1. Implementa `ContactsProvider`.

    Construir con `client` inyectado para tests (respx), o dejar que cree el suyo.
    """

    name: ClassVar[str] = "google"

    def __init__(
        self,
        config: ContactsSyncConfig,
        access_token: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._log = get_logger("memex.modules.identidades.providers.google")

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(config.timeout_s),
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> GooglePeopleClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def health_check(self) -> HealthResult:
        now = datetime.now(UTC)
        try:
            await self._request("GET", "/people/me", params={"personFields": "names"})
        except Exception as e:  # health_check NUNCA lanza (contrato Source/ContactsProvider)
            return HealthResult(status="unhealthy", detail=f"google people: {e}", checked_at=now)
        return HealthResult(status="healthy", detail="google people reachable", checked_at=now)

    async def list_delta(
        self,
        *,
        sync_token: str | None,
        page_token: str | None = None,
    ) -> ProviderContactsPage:
        # Al paginar / en incremental, todos los params deben coincidir con la 1ª llamada → se
        # re-mandan personFields + (requestSyncToken | syncToken) en cada página.
        params: dict[str, str] = {
            "personFields": _PERSON_FIELDS,
            "pageSize": str(self._config.page_size),
        }
        if sync_token:
            params["syncToken"] = sync_token
        else:
            params["requestSyncToken"] = "true"
        if page_token:
            params["pageToken"] = page_token

        resp = await self._request("GET", "/people/me/connections", params=params)
        data: Any = resp.json()
        raw_items = data.get("connections", []) if isinstance(data, dict) else []

        contacts: list[ProviderContact] = []
        for item in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(item, dict):
                continue
            contact = _parse_contact(item)
            if contact is not None:
                contacts.append(contact)

        next_page = data.get("nextPageToken") if isinstance(data, dict) else None
        next_sync = data.get("nextSyncToken") if isinstance(data, dict) else None
        return ProviderContactsPage(
            contacts=tuple(contacts),
            next_page_token=next_page if isinstance(next_page, str) else None,
            next_sync_token=next_sync if isinstance(next_sync, str) else None,
        )

    @staticmethod
    def _is_expired_sync_token(resp: httpx.Response) -> bool:
        """People API: 410 GONE (igual que Calendar); por robustez también 400 con el reason
        `EXPIRED_SYNC_TOKEN` en el body."""
        if resp.status_code == 410:
            return True
        if resp.status_code == 400:
            return "EXPIRED_SYNC_TOKEN" in resp.text
        return False

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """HTTP con retry de 429/5xx/red (backoff exponencial); sync token caducado → expired; otro
        4xx → error inmediato."""
        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                resp = await self._client.request(method, path, params=params, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                self._log.warning(
                    "identidades.google.request.network_error",
                    path=path,
                    exc=str(e),
                    attempt=attempt,
                )
            else:
                if self._is_expired_sync_token(resp):
                    raise ContactsSyncTokenExpired(
                        resp.status_code,
                        "sync token expired",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    last_exc = ContactsProviderError(
                        resp.status_code,
                        f"server/rate error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                    self._log.warning(
                        "identidades.google.request.retryable",
                        status=resp.status_code,
                        attempt=attempt,
                    )
                elif 400 <= resp.status_code < 500:
                    raise ContactsProviderError(
                        resp.status_code,
                        f"client error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                else:
                    return resp

            if attempt < self._config.max_retries:
                await asyncio.sleep(self._config.backoff_base * (2**attempt))

        if isinstance(last_exc, ContactsProviderError):
            raise last_exc
        raise ContactsProviderError(0, f"network error on {method} {path}") from last_exc
