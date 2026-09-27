"""Cola de notificaciones (`GET /notifications` + acciones) y configuración del daemon.

Lectura/gestión de la cola (página `/notificaciones` y AlertBell) y, desde 0097, la configuración
que el dueño cambia en caliente: daemon prendido y zona horaria (`/notifications/settings`),
productores (`/notifications/producers/{name}`: activo, redacción, tiempos), canales de entrega
(`/notifications/channels`), entregas (`/notifications/deliveries`) y un ciclo a demanda
(`POST /notifications/run`, dry-run por default). La escritura de avisos sigue siendo por el seam
`Notifier` y los productores, no por HTTP. Multi-tenant: todo acotado a `current_user_id`.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from memex.api.auth import current_user_id
from memex.api.schemas import (
    NotificationChannelIn,
    NotificationChannelOut,
    NotificationChannelPatch,
    NotificationChannelTestIn,
    NotificationDeliveryRow,
    NotificationList,
    NotificationProducerOut,
    NotificationProducerPatch,
    NotificationRunIn,
    NotificationRunOut,
    NotificationSettingsOut,
    NotificationSettingsPatch,
)
from memex.db import connection
from memex.logging import get_logger
from memex.notifications import store
from memex.notifications.channels import ChannelError, build_channel, channel_names
from memex.notifications.client import AUDIENCES, SEVERITIES, Notification
from memex.notifications.producers import ProducerSpec, all_producers
from memex.notifications.service import run_cycle
from memex.notifications.settings import (
    NotificationsEnv,
    coerce_config,
    get_producer_settings,
    get_settings,
    parse_hhmm,
    upsert_producer_settings,
    upsert_settings,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.notifications")


@router.get("", response_model=NotificationList)
async def list_notifications(
    user_id: UserID,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: int | None = Query(default=None, description="id < cursor (paginación newest-first)"),
) -> dict[str, Any]:
    """Cola activa de avisos del usuario (newest-first). Excluye descartados y vencidos; incluye el
    conteo de no-leídas para el badge de la campana. Paginación por cursor (`id < :cursor`)."""
    with connection() as conn:
        items = store.list_active(conn, user_id=user_id, limit=limit, cursor=cursor)
        unread = store.count_unread(conn, user_id=user_id)
    next_cursor = items[-1]["id"] if len(items) == limit else None
    return {"items": items, "unread": unread, "next_cursor": next_cursor}


@router.post("/read-all")
async def mark_all_notifications_read(user_id: UserID) -> dict[str, int]:
    """Marca leídos todos los avisos activos sin leer del usuario. Devuelve cuántos cambió."""
    with connection() as conn:
        updated = store.mark_all_read(conn, user_id=user_id)
    _log.info("notifications.read_all", user_id=user_id, updated=updated)
    return {"updated": updated}


# ----- configuración del daemon (0097) ----------------------------------------------------- #


def _producer_out(conn: Any, user_id: int, spec: ProducerSpec) -> dict[str, Any]:
    ps = get_producer_settings(
        conn,
        user_id,
        spec.name,
        default_enabled=spec.default_enabled,
        default_render=spec.default_render,
        default_config=spec.default_config,
    )
    return {
        "name": spec.name,
        "audience": spec.audience,
        "description": spec.description,
        "enabled": ps.enabled,
        "render": ps.render if spec.llm_allowed else "plantilla",
        "llm_allowed": spec.llm_allowed,
        "config": ps.config,
        "default_config": spec.default_config,
        "from_db": ps.from_db,
    }


def _hhmm(value: Any) -> str | None:
    return value.strftime("%H:%M") if value is not None else None


def _channel_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "channel": str(row["channel"]),
        "target": str(row["target"]),
        "enabled": bool(row["enabled"]),
        "subscriptions": dict(row["subscriptions"] or {}),
        "quiet_from": _hhmm(row["quiet_from"]),
        "quiet_until": _hhmm(row["quiet_until"]),
        "created_at": row["created_at"],
    }


def _settings_out(conn: Any, user_id: int) -> dict[str, Any]:
    s = get_settings(conn, user_id, default_tz=NotificationsEnv().timezone)
    return {
        "daemon_enabled": s.daemon_enabled,
        "timezone": s.timezone,
        "producers": [_producer_out(conn, user_id, spec) for spec in all_producers().values()],
        "channels": [_channel_out(c) for c in store.list_channels(conn, user_id=user_id)],
        "channel_names": list(channel_names()),
    }


def _validate_subscriptions(subs: dict[str, str]) -> dict[str, str]:
    for audience, minimum in subs.items():
        if audience not in AUDIENCES:
            raise HTTPException(422, f"audiencia desconocida: {audience!r}; válidas: {AUDIENCES}")
        if minimum not in SEVERITIES:
            raise HTTPException(422, f"severidad inválida: {minimum!r}; válidas: {SEVERITIES}")
    return subs


def _parse_quiet(value: str | None) -> Any:
    if value is None or not value.strip():
        return None
    try:
        return parse_hhmm(value)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def _stringify(changes: dict[str, Any]) -> dict[str, str]:
    """El PATCH trae JSON tipado; `coerce_config` espera texto (es el mismo camino que el CLI)."""
    out: dict[str, str] = {}
    for k, v in changes.items():
        if isinstance(v, bool):
            out[k] = "si" if v else "no"
        elif isinstance(v, list):
            out[k] = ",".join(str(x) for x in v)
        else:
            out[k] = str(v)
    return out


@router.get("/settings", response_model=NotificationSettingsOut)
async def get_notification_settings(user_id: UserID) -> dict[str, Any]:
    """Daemon (prendido, zona), productores con su config efectiva, canales y los disponibles."""
    with connection() as conn:
        return _settings_out(conn, user_id)


@router.patch("/settings", response_model=NotificationSettingsOut)
async def patch_notification_settings(
    body: NotificationSettingsPatch, user_id: UserID
) -> dict[str, Any]:
    """Prende/apaga el daemon (lo relee cada tick) y/o cambia la zona horaria del usuario."""
    fields = body.model_dump(exclude_unset=True)
    with connection() as conn:
        if fields:
            try:
                upsert_settings(
                    conn,
                    user_id,
                    daemon_enabled=fields.get("daemon_enabled"),
                    timezone=fields.get("timezone"),
                )
            except Exception as e:  # zona horaria inexistente
                raise HTTPException(422, f"zona horaria inválida: {e}") from e
            _log.info("notifications.settings.patched", user_id=user_id, fields=list(fields))
        return _settings_out(conn, user_id)


@router.patch("/producers/{name}", response_model=NotificationProducerOut)
async def patch_notification_producer(
    name: str, body: NotificationProducerPatch, user_id: UserID
) -> dict[str, Any]:
    """Cambia un productor: activo, redacción (`plantilla`|`llm`, solo si lo admite) y claves de
    config validadas contra sus defaults (tipo y nombre)."""
    spec = all_producers().get(name)
    if spec is None:
        raise HTTPException(404, f"productor desconocido: {name}")
    fields = body.model_dump(exclude_unset=True)
    if fields.get("render") == "llm" and not spec.llm_allowed:
        raise HTTPException(422, f"{name} es solo plantilla")
    try:
        cfg = coerce_config(spec.default_config, _stringify(fields.get("config") or {}))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    with connection() as conn:
        upsert_producer_settings(
            conn,
            user_id,
            spec.name,
            default_enabled=spec.default_enabled,
            default_render=spec.default_render,
            default_config=spec.default_config,
            enabled=fields.get("enabled"),
            render=fields.get("render"),
            config_changes=cfg,
        )
        out = _producer_out(conn, user_id, spec)
    _log.info("notifications.producer.patched", user_id=user_id, producer=name, fields=list(fields))
    return out


@router.get("/channels", response_model=list[NotificationChannelOut])
async def list_notification_channels(user_id: UserID) -> list[dict[str, Any]]:
    with connection() as conn:
        return [_channel_out(c) for c in store.list_channels(conn, user_id=user_id)]


@router.post("/channels", response_model=NotificationChannelOut, status_code=201)
async def add_notification_channel(body: NotificationChannelIn, user_id: UserID) -> dict[str, Any]:
    """Alta (o reemplazo del mismo canal+destino). `telegram` exige `target` = chat_id numérico."""
    if body.channel not in channel_names():
        raise HTTPException(422, f"canal desconocido: {body.channel}; válidos: {channel_names()}")
    target = body.target.strip()
    if body.channel == "telegram" and not target.lstrip("-").isdigit():
        raise HTTPException(422, "telegram necesita target = chat_id numérico")
    subs = _validate_subscriptions(body.subscriptions)
    qf, qu = _parse_quiet(body.quiet_from), _parse_quiet(body.quiet_until)
    if (qf is None) != (qu is None):
        raise HTTPException(422, "el silencio necesita quiet_from y quiet_until")
    with connection() as conn:
        cid = store.upsert_channel(
            conn,
            user_id=user_id,
            channel=body.channel,
            target=target,
            subscriptions=subs,
            quiet_from=qf,
            quiet_until=qu,
        )
        row = store.get_channel(conn, user_id=user_id, channel_id=cid)
    assert row is not None
    _log.info("notifications.channel.added", user_id=user_id, channel=body.channel, id=cid)
    return _channel_out(row)


@router.patch("/channels/{channel_id}", response_model=NotificationChannelOut)
async def patch_notification_channel(
    channel_id: int, body: NotificationChannelPatch, user_id: UserID
) -> dict[str, Any]:
    """Activo, suscripciones y/o silencio (mandar ambos `quiet_*` en null para quitarlo)."""
    fields = body.model_dump(exclude_unset=True)
    subs = (
        _validate_subscriptions(fields["subscriptions"])
        if fields.get("subscriptions") is not None
        else None
    )
    quiet: tuple[Any, Any] | None = None
    if "quiet_from" in fields or "quiet_until" in fields:
        qf, qu = _parse_quiet(fields.get("quiet_from")), _parse_quiet(fields.get("quiet_until"))
        if (qf is None) != (qu is None):
            raise HTTPException(422, "el silencio necesita quiet_from y quiet_until (o ambos null)")
        quiet = (qf, qu)
    with connection() as conn:
        ok = store.update_channel(
            conn,
            user_id=user_id,
            channel_id=channel_id,
            enabled=fields.get("enabled"),
            subscriptions=subs,
            quiet=quiet,
        )
        if not ok:
            raise HTTPException(404, "channel not found")
        row = store.get_channel(conn, user_id=user_id, channel_id=channel_id)
    assert row is not None
    return _channel_out(row)


@router.delete("/channels/{channel_id}")
async def delete_notification_channel(channel_id: int, user_id: UserID) -> dict[str, bool]:
    with connection() as conn:
        ok = store.remove_channel(conn, user_id=user_id, channel_id=channel_id)
    if not ok:
        raise HTTPException(404, "channel not found")
    return {"ok": True}


@router.post("/channels/{channel_id}/test")
async def test_notification_channel(
    channel_id: int, body: NotificationChannelTestIn, user_id: UserID
) -> dict[str, Any]:
    """Manda un mensaje de prueba por ese canal (no pasa por la cola)."""
    with connection() as conn:
        row = store.get_channel(conn, user_id=user_id, channel_id=channel_id)
    if row is None:
        raise HTTPException(404, "channel not found")
    try:
        channel = build_channel(str(row["channel"]), NotificationsEnv())
    except ChannelError as e:
        raise HTTPException(422, str(e)) from e
    from datetime import UTC, datetime

    n = Notification(
        kind="prueba",
        severity="info",
        title="memex",
        body=body.text,
        dedup_key="",
        created_at=datetime.now(UTC),
        user_id=user_id,
    )
    try:
        ref = await channel.send(str(row["target"]), f"memex\n{body.text}", n)
    except ChannelError as e:
        raise HTTPException(502, f"el canal falló: {e}") from e
    finally:
        await channel.aclose()
    return {"ok": True, "ref": ref}


@router.get("/deliveries", response_model=list[NotificationDeliveryRow])
async def list_notification_deliveries(
    user_id: UserID, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[dict[str, Any]]:
    with connection() as conn:
        return store.list_deliveries(conn, user_id=user_id, limit=limit)


@router.post("/run", response_model=NotificationRunOut)
async def run_notifications_cycle(body: NotificationRunIn, user_id: UserID) -> dict[str, Any]:
    """Un ciclo (producir + entregar) a demanda. Con `dry_run` solo reporta qué haría."""
    if body.producer is not None and body.producer not in all_producers():
        raise HTTPException(422, f"productor desconocido: {body.producer}")
    stats = await run_cycle(user_id, only_producer=body.producer, dry_run=body.dry_run)
    _log.info(
        "notifications.run",
        user_id=user_id,
        dry_run=body.dry_run,
        enqueued=stats.enqueued,
        delivered=stats.delivered,
    )
    return stats.__dict__


# ----- acciones sobre un aviso ---------------------------------------------------------------- #


@router.post("/{notification_id}/read")
async def mark_notification_read(notification_id: int, user_id: UserID) -> dict[str, bool]:
    """Marca un aviso como leído (sale del conteo de no-leídas). 404 si no es del usuario."""
    with connection() as conn:
        ok = store.mark_read(conn, notification_id=notification_id, user_id=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="notification not found")
    return {"ok": True}


@router.post("/{notification_id}/dismiss")
async def dismiss_notification(notification_id: int, user_id: UserID) -> dict[str, bool]:
    """Descarta un aviso (lo saca de la cola activa). 404 si no es del usuario."""
    with connection() as conn:
        ok = store.dismiss(conn, notification_id=notification_id, user_id=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="notification not found")
    return {"ok": True}
