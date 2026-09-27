"""Backfill histórico para fuentes del cliente local (push), espejo del `mode=range` server-side.

El daemon solo hace ingesta INCREMENTAL (cursor + cap por-corrida). El backfill es la operación
faltante: traer una ventana histórica `[since, until)` a demanda, **independiente del cursor**
(no lo pisa) e **idempotente** (el dedup `UNIQUE(source_id, external_id)` absorbe re-corridas).

Reusa todo: el plugin construye su Source con `backfill_since`/`backfill_until` (contrato en
`protocol.py`), y `run_ingestor` lo drena en streaming hacia el `/ingest` del gateway. La única
diferencia con una corrida normal es el sink: NO persiste el cursor incremental.

La cobertura (`/inbox/coverage`) sale gratis: los correos ingeridos pueblan el timeline del source.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memex.ingestors.gateway_client import GatewayClient
from memex.ingestors.runner import RunStats, run_ingestor
from memex.logging import get_logger
from memex_local_client.protocol import LocalPlugin
from memex_local_client.registry import attach_source_id
from memex_local_client.run import load_plugin_config
from memex_local_client.state import State

_log = get_logger("memex_local_client.backfill")


class BackfillError(Exception):
    """Ventana inválida o backfill mal pedido."""


@dataclass(frozen=True)
class BackfillWindow:
    """Ventana histórica a traer: `[since, until)` (until exclusivo, ambos UTC-aware)."""

    since: datetime
    until: datetime


def _minus_months(dt: datetime, months: int) -> datetime:
    """Resta `months` meses calendario, clampando el día al fin de mes (sin dependencia externa)."""
    m = dt.month - 1 - months
    year = dt.year + m // 12
    month = m % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _parse_day(value: str | None) -> datetime:
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def resolve_window(
    *,
    months: int | None = None,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    now: datetime | None = None,
) -> BackfillWindow:
    """Resuelve la ventana desde `--since`/`--months`/`--days` (+ `--until`, default ahora)."""
    now = now or datetime.now(UTC)
    until_dt = _parse_day(until) if until else now
    if since:
        since_dt = _parse_day(since)
    elif days is not None:
        since_dt = until_dt - timedelta(days=days)
    elif months is not None:
        since_dt = _minus_months(until_dt, months)
    else:
        raise BackfillError("especificá --since, --months o --days")
    if since_dt >= until_dt:
        raise BackfillError(
            f"ventana vacía: since {since_dt.isoformat()} >= until {until_dt.isoformat()}"
        )
    return BackfillWindow(since=since_dt, until=until_dt)


class _CountingSink:
    """Sink de dry-run (MemexSink-shaped): cuenta lo que se mandaría, sin red ni escritura."""

    def get_sources_by_type(self, source_type: str) -> list[dict[str, Any]]:
        return []

    def get_checkpoint(self, source_id: int) -> dict[str, Any] | None:
        return {}

    def put_checkpoint(self, source_id: int, cursor: dict[str, Any]) -> None:
        pass

    def post_ingest_batch(self, records: list[dict[str, Any]]) -> dict[str, int]:
        return {"inserted": 0, "duplicates": 0, "errors": 0, "filtered": 0}


class BackfillGatewayClient(GatewayClient):
    """GatewayClient que NO toca el cursor incremental — para backfill independiente del cursor.

    Postea por `/ingest` (resolviendo el source vía `/state`) igual que el normal, pero ignora el
    checkpoint persistido y no lo escribe, así un backfill de historia vieja no pisa el incremental.
    """

    def get_checkpoint(self, source_id: int = 0) -> dict[str, Any] | None:
        del source_id  # el backfill arranca desde la ventana, no desde el checkpoint
        return {}

    def put_checkpoint(self, source_id: int, cursor: dict[str, Any]) -> None:
        del source_id, cursor  # no persistir el cursor incremental (espeja mode=range)


def run_backfill(
    plugin: LocalPlugin,
    *,
    gateway_url: str,
    api_token: str | None,
    plugins_root: Path,
    window: BackfillWindow,
    state: State,
    dry_run: bool = False,
    on_chunk: Callable[[RunStats], None] | None = None,
) -> RunStats:
    """Trae `window` para `plugin` a completitud (streaming, dedup, sin tocar el cursor).

    `dry_run` cuenta sin postear. Devuelve `RunStats` y registra la corrida (`mode=backfill`).
    """
    log = _log.bind(
        plugin=plugin.name, since=window.since.isoformat(), until=window.until.isoformat()
    )
    config = dict(load_plugin_config(plugin.name, plugins_root))
    config["backfill_since"] = window.since.isoformat()
    config["backfill_until"] = window.until.isoformat()
    source = plugin.build_source(config)

    with state.start_run(plugin.name, mode="backfill") as run_id:
        if dry_run:
            counting = _CountingSink()
            stats = run_ingestor(
                source, source_id=0, sink=counting, chunk_sleep_ms=0, on_chunk=on_chunk
            )
            state.finalize_run(run_id, status="ok", posted=stats.posted)
            log.info("memex_local_client.backfill.dry_run", scanned=stats.posted)
            return stats

        client = BackfillGatewayClient(
            base_url=gateway_url,
            plugin_name=plugin.name,
            source_type=plugin.source_type,
            api_token=api_token,
        )
        try:
            stats = run_ingestor(source, source_id=0, sink=client, on_chunk=on_chunk)
            if client.resolved_source_id is not None:
                attach_source_id(state, plugin.name, client.resolved_source_id)
            state.finalize_run(
                run_id,
                status="ok",
                posted=stats.posted,
                inserted=stats.inserted,
                duplicates=stats.duplicates,
                errors=stats.errors,
                filtered=stats.filtered,
            )
            log.info(
                "memex_local_client.backfill.finished",
                source_id=client.resolved_source_id,
                posted=stats.posted,
                inserted=stats.inserted,
                duplicates=stats.duplicates,
                filtered=stats.filtered,
            )
            return stats
        finally:
            client.close()
