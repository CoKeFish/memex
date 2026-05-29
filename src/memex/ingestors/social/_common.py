"""Helpers compartidos por las tres sources sociales (`instagram` / `facebook` / `x`).

Centraliza lo que las tres comparten para evitar copias divergentes:

- `social_fetch`: el collect loop (correr el actor por cuenta + parsear + filtro de
  novedad + orden oldest-first), parametrizado por `parse_item` y `build_run_input`.
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
from collections.abc import Callable, Iterator
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

    Generador sync sobre httpx (no necesita el puente sync-over-async de Telegram).
    Por cuenta: corre el actor, parsea, filtra por cursor, ordena oldest-first
    (para que el runner avance el cursor a `chunk[-1]` = el más nuevo) y yieldea.
    Un error de cuenta se loggea y se saltea — no tumba el run completo.
    """
    if not cfg.accounts:
        log.info("social.fetch.skip", reason="no_accounts")
        return

    log.info("social.fetch.start", accounts_count=len(cfg.accounts))
    total_cost = 0.0
    cost_known = False

    with ApifyClient(
        cfg.apify_token.get_secret_value(),
        timeout=float(cfg.run_timeout_s),
        max_wait_s=float(cfg.run_timeout_s),
    ) as apify:
        for allowed in cfg.accounts:
            account = allowed.account
            acct_cursor = checkpoint.accounts.get(account)
            acct_log = log.bind(account=account)
            try:
                result = apify.run_actor(cfg.actor_id, build_run_input(account, cfg.results_limit))
            except ApifyError as e:
                acct_log.warning(
                    "social.fetch.account_error",
                    status_code=e.status_code,
                    exc_msg=str(e),
                )
                continue

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

            if result.usage_usd is not None:
                total_cost += result.usage_usd
                cost_known = True
            acct_log.info(
                "social.fetch.account_done",
                scraped=len(result.items),
                kept=len(kept),
                apify_run_id=result.run_id,
                apify_cost_usd=result.usage_usd,
            )
            yield from kept

    log.info(
        "social.fetch.end",
        accounts_count=len(cfg.accounts),
        apify_cost_usd=round(total_cost, 6) if cost_known else None,
    )


async def social_health_probe(cfg: SocialConfig) -> HealthResult:
    """Valida el token de Apify vía `GET /v2/users/me`. Nunca lanza, nunca gasta.

    Corre el httpx bloqueante en un threadpool, igual que `ImapSource.health_check`.
    El `detail` nunca incluye el token.
    """

    def _probe() -> tuple[Literal["healthy", "unhealthy"], str]:
        try:
            with ApifyClient(
                cfg.apify_token.get_secret_value(), timeout=float(cfg.run_timeout_s)
            ) as client:
                me = client.whoami()
            username = me.get("username") or me.get("id") or "?"
            return ("healthy", f"apify token ok, user={username}")
        except ApifyError as e:
            return ("unhealthy", f"apify: {e.status_code}")
        except Exception as e:
            return ("unhealthy", f"{type(e).__name__}: {e}")

    status, detail = await asyncio.to_thread(_probe)
    return HealthResult(status=status, detail=detail, checked_at=datetime.now(UTC))
