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
end}`. La plataforma va como campo bindeado del logger, no en el nombre del evento.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from datetime import UTC, datetime
from typing import Any, Literal

from memex.core.cursors import AccountCursor, SocialCursor
from memex.core.source import HealthResult, SourceRecord
from memex.ingestors.social.apify_client import ApifyClient, ApifyError
from memex.ingestors.social.config import SocialConfig

# parse_item(item, account) -> SourceRecord | None ; build_run_input(account, limit) -> dict
ParseItem = Callable[[dict[str, Any], str], SourceRecord | None]
BuildRunInput = Callable[[str, int], dict[str, Any]]

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

    LIMITACIÓN (compartida con IMAP/Telegram): el runner avanza el checkpoint a
    `chunk[-1]` por flush, y eso solo actualiza la cuenta de ese último record. Si
    un chunk mezcla varias cuentas, las cuentas que no terminan el chunk no avanzan
    su cursor en esa pasada. Es benigno: los posts ya quedaron en `inbox` y la
    próxima pasada los re-postea deduplicados (`UNIQUE(source_id, external_id)`); no
    hay pérdida de datos ni costo extra de scrape (siempre se scrapean los últimos N
    por cuenta). El fix "correcto" (flush por cuenta) es un cambio en el runner que
    aplicaría a todos los ingestors — fuera del alcance de este ticket.
    """
    parsed = split_social_external_id(last.external_id)
    if parsed is None:
        return checkpoint
    _platform, account, post_id = parsed
    new_accounts = dict(checkpoint.accounts)
    new_accounts[account] = AccountCursor(last_post_id=post_id, last_posted_at=last.occurred_at)
    return SocialCursor(accounts=new_accounts)


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


def social_fetch(
    cfg: SocialConfig,
    checkpoint: SocialCursor,
    *,
    parse_item: ParseItem,
    build_run_input: BuildRunInput,
    log: Any,
) -> Iterator[SourceRecord]:
    """Corre el actor por cada cuenta de la allowlist y yieldea records nuevos.

    Generador sync (parte del contrato `Source`) que puentea, vía `run_sync`, al
    trabajo async: las cuentas se scrapean EN PARALELO (gather + semáforo) y los
    records salen ya aplanados en orden de cuenta, oldest-first dentro de cada una
    (para que el runner avance el cursor a `chunk[-1]` = el más nuevo). Un error de
    cuenta se loggea y se saltea — no tumba el run completo.
    """
    yield from run_sync(
        _social_fetch_async(
            cfg,
            checkpoint,
            parse_item=parse_item,
            build_run_input=build_run_input,
            log=log,
        )
    )


async def _social_fetch_async(
    cfg: SocialConfig,
    checkpoint: SocialCursor,
    *,
    parse_item: ParseItem,
    build_run_input: BuildRunInput,
    log: Any,
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

    async with ApifyClient(
        cfg.apify_token.get_secret_value(),
        timeout=float(cfg.run_timeout_s),
        max_wait_s=float(cfg.run_timeout_s),
    ) as apify:

        async def _one(account: str) -> tuple[list[SourceRecord], float | None]:
            acct_cursor = checkpoint.accounts.get(account)
            acct_log = log.bind(account=account)
            async with sem:
                try:
                    result = await apify.run_actor(
                        cfg.actor_id, build_run_input(account, cfg.results_limit)
                    )
                except ApifyError as e:
                    acct_log.warning(
                        "social.fetch.account_error",
                        status_code=e.status_code,
                        exc_msg=str(e),
                    )
                    return [], None

            kept: list[SourceRecord] = []
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
                if not is_new_record(record, acct_cursor):
                    continue
                kept.append(record)

            # oldest-first: el runner avanza el cursor a chunk[-1], así el último
            # flusheado es el más nuevo. Los actores devuelven newest-first.
            kept.sort(key=lambda r: (r.occurred_at, r.external_id))

            acct_log.info(
                "social.fetch.account_done",
                scraped=len(result.items),
                kept=len(kept),
                apify_run_id=result.run_id,
                apify_cost_usd=result.usage_usd,
            )
            return kept, result.usage_usd

        results: list[tuple[list[SourceRecord], float | None]] = await asyncio.gather(
            *(_one(allowed.account) for allowed in cfg.accounts)
        )

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
