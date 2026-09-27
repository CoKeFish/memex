"""Un ciclo del daemon de notificaciones: PRODUCIR (productores → cola) y ENTREGAR (cola → canales).

`run_cycle(user_id)` es la unidad que corren el daemon (cada tick), el job `notifications` del
scheduler y el CLI (`memex-notifications run`). Es idempotente: un borrador cuya `dedup_key` ya
está en la cola se descarta antes de redactar (no se paga LLM dos veces), y cada (aviso, canal) se
entrega una sola vez (`notification_deliveries`). Con `dry_run` no escribe ni llama al modelo: solo
reporta qué produciría y qué entregaría.

Filtros de entrega que dependen del usuario (se resuelven acá, no en SQL): severidad mínima por
audiencia (`subscriptions` del canal) y horas de silencio (solo lo `critica` pasa; el resto queda
pendiente hasta que termine la ventana).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any

from sqlalchemy import Connection

from memex.db import connection
from memex.llm.registry import build_llm_client
from memex.logging import get_logger
from memex.notifications import store
from memex.notifications.channels import Channel, ChannelError, build_channel
from memex.notifications.client import SEVERITIES, Notification
from memex.notifications.producers import Draft, ProducerContext, ProducerSpec, all_producers
from memex.notifications.render import LLMWriter, Writer, format_message
from memex.notifications.settings import (
    NotificationsEnv,
    get_producer_settings,
    get_settings,
    local_now,
)

_log = get_logger("memex.notifications.service")

_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass
class CycleStats:
    producers: list[str] = field(default_factory=list)
    drafts: int = 0
    enqueued: int = 0
    skipped_existing: int = 0
    rendered_llm: int = 0
    pending: int = 0
    delivered: int = 0
    failed: int = 0
    held_quiet: int = 0
    below_severity: int = 0
    errors: list[str] = field(default_factory=list)
    #: Solo con dry_run: (productor, título, cuerpo) de lo que se produciría.
    preview: list[dict[str, Any]] = field(default_factory=list)

    @property
    def did_something(self) -> bool:
        return bool(self.enqueued or self.delivered or self.failed or self.errors)


def in_quiet_hours(now: time, quiet_from: time | None, quiet_until: time | None) -> bool:
    """¿`now` cae en la ventana de silencio? Soporta ventanas que cruzan la medianoche."""
    if quiet_from is None or quiet_until is None or quiet_from == quiet_until:
        return False
    if quiet_from < quiet_until:
        return quiet_from <= now < quiet_until
    return now >= quiet_from or now < quiet_until


def passes_severity(severity: str, audience: str, subscriptions: dict[str, Any]) -> bool:
    minimum = str(subscriptions.get(audience, "info"))
    return _SEV_RANK.get(severity, 0) >= _SEV_RANK.get(minimum, 0)


def _notification_from_row(row: dict[str, Any], user_id: int) -> Notification:
    return Notification(
        kind=str(row["kind"]),
        severity=str(row["severity"]),
        title=str(row["title"]),
        body=str(row["body"]),
        dedup_key="",
        created_at=row["created_at"],
        user_id=user_id,
        payload=dict(row["payload"] or {}),
        deep_link=row["deep_link"],
        expires_at=row["expires_at"],
        audience=str(row["audience"]),
    )


def _to_notification(
    draft: Draft, body: str, spec: ProducerSpec, user_id: int, now: datetime
) -> Notification:
    return Notification(
        kind=draft.kind,
        severity=draft.severity,
        title=draft.title,
        body=body,
        dedup_key=draft.dedup_key,
        created_at=now,
        user_id=user_id,
        payload={"producer": spec.name, **draft.facts},
        deep_link=draft.deep_link,
        expires_at=draft.expires_at,
        audience=spec.audience,
    )


def build_writer(user_id: int) -> LLMWriter:
    client = build_llm_client("notifications", user_id=user_id)
    return LLMWriter(client, user_id=user_id)


async def _produce(
    conn: Connection,
    *,
    user_id: int,
    now: datetime,
    stats: CycleStats,
    only: str | None,
    dry_run: bool,
    writer: Writer | None,
    writer_factory: Any,
) -> Writer | None:
    for spec in all_producers().values():
        if only is not None and spec.name != only:
            continue
        ps = get_producer_settings(
            conn,
            user_id,
            spec.name,
            default_enabled=spec.default_enabled,
            default_render=spec.default_render,
            default_config=spec.default_config,
        )
        if not ps.enabled:
            continue
        stats.producers.append(spec.name)
        try:
            drafts = await spec.produce(ProducerContext(conn, user_id, now, ps.config))
        except Exception as e:  # un productor roto no frena a los demás
            _log.warning("notifications.producer.failed", producer=spec.name, error=str(e)[:300])
            stats.errors.append(f"{spec.name}: {e}")
            continue
        use_llm = ps.render == "llm" and spec.llm_allowed
        for d in drafts:
            stats.drafts += 1
            if store.exists(conn, user_id=user_id, dedup_key=d.dedup_key):
                stats.skipped_existing += 1
                continue
            if dry_run:
                stats.preview.append(
                    {"producer": spec.name, "title": d.title, "body": d.body, "render": ps.render}
                )
                continue
            body = d.body
            if use_llm and d.llm_brief:
                if writer is None:
                    try:
                        writer = writer_factory(user_id)
                    except Exception as e:  # sin proveedor: se queda la plantilla
                        _log.warning("notifications.render.no_llm", error=str(e)[:200])
                        writer_factory = _no_writer
                if writer is not None:
                    text = await writer.write(d)
                    if text:
                        body, stats.rendered_llm = text, stats.rendered_llm + 1
            store.enqueue(conn, _to_notification(d, body, spec, user_id, now))
            stats.enqueued += 1
            _log.info(
                "notification.produced",
                producer=spec.name,
                kind=d.kind,
                dedup_key=d.dedup_key,
                severity=d.severity,
            )
    return writer


def _no_writer(user_id: int) -> Writer | None:
    return None


async def _deliver(
    conn: Connection,
    *,
    user_id: int,
    now: datetime,
    env: NotificationsEnv,
    stats: CycleStats,
    dry_run: bool,
    channels: dict[str, Channel],
) -> None:
    rows = store.pending_deliveries(
        conn, user_id=user_id, max_attempts=env.max_attempts, retry_after_s=env.retry_after_s
    )
    stats.pending = len(rows)
    broken: set[str] = set()
    for row in rows:
        subs = dict(row["subscriptions"] or {})
        if not passes_severity(str(row["severity"]), str(row["audience"]), subs):
            stats.below_severity += 1
            continue
        if str(row["severity"]) != "critica" and in_quiet_hours(
            now.time(), row["quiet_from"], row["quiet_until"]
        ):
            stats.held_quiet += 1
            continue
        if dry_run:
            stats.preview.append(
                {"deliver": f"{row['channel']}:{row['target']}", "title": row["title"]}
            )
            continue
        name = str(row["channel"])
        if name in broken:
            continue
        channel = channels.get(name)
        if channel is None:
            try:
                channel = build_channel(name, env)
            except ChannelError as e:
                broken.add(name)
                stats.errors.append(f"canal {name}: {e}")
                _log.warning("notifications.channel.unavailable", channel=name, error=str(e))
                continue
            channels[name] = channel
        text = format_message(
            title=str(row["title"]),
            body=str(row["body"]),
            deep_link=row["deep_link"],
            dashboard_url=env.dashboard_url,
        )
        n = _notification_from_row(row, user_id)
        try:
            ref = await channel.send(str(row["target"]), text, n)
        except ChannelError as e:
            store.record_delivery(
                conn,
                notification_id=int(row["notification_id"]),
                channel_id=int(row["channel_id"]),
                status="fallida",
                error=str(e),
            )
            stats.failed += 1
            _log.warning(
                "notification.delivery.failed",
                channel=name,
                notification_id=row["notification_id"],
                attempts=int(row["attempts"]) + 1,
                error=str(e)[:200],
            )
            continue
        store.record_delivery(
            conn,
            notification_id=int(row["notification_id"]),
            channel_id=int(row["channel_id"]),
            status="enviada",
            external_ref=ref,
        )
        stats.delivered += 1
        _log.info(
            "notification.delivered",
            channel=name,
            target=str(row["target"]),
            notification_id=row["notification_id"],
            kind=row["kind"],
        )


async def run_cycle(
    user_id: int,
    *,
    env: NotificationsEnv | None = None,
    now: datetime | None = None,
    only_producer: str | None = None,
    dry_run: bool = False,
    channels: dict[str, Channel] | None = None,
    writer: Writer | None = None,
    writer_factory: Any = build_writer,
) -> CycleStats:
    """Produce y entrega una vez. `channels`/`writer` inyectables (tests, `channels test`)."""
    env = env or NotificationsEnv()
    stats = CycleStats()
    chans: dict[str, Channel] = dict(channels or {})
    own_writer = writer is None
    with connection() as conn:
        settings = get_settings(conn, user_id, default_tz=env.timezone)
        now_local = local_now(settings.tz, now)
        writer = await _produce(
            conn,
            user_id=user_id,
            now=now_local,
            stats=stats,
            only=only_producer,
            dry_run=dry_run,
            writer=writer,
            writer_factory=writer_factory,
        )
        await _deliver(
            conn,
            user_id=user_id,
            now=now_local,
            env=env,
            stats=stats,
            dry_run=dry_run,
            channels=chans,
        )
    for name, ch in chans.items():
        if channels is None or name not in channels:
            await ch.aclose()
    if own_writer and writer is not None:
        await writer.aclose()
    if stats.did_something or stats.errors:
        _log.info(
            "notifications.cycle",
            user_id=user_id,
            producers=stats.producers,
            drafts=stats.drafts,
            enqueued=stats.enqueued,
            rendered_llm=stats.rendered_llm,
            delivered=stats.delivered,
            failed=stats.failed,
            held_quiet=stats.held_quiet,
            errors=stats.errors,
        )
    return stats
