"""Helpers compartidos por las tres sources sociales (`instagram` / `facebook` / `x`).

Centraliza lo que las tres comparten para evitar copias divergentes:

- `social_fetch`: generador sync del contrato `Source` que puentea (vía `run_sync`)
  al trabajo async `_social_fetch_async`: corre el actor por cuenta EN PARALELO
  (gather + semáforo), parsea, filtra por novedad y ordena oldest-first.
- `run_sync`: puente async→sync (espejo de `telegram.client.run_sync`).
- `advance_social_checkpoint`: avanza el `SocialCursor` desde el `external_id`.
- `is_new_record`: filtro "since" client-side (los scrapers no tienen cursor nativo).
- `split_social_external_id`: parsea `{platform}:{account}:{post_id}` defensivamente.
- `social_health_probe`: valida el token de Apify sin scrapear.

ADR-001: vive en `ingestors/`, solo importa `memex.core.*`, `memex.logging` y los otros
módulos de `social/`. No toca DB.

Event names = literales estáticos (ADR-007): `social.fetch.{start,account_done,account_error,
end}` y `social.media.{downloaded,too_large,fetch_error,skipped}`. La plataforma/cuenta van
como campos bindeados del logger, no en el nombre del evento.

Extracción de media: si `cfg.extract_media`, tras parsear se bajan los bytes de las
`media_refs` del payload (fotos + video crudo) con un `httpx.AsyncClient` PROPIO (no el de
Apify: son CDNs públicos) y se adjuntan en `SourceRecord.media` para que el borde de ingest
los suba a MinIO + OCR. Las URLs de CDN llevan tokens firmados → se redactan (sin query) en logs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

import httpx

from memex.core.cursors import AccountCursor, SocialCursor
from memex.core.media_types import (
    SOCIAL_MEDIA_CONTENT_TYPES,
    make_media_blob,
    normalize_content_type,
)
from memex.core.source import ActorRunReport, HealthResult, MediaBlob, SourceRecord
from memex.ingestors.social.apify_client import ApifyClient, ApifyError, ApifyTimeoutError
from memex.ingestors.social.config import SocialConfig, SocialFetchMode


@dataclass(frozen=True)
class RunWindow:
    """Ventana efectiva del run de actor de UNA cuenta: modo + cotas de fecha + tope de items.

    La computa `_account_window` a partir del config (modo/since/until/limit) y, en
    incremental con `native_since`, del cursor por-cuenta. Los builders por plataforma la
    traducen al shape del actor (onlyPostsNewerThan / olderThan / start / end / limits).
    """

    mode: SocialFetchMode
    since: date | None
    until: date | None
    limit: int


# parse_item(item, account) -> SourceRecord | None ; build_run_input(account, window) -> dict
ParseItem = Callable[[dict[str, Any], str], SourceRecord | None]
BuildRunInput = Callable[[str, RunWindow], dict[str, Any]]

#: Tope de items por cuenta cuando un range no trae fetch_limit (espejo del default de
#: imap.fetch_range). En Instagram es el ÚNICO freno de costo del rango (escanea desde hoy).
_RANGE_DEFAULT_LIMIT = 200

_PLATFORMS = ("instagram", "facebook", "x")
# Cuentas scrapeadas en paralelo por run. Tope para no martillar Apify ni gatillar
# rate-limits; promovible a campo de SocialConfig si hiciera falta afinarlo.
_MAX_CONCURRENCY = 4


def split_social_external_id(external_id: str) -> tuple[str, str, str] | None:
    """Parsea `{platform}:{account}:{post_id}` → (platform, account, post_id).

    `maxsplit=2` deja el `post_id` intacto aunque contuviera `:`. Devuelve `None`
    si el prefijo no es una plataforma social o alguna parte queda vacía.
    """
    parts = external_id.split(":", 2)
    if len(parts) != 3:
        return None
    platform, account, post_id = parts
    if platform not in _PLATFORMS or not account or not post_id:
        return None
    return platform, account, post_id


def advance_social_checkpoint(checkpoint: SocialCursor, last: SourceRecord) -> SocialCursor:
    """Actualiza el `AccountCursor` de la cuenta del último record posteado.

    El `account` y `post_id` salen del `external_id` (keyed por la cuenta de la
    allowlist). `posted_at` se toma de `last.occurred_at` (ya es el timestamp del
    post). Record de otra source / malformado → cursor sin cambios (defensivo).

    Avance por-cuenta: esta función actualiza UNA cuenta (la del record que recibe).
    El runner la pliega sobre todos los records del chunk flusheado, así CADA cuenta
    avanza a su propio último post en una sola pasada (ver `run_ingestor`). Como los
    records salen oldest-first dentro de cada cuenta, el fold deja a cada cuenta en su
    máximo; las demás cuentas se preservan intactas.
    """
    parsed = split_social_external_id(last.external_id)
    if parsed is None:
        return checkpoint
    _platform, account, post_id = parsed
    new_accounts = dict(checkpoint.accounts)
    new_accounts[account] = AccountCursor(last_post_id=post_id, last_posted_at=last.occurred_at)
    return SocialCursor(accounts=new_accounts)


def _account_window(cfg: SocialConfig, acct_cursor: AccountCursor | None) -> RunWindow:
    """Ventana del run de actor para UNA cuenta según el modo (+ cursor en incremental).

    Incremental con `native_since`: el cursor por-cuenta viaja como cota inferior nativa con
    1 día de margen — los actores filtran a precisión de día y los pinned de IG se cuelan;
    el solape lo absorben `is_new_record` + el UNIQUE(source_id, external_id).
    """
    if cfg.fetch_mode == "range":
        return RunWindow(
            "range", cfg.fetch_since, cfg.fetch_until, cfg.fetch_limit or _RANGE_DEFAULT_LIMIT
        )
    if cfg.fetch_mode == "last":
        return RunWindow("last", None, None, cfg.fetch_limit or cfg.results_limit)
    since: date | None = None
    if cfg.native_since and acct_cursor is not None and acct_cursor.last_posted_at is not None:
        since = acct_cursor.last_posted_at.date() - timedelta(days=1)
    return RunWindow("incremental", since, None, cfg.results_limit)


def _window_bounds(window: RunWindow) -> tuple[datetime | None, datetime | None]:
    """[since, until) como datetimes UTC (medianoche) para el backstop client-side del rango."""
    since_dt = (
        datetime(window.since.year, window.since.month, window.since.day, tzinfo=UTC)
        if window.since is not None
        else None
    )
    until_dt = (
        datetime(window.until.year, window.until.month, window.until.day, tzinfo=UTC)
        if window.until is not None
        else None
    )
    return since_dt, until_dt


def _should_warn_saturation(
    window: RunWindow, acct_cursor: AccountCursor | None, *, scraped: int, saw_old: bool
) -> bool:
    """¿La ventana newest-N pudo dejar posts nuevos sin traer? Solo aplica al incremental.

    Sin cota nativa: traer el tope SIN ver ninguno viejo = no se alcanzó el cursor → gap
    posible. Con cota nativa (`window.since`): el actor ya filtró lo viejo, así que traer el
    tope (todo nuevo) sigue indicando gap posible. range/last son backfills acotados a
    propósito — el corte ahí es esperado, no una pérdida silenciosa.
    """
    if window.mode != "incremental":
        return False
    if acct_cursor is None or acct_cursor.last_posted_at is None:
        return False
    if scraped < window.limit:
        return False
    return window.since is not None or not saw_old


def is_new_record(record: SourceRecord, cursor: AccountCursor | None) -> bool:
    """True si el record es más nuevo que el cursor de su cuenta.

    Mantiene si `posted_at > last_posted_at`, o si es del mismo instante pero con
    distinto `post_id` (evita perder posts del mismo segundo). El re-fetch del post
    borde lo absorbe el dedupe `UNIQUE(source_id, external_id)` de memex.
    """
    if cursor is None or cursor.last_posted_at is None:
        return True
    if record.occurred_at > cursor.last_posted_at:
        return True
    if record.occurred_at == cursor.last_posted_at:
        parsed = split_social_external_id(record.external_id)
        post_id = parsed[2] if parsed is not None else None
        return post_id != cursor.last_post_id
    return False


def _redact_url(url: str) -> str:
    """Quita el query string (tokens firmados de CDN) para que la URL sea loggeable."""
    return url.split("?", 1)[0]


def _filename_from_url(url: str) -> str | None:
    """Último segmento del path de la URL como filename (para extensión / media_assets)."""
    name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return name or None


async def download_social_media(
    refs: list[dict[str, Any]],
    *,
    http: httpx.AsyncClient,
    max_image_bytes: int,
    max_video_bytes: int,
    log: Any,
) -> list[MediaBlob]:
    """Baja los bytes de las `media_refs` de un post → lista de `MediaBlob`.

    Defensivo y best-effort: una URL que falla (404, red, tipo no whitelisteado, supera el
    tope) se loggea y se saltea — nunca tumba el post ni el run. Dedup por sha256 (un mismo
    asset referenciado dos veces se sube una vez). El `content_type` real sale del header de
    respuesta (cae al de la ref si el header no viene); solo se aceptan los de
    `SOCIAL_MEDIA_CONTENT_TYPES` (imágenes + video).
    """
    blobs: list[MediaBlob] = []
    seen_sha: set[str] = set()
    total_bytes = 0
    for ref in refs:
        url = ref.get("url")
        if not isinstance(url, str) or not url:
            continue
        is_video = ref.get("kind") == "video"
        max_bytes = max_video_bytes if is_video else max_image_bytes
        try:
            resp = await http.get(url)
            resp.raise_for_status()
        except Exception as e:
            log.warning(
                "social.media.fetch_error",
                url=_redact_url(url),
                exc_type=type(e).__name__,
                exc_msg=str(e),
            )
            continue
        data = resp.content
        if not data:
            continue
        if len(data) > max_bytes:
            log.warning(
                "social.media.too_large", url=_redact_url(url), size=len(data), max_bytes=max_bytes
            )
            continue
        ctype = normalize_content_type(resp.headers.get("content-type")) or normalize_content_type(
            ref.get("content_type")
        )
        if ctype not in SOCIAL_MEDIA_CONTENT_TYPES:
            log.warning("social.media.skipped", url=_redact_url(url), content_type=ctype)
            continue
        blob = make_media_blob(data, content_type=ctype, filename=_filename_from_url(url))
        if blob.sha256 in seen_sha:
            continue
        seen_sha.add(blob.sha256)
        blobs.append(blob)
        total_bytes += len(data)
    if blobs:
        log.info("social.media.downloaded", count=len(blobs), bytes=total_bytes)
    return blobs


async def _attach_media(
    record: SourceRecord, *, http: httpx.AsyncClient, cfg: SocialConfig, log: Any
) -> SourceRecord:
    """Baja la media del record (de `payload['media_refs']`) y la adjunta en `record.media`."""
    refs = record.payload.get("media_refs")
    if not isinstance(refs, list) or not refs:
        return record
    blobs = await download_social_media(
        refs,
        http=http,
        max_image_bytes=cfg.max_attachment_bytes,
        max_video_bytes=cfg.max_video_bytes,
        log=log,
    )
    if not blobs:
        return record
    return replace(record, media=blobs)


def social_fetch(
    cfg: SocialConfig,
    checkpoint: SocialCursor,
    *,
    parse_item: ParseItem,
    build_run_input: BuildRunInput,
    log: Any,
    reports: list[ActorRunReport] | None = None,
) -> Iterator[SourceRecord]:
    """Corre el actor por cada cuenta de la allowlist y yieldea records nuevos.

    Generador sync (parte del contrato `Source`) que puentea, vía `run_sync`, al
    trabajo async: las cuentas se scrapean EN PARALELO (gather + semáforo) y los
    records salen ya aplanados en orden de cuenta, oldest-first dentro de cada una
    (para que el runner avance el cursor a `chunk[-1]` = el más nuevo). Un error de
    cuenta se loggea y se saltea — no tumba el run completo.

    `reports` (opcional) acumula un `ActorRunReport` por run de actor — TAMBIÉN en
    error/timeout (un run fallido pudo cobrar). Es seguro sin lock: las corutinas
    appendean desde el MISMO event loop. La Source lo expone vía `pop_run_reports`.
    """
    yield from run_sync(
        _social_fetch_async(
            cfg,
            checkpoint,
            parse_item=parse_item,
            build_run_input=build_run_input,
            log=log,
            reports=reports,
        )
    )


async def _social_fetch_async(
    cfg: SocialConfig,
    checkpoint: SocialCursor,
    *,
    parse_item: ParseItem,
    build_run_input: BuildRunInput,
    log: Any,
    reports: list[ActorRunReport] | None = None,
) -> list[SourceRecord]:
    """Trabajo async de `social_fetch`: scrapeo concurrente de la allowlist.

    Un único `ApifyClient` (AsyncClient) se comparte entre las corutinas; el
    semáforo limita cuántas cuentas corren a la vez. `gather` preserva el orden de
    `cfg.accounts`, así que el aplanado final es determinístico.
    """
    if not cfg.accounts:
        log.info("social.fetch.skip", reason="no_accounts")
        return []

    log.info("social.fetch.start", accounts_count=len(cfg.accounts))
    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    def _report(
        account: str,
        status: Literal["ok", "error", "timeout"],
        *,
        run_id: str | None = None,
        items_scraped: int = 0,
        items_kept: int = 0,
        cost_usd: float | None = None,
        charged_events: dict[str, int] | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        """Acumula el reporte del run de esta cuenta (si el caller pidió trazabilidad)."""
        if reports is None:
            return
        reports.append(
            ActorRunReport(
                platform=cfg.platform,
                account=account,
                actor_id=cfg.actor_id,
                apify_run_id=run_id,
                status=status,
                items_scraped=items_scraped,
                items_kept=items_kept,
                cost_usd=cost_usd,
                charged_events=charged_events,
                started_at=started_at,
                finished_at=finished_at,
            )
        )

    async with ApifyClient(
        cfg.apify_token.get_secret_value(),
        timeout=float(cfg.run_timeout_s),
        max_wait_s=float(cfg.run_timeout_s),
    ) as apify:
        # Cliente HTTP separado para bajar media de los CDNs públicos (NO el de Apify, que lleva
        # el token). `follow_redirects` porque los CDN suelen redirigir a un host de assets.
        media_http: httpx.AsyncClient | None = None
        if cfg.extract_media:
            media_http = httpx.AsyncClient(
                timeout=httpx.Timeout(float(cfg.run_timeout_s)), follow_redirects=True
            )

        async def _one(account: str) -> tuple[list[SourceRecord], float | None]:
            acct_cursor = checkpoint.accounts.get(account)
            window = _account_window(cfg, acct_cursor)
            acct_log = log.bind(account=account)
            async with sem:
                try:
                    result = await apify.run_actor(
                        cfg.actor_id,
                        build_run_input(account, window),
                        max_items=window.limit,
                        max_total_charge_usd=cfg.max_run_charge_usd,
                    )
                except ApifyTimeoutError as e:
                    # El run se abortó pero cobró lo consumido: reportarlo con su costo parcial.
                    acct_log.warning(
                        "social.fetch.account_timeout",
                        apify_run_id=e.run_id,
                        exc_msg=str(e),
                    )
                    _report(
                        account,
                        "timeout",
                        run_id=e.run_id,
                        cost_usd=e.usage_usd,
                        charged_events=e.charged_events,
                    )
                    return [], e.usage_usd
                except ApifyError as e:
                    acct_log.warning(
                        "social.fetch.account_error",
                        status_code=e.status_code,
                        exc_msg=str(e),
                    )
                    _report(account, "error")
                    return [], None

            since_dt, until_dt = _window_bounds(window)
            kept: list[SourceRecord] = []
            saw_old = False
            for raw in result.items:
                try:
                    record = parse_item(raw, account)
                except Exception as e:
                    # Defensa en profundidad: un item venenoso de un actor no debe
                    # tumbar el run completo (el parser ya intenta ser no-raising).
                    acct_log.warning(
                        "social.fetch.parse_error",
                        exc_type=type(e).__name__,
                        exc_msg=str(e),
                    )
                    continue
                if record is None:
                    continue
                if window.mode == "incremental":
                    # El filtro de cursor es DEL incremental: un backfill (range/last) debe
                    # poder traer posts más viejos que el cursor sin que se descarten.
                    if not is_new_record(record, acct_cursor):
                        saw_old = True
                        continue
                elif window.mode == "range":
                    # Backstop client-side del rango (since inclusivo / until exclusivo, UTC):
                    # IG no tiene techo nativo y los actores son imprecisos en el borde.
                    if since_dt is not None and record.occurred_at < since_dt:
                        continue
                    if until_dt is not None and record.occurred_at >= until_dt:
                        continue
                kept.append(record)

            # Saturación de la ventana newest-N del incremental (ver _should_warn_saturation).
            if _should_warn_saturation(
                window, acct_cursor, scraped=len(result.items), saw_old=saw_old
            ):
                acct_log.warning(
                    "social.fetch.window_saturated",
                    scraped=len(result.items),
                    results_limit=window.limit,
                )

            # oldest-first: el runner avanza el cursor a chunk[-1], así el último
            # flusheado es el más nuevo. Los actores devuelven newest-first.
            kept.sort(key=lambda r: (r.occurred_at, r.external_id))

            # Media: bajar bytes (fotos + video) FUERA del semáforo de Apify (no retener el slot
            # del actor durante descargas). Best-effort por record; un fallo no tumba la cuenta.
            if media_http is not None:
                kept = [
                    await _attach_media(rec, http=media_http, cfg=cfg, log=acct_log) for rec in kept
                ]

            acct_log.info(
                "social.fetch.account_done",
                scraped=len(result.items),
                kept=len(kept),
                media_assets=sum(len(r.media) for r in kept),
                apify_run_id=result.run_id,
                apify_cost_usd=result.usage_usd,
            )
            _report(
                account,
                "ok",
                run_id=result.run_id,
                items_scraped=len(result.items),
                items_kept=len(kept),
                cost_usd=result.usage_usd,
                charged_events=result.charged_events,
                started_at=result.started_at,
                finished_at=result.finished_at,
            )
            return kept, result.usage_usd

        try:
            results: list[tuple[list[SourceRecord], float | None]] = await asyncio.gather(
                *(_one(allowed.account) for allowed in cfg.accounts)
            )
        finally:
            if media_http is not None:
                await media_http.aclose()

    records: list[SourceRecord] = []
    total_cost = 0.0
    cost_known = False
    for kept, usage_usd in results:
        records.extend(kept)
        if usage_usd is not None:
            total_cost += usage_usd
            cost_known = True

    log.info(
        "social.fetch.end",
        accounts_count=len(cfg.accounts),
        apify_cost_usd=round(total_cost, 6) if cost_known else None,
    )
    return records


async def social_health_probe(cfg: SocialConfig) -> HealthResult:
    """Valida el token de Apify vía `GET /v2/users/me`. Nunca lanza, nunca gasta.

    Async-nativo (el `ApifyClient` ya es async). El `detail` nunca incluye el token.
    """
    status: Literal["healthy", "unhealthy"]
    try:
        async with ApifyClient(
            cfg.apify_token.get_secret_value(), timeout=float(cfg.run_timeout_s)
        ) as client:
            me = await client.whoami()
        username = me.get("username") or me.get("id") or "?"
        status, detail = "healthy", f"apify token ok, user={username}"
    except ApifyError as e:
        status, detail = "unhealthy", f"apify: {e.status_code}"
    except Exception as e:
        status, detail = "unhealthy", f"{type(e).__name__}: {e}"
    return HealthResult(status=status, detail=detail, checked_at=datetime.now(UTC))


def run_sync[T](coro: Coroutine[Any, Any, T] | Awaitable[T]) -> T:
    """Ejecuta una corrida async desde un caller sync (espejo de `telegram.run_sync`).

    Crea un event loop nuevo por invocación vía `asyncio.run`. NO usar desde dentro
    de un loop ya activo — solo desde el runner/CLI sync de polling, que es el caso.
    """
    if asyncio.iscoroutine(coro):
        result: T = asyncio.run(coro)
        return result

    async def _wrap() -> T:
        return await coro

    return asyncio.run(_wrap())
