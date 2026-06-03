"""Worker de sincronización de calendarios externos (slice 1 = ingress read-only idempotente).

`memex-calendar-sync pull` trae el delta de eventos de un proveedor (Google) y los escribe DIRECTO
a `mod_calendar_events` (origin='provider'), corriendo el MISMO dedup determinista FASE 1 que la
extracción LLM. NO pasa por inbox/classifier/LLM: los eventos del proveedor ya vienen
estructurados (decisión del dueño — el sync vive dentro del módulo, ADR-015 §4 enmendado).

Idempotencia: un `provider_event_id` no se duplica entre corridas (UNIQUE parcial + upsert por
SELECT-then-INSERT/UPDATE comparando `etag`). El cursor incremental (`sync_token` de Google) se
persiste en la cuenta; el próximo `pull` solo trae el delta. 410 GONE (token caducado) → full
resync transparente.

Eventos manuales = autoritativos (decisión 7): un evento del proveedor SIN marcador memex
(`memex_consolidated_id`) lo agregó el usuario a mano → `manual=TRUE` + `priority_rank` alto.

Observabilidad (requisito del dueño): cada corrida escribe una fila `mod_calendar_sync_runs`
(contadores created/modified/deleted/...) y una fila `mod_calendar_event_changes` por cada evento
creado/modificado/eliminado, todo atómico con los eventos + el cursor en una sola transacción.

Cliente del proveedor inyectable (tests con fake, sin red), igual que el worker de OCR.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, time

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.db import connection
from memex.logging import get_logger
from memex.modules.calendar.dedup import DedupRow
from memex.modules.calendar.module import _mark_dedup
from memex.modules.calendar.providers import oauth, resolve
from memex.modules.calendar.providers.base import (
    CalendarProvider,
    CalendarProviderError,
    CalendarSyncTokenExpired,
    ProviderEvent,
    ProviderEventWrite,
)
from memex.modules.calendar.providers.config import CalendarSyncConfig
from memex.modules.calendar.providers.google import GoogleCalendarClient

_log = get_logger("memex.modules.calendar.sync")

#: Prioridad por default de un evento que el usuario agregó a mano en su proveedor (sin marcador
#: memex). Alta para que gane en la consolidación y no sea sobrescrito (decisión 7). Configurable
#: a futuro vía `module_settings.config`.
_MANUAL_PRIORITY_RANK = 100


@dataclass
class SyncStats:
    """Resumen de una corrida de sync (espeja los contadores de `mod_calendar_sync_runs`)."""

    pulled: int = 0  # eventos no-cancelados traídos del proveedor
    created: int = 0  # filas nuevas insertadas
    modified: int = 0  # filas existentes actualizadas (cambió el etag)
    deleted: int = 0  # eventos marcados cancelled en el proveedor
    unchanged: int = 0  # eventos sin cambios (mismo etag)
    dedup_pairs: int = 0  # pares candidatos marcados por el dedup FASE 1
    errors: int = 0


@dataclass(frozen=True)
class _Account:
    id: int
    user_id: int
    provider: str
    calendar_id: str
    token_path_env: str
    sync_token: str | None
    enabled: bool


# --- DB helpers -------------------------------------------------------------------- #


def _load_account(conn: Connection, user_id: int, account_id: int) -> _Account | None:
    row = conn.execute(
        text(
            """
            SELECT id, user_id, provider, calendar_id, token_path_env, sync_token, enabled
            FROM mod_calendar_provider_accounts
            WHERE id = :id AND user_id = :uid
            """
        ),
        {"id": account_id, "uid": user_id},
    ).first()
    if row is None:
        return None
    return _Account(
        id=int(row[0]),
        user_id=int(row[1]),
        provider=str(row[2]),
        calendar_id=str(row[3]),
        token_path_env=str(row[4]),
        sync_token=(str(row[5]) if row[5] is not None else None),
        enabled=bool(row[6]),
    )


def _start_run(
    conn: Connection, user_id: int, account_id: int, *, direction: str = "ingress"
) -> int:
    return int(
        conn.execute(
            text(
                """
                INSERT INTO mod_calendar_sync_runs (user_id, provider_account_id, direction)
                VALUES (:uid, :aid, :dir)
                RETURNING id
                """
            ),
            {"uid": user_id, "aid": account_id, "dir": direction},
        ).scalar_one()
    )


def _finish_run(conn: Connection, run_id: int, stats: SyncStats, *, status: str) -> None:
    conn.execute(
        text(
            """
            UPDATE mod_calendar_sync_runs SET
              pulled = :pulled, created = :created, modified = :modified, deleted = :deleted,
              unchanged = :unchanged, dedup_pairs = :dedup_pairs, errors = :errors,
              status = :status, finished_at = NOW()
            WHERE id = :id
            """
        ),
        {
            "id": run_id,
            "pulled": stats.pulled,
            "created": stats.created,
            "modified": stats.modified,
            "deleted": stats.deleted,
            "unchanged": stats.unchanged,
            "dedup_pairs": stats.dedup_pairs,
            "errors": stats.errors,
            "status": status,
        },
    )


def _record_change(
    conn: Connection,
    *,
    user_id: int,
    run_id: int,
    event_id: int | None,
    provider: str,
    provider_event_id: str | None,
    action: str,
    direction: str = "ingress",
    consolidated_id: int | None = None,
    detail: dict[str, object] | None = None,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mod_calendar_event_changes
              (user_id, sync_run_id, event_id, consolidated_id, provider, provider_event_id,
               direction, action, detail)
            VALUES
              (:uid, :run, :eid, :cid, :provider, :peid, :dir, :action, CAST(:detail AS JSONB))
            """
        ),
        {
            "uid": user_id,
            "run": run_id,
            "eid": event_id,
            "cid": consolidated_id,
            "provider": provider,
            "peid": provider_event_id,
            "dir": direction,
            "action": action,
            "detail": json.dumps(detail or {}),
        },
    )


def _dedup_row(event_id: int, ev: ProviderEvent) -> DedupRow:
    return DedupRow(
        event_id=event_id,
        title=ev.title,
        location=ev.location,
        starts_on=ev.starts_on,
        ends_on=ev.ends_on,
        start_time=ev.start_time,
        end_time=ev.end_time,
    )


def _upsert_event(
    conn: Connection, account: _Account, ev: ProviderEvent, run_id: int
) -> tuple[str, DedupRow | None]:
    """Inserta o actualiza un evento de proveedor por su `provider_event_id`. Devuelve la acción
    ('created'/'modified'/'unchanged') y el `DedupRow` para el dedup (None si no cambió)."""
    existing = conn.execute(
        text(
            """
            SELECT id, provider_etag, recurring_event_id FROM mod_calendar_events
            WHERE provider = :provider AND provider_account_id = :aid
              AND provider_event_id = :peid
            """
        ),
        {"provider": account.provider, "aid": account.id, "peid": ev.provider_event_id},
    ).first()

    manual = ev.memex_consolidated_id is None
    # El marcador memex (si vino en extendedProperties) se guarda en metadata: la consolidación lo
    # usa para reconocer ecos (eventos que memex escribió en el proveedor) y evitar el loop.
    meta = {"memex_consolidated_id": ev.memex_consolidated_id} if ev.memex_consolidated_id else {}
    params: dict[str, object] = {
        "title": ev.title,
        "starts_on": ev.starts_on,
        "ends_on": ev.ends_on,
        "start_time": ev.start_time,
        "end_time": ev.end_time,
        "location": ev.location,
        "description": ev.description,
        "etag": ev.etag,
        "updated": ev.updated,
        "status": ev.status,
        "manual": manual,
        "rank": _MANUAL_PRIORITY_RANK if manual else 0,
        "metadata": json.dumps(meta),
        "recurring": ev.recurring_event_id,
    }

    if existing is None:
        event_id = int(
            conn.execute(
                text(
                    """
                    INSERT INTO mod_calendar_events
                      (user_id, source_inbox_ids, title, starts_on, ends_on, start_time, end_time,
                       location, description, origin, provider, provider_account_id,
                       provider_event_id, provider_etag, provider_updated, provider_status,
                       manual, priority_rank, metadata, recurring_event_id)
                    VALUES
                      (:uid, ARRAY[]::bigint[], :title, :starts_on, :ends_on, :start_time,
                       :end_time, :location, :description, 'provider', :provider, :aid, :peid,
                       :etag, :updated, :status, :manual, :rank, CAST(:metadata AS JSONB),
                       :recurring)
                    RETURNING id
                    """
                ),
                {
                    **params,
                    "uid": account.user_id,
                    "provider": account.provider,
                    "aid": account.id,
                    "peid": ev.provider_event_id,
                },
            ).scalar_one()
        )
        _record_change(
            conn,
            user_id=account.user_id,
            run_id=run_id,
            event_id=event_id,
            provider=account.provider,
            provider_event_id=ev.provider_event_id,
            action="created",
            detail={"title": ev.title},
        )
        # Los ecos (eventos que memex escribió en el proveedor) NO entran al dedup: la consolidación
        # los linkea directo a su consolidado (evita pares espurios + costo de LLM + el loop).
        return "created", (None if ev.memex_consolidated_id else _dedup_row(event_id, ev))

    event_id = int(existing[0])
    current_etag = existing[1]
    current_recurring = existing[2]
    # "Sin cambios" exige también que `recurring_event_id` ya coincida: así un full resync
    # backfillea la columna en eventos viejos (mismo etag, columna NULL) desde la API, sin
    # re-parsear el id.
    if (
        ev.etag is not None
        and current_etag == ev.etag
        and current_recurring == ev.recurring_event_id
    ):
        return "unchanged", None

    conn.execute(
        text(
            """
            UPDATE mod_calendar_events SET
              title = :title, starts_on = :starts_on, ends_on = :ends_on,
              start_time = :start_time, end_time = :end_time, location = :location,
              description = :description, provider_etag = :etag, provider_updated = :updated,
              provider_status = :status, manual = :manual, priority_rank = :rank,
              recurring_event_id = :recurring
            WHERE id = :id
            """
        ),
        {**params, "id": event_id},
    )
    _record_change(
        conn,
        user_id=account.user_id,
        run_id=run_id,
        event_id=event_id,
        provider=account.provider,
        provider_event_id=ev.provider_event_id,
        action="modified",
        detail={"title": ev.title},
    )
    return "modified", (None if ev.memex_consolidated_id else _dedup_row(event_id, ev))


def _mark_cancelled(
    conn: Connection, account: _Account, provider_event_id: str, run_id: int
) -> bool:
    """Marca `provider_status='cancelled'` el evento local borrado en el proveedor. NO borra la
    fila (coexistencia / auditoría). Devuelve True si existía una fila para marcar."""
    row = conn.execute(
        text(
            """
            UPDATE mod_calendar_events SET provider_status = 'cancelled'
            WHERE provider = :provider AND provider_account_id = :aid
              AND provider_event_id = :peid
            RETURNING id
            """
        ),
        {"provider": account.provider, "aid": account.id, "peid": provider_event_id},
    ).first()
    if row is None:
        return False
    _record_change(
        conn,
        user_id=account.user_id,
        run_id=run_id,
        event_id=int(row[0]),
        provider=account.provider,
        provider_event_id=provider_event_id,
        action="deleted",
    )
    return True


def _save_cursor(conn: Connection, account_id: int, sync_token: str | None) -> None:
    if sync_token is not None:
        conn.execute(
            text(
                "UPDATE mod_calendar_provider_accounts "
                "SET sync_token = :t, last_sync_at = NOW() WHERE id = :id"
            ),
            {"t": sync_token, "id": account_id},
        )
    else:
        conn.execute(
            text("UPDATE mod_calendar_provider_accounts SET last_sync_at = NOW() WHERE id = :id"),
            {"id": account_id},
        )


# --- fetch (red) ------------------------------------------------------------------- #


async def _fetch_all(
    client: CalendarProvider, *, sync_token: str | None, full: bool
) -> tuple[list[ProviderEvent], list[str], str | None]:
    """Pagina `list_delta` hasta agotar, acumulando eventos + borrados y capturando el
    `next_sync_token` final. Si el `sync_token` caducó (410) hace un full resync una vez."""
    use_token = None if full else sync_token
    events: list[ProviderEvent] = []
    deleted_ids: list[str] = []
    page_token: str | None = None
    new_sync_token: str | None = None

    while True:
        try:
            page = await client.list_delta(sync_token=use_token, page_token=page_token)
        except CalendarSyncTokenExpired:
            if use_token is None:  # ya era full sync → no hay token que invalidar; propagá
                raise
            _log.warning("calendar.sync.token_expired_full_resync")
            use_token = None
            page_token = None
            events, deleted_ids = [], []
            continue
        events.extend(page.events)
        deleted_ids.extend(page.deleted_ids)
        if page.next_page_token:
            page_token = page.next_page_token
            # NO anular use_token: el cliente necesita saber el modo (full vs incremental) para
            # re-mandar la ventana en cada página del full sync (Google la pierde si se omite).
            continue
        new_sync_token = page.next_sync_token
        break

    return events, deleted_ids, new_sync_token


# --- entry point ------------------------------------------------------------------- #


async def run_pull(
    user_id: int,
    account_id: int,
    *,
    full: bool = False,
    past_days: int | None = None,
    future_days: int | None = None,
    client: CalendarProvider | None = None,
) -> SyncStats:
    """Sincroniza (ingress) una cuenta de calendario externo hacia `mod_calendar_events`.

    `full=True` ignora el cursor y trae todo. `past_days`/`future_days` overridean la ventana de
    fechas del full sync (default: config 183/365) — perillas por-corrida. `client` inyectable
    (tests con fake, sin red). Best-effort: errores de proveedor se loguean + registran en
    `mod_calendar_sync_runs`.
    """
    stats = SyncStats()

    with connection() as conn:
        account = _load_account(conn, user_id, account_id)
    if account is None:
        _log.error("calendar.sync.account_not_found", user_id=user_id, account_id=account_id)
        stats.errors += 1
        return stats
    if not account.enabled:
        _log.info("calendar.sync.account_disabled", account_id=account_id)
        return stats

    owns_client = client is None
    active: CalendarProvider
    if client is not None:
        active = client
    else:
        token_path = oauth.resolve_token_path(account.provider, account.id, account.token_path_env)
        access = oauth.access_token(account.provider, token_path=token_path)
        cfg = CalendarSyncConfig.from_env()
        window = {
            k: v
            for k, v in (("sync_past_days", past_days), ("sync_future_days", future_days))
            if v is not None
        }
        if window:
            cfg = cfg.model_copy(update=window)
        active = resolve(account.provider)(cfg, access, account.calendar_id)

    _log.info(
        "calendar.sync.start",
        user_id=user_id,
        account_id=account_id,
        provider=account.provider,
        full=full,
    )

    try:
        events, deleted_ids, new_sync_token = await _fetch_all(
            active, sync_token=account.sync_token, full=full
        )
    except CalendarProviderError as e:
        stats.errors += 1
        _log.error(
            "calendar.sync.fetch_failed", account_id=account_id, status=e.status_code, msg=str(e)
        )
        with connection() as conn:
            run_id = _start_run(conn, user_id, account_id)
            _finish_run(conn, run_id, stats, status="error")
        return stats
    finally:
        if owns_client and isinstance(active, GoogleCalendarClient):
            await active.aclose()

    stats.pulled = len(events)
    new_rows: list[DedupRow] = []
    with connection() as conn:
        run_id = _start_run(conn, user_id, account_id)
        for ev in events:
            action, row = _upsert_event(conn, account, ev, run_id)
            if action == "created":
                stats.created += 1
            elif action == "modified":
                stats.modified += 1
            else:
                stats.unchanged += 1
            if row is not None:
                new_rows.append(row)
        for provider_event_id in deleted_ids:
            if _mark_cancelled(conn, account, provider_event_id, run_id):
                stats.deleted += 1
        if new_rows:
            stats.dedup_pairs = _mark_dedup(conn, user_id, new_rows)
        _save_cursor(conn, account_id, new_sync_token)
        _finish_run(conn, run_id, stats, status="ok")

    _log.info(
        "calendar.sync.end",
        user_id=user_id,
        account_id=account_id,
        pulled=stats.pulled,
        created=stats.created,
        modified=stats.modified,
        deleted=stats.deleted,
        unchanged=stats.unchanged,
        dedup_pairs=stats.dedup_pairs,
    )
    return stats


# --- write-back (egress, slice 5) -------------------------------------------------- #


@dataclass
class PushStats:
    """Resumen de una corrida de write-back (egress) a un proveedor."""

    consolidated: int = 0  # consolidados considerados
    created: int = 0  # eventos creados en el proveedor
    updated: int = 0  # eventos actualizados (cambió el contenido)
    deleted: int = 0  # eventos borrados en el proveedor (consolidado tombstoneado)
    skipped: int = 0  # sin cambios (echo-suppression) o el proveedor ya tiene el evento del user
    errors: int = 0


@dataclass(frozen=True)
class _ConsView:
    cons_id: int
    title: str
    starts_on: date
    ends_on: date | None
    start_time: time | None
    end_time: time | None
    location: str
    description: str
    deleted: bool


@dataclass(frozen=True)
class _WbRow:
    provider_event_id: str | None
    last_pushed_etag: str | None
    last_pushed_signature: str | None
    state: str


def _load_consolidated(conn: Connection, user_id: int) -> list[_ConsView]:
    rows = (
        conn.execute(
            text(
                """
                SELECT id, title, starts_on, ends_on, start_time, end_time, location,
                       description, deleted
                FROM mod_calendar_consolidated WHERE user_id = :uid ORDER BY id
                """
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    )
    return [
        _ConsView(
            cons_id=int(r["id"]),
            title=str(r["title"]),
            starts_on=r["starts_on"],
            ends_on=r["ends_on"],
            start_time=r["start_time"],
            end_time=r["end_time"],
            location=str(r["location"]),
            description=str(r["description"]),
            deleted=bool(r["deleted"]),
        )
        for r in rows
    ]


def _account_has_user_event(conn: Connection, cons_id: int, account_id: int) -> bool:
    """¿El consolidado ya tiene un evento del usuario (NO un eco) en esta cuenta? Si sí, la cuenta
    es la fuente autoritativa → no se le pushea (evita duplicar lo que el usuario puso a mano)."""
    return (
        conn.execute(
            text(
                """
                SELECT 1 FROM mod_calendar_event_links l
                JOIN mod_calendar_events e ON e.id = l.event_id
                WHERE l.consolidated_id = :cid AND e.provider_account_id = :aid
                  AND (e.metadata->>'memex_consolidated_id') IS NULL
                LIMIT 1
                """
            ),
            {"cid": cons_id, "aid": account_id},
        ).first()
        is not None
    )


def _load_wb(conn: Connection, cons_id: int, account_id: int) -> _WbRow | None:
    row = conn.execute(
        text(
            """
            SELECT provider_event_id, last_pushed_etag, last_pushed_signature, state
            FROM mod_calendar_writeback
            WHERE consolidated_id = :cid AND provider_account_id = :aid
            """
        ),
        {"cid": cons_id, "aid": account_id},
    ).first()
    if row is None:
        return None
    return _WbRow(
        provider_event_id=(str(row[0]) if row[0] is not None else None),
        last_pushed_etag=(str(row[1]) if row[1] is not None else None),
        last_pushed_signature=(str(row[2]) if row[2] is not None else None),
        state=str(row[3]),
    )


def _signature(cv: _ConsView) -> str:
    return json.dumps(
        [
            cv.title,
            cv.starts_on.isoformat(),
            cv.ends_on.isoformat() if cv.ends_on else None,
            cv.start_time.isoformat() if cv.start_time else None,
            cv.end_time.isoformat() if cv.end_time else None,
            cv.location,
            cv.description,
        ],
        ensure_ascii=False,
    )


def _to_write(cv: _ConsView) -> ProviderEventWrite:
    return ProviderEventWrite(
        title=cv.title,
        starts_on=cv.starts_on,
        ends_on=cv.ends_on,
        start_time=cv.start_time,
        end_time=cv.end_time,
        location=cv.location,
        description=cv.description,
        memex_consolidated_id=str(cv.cons_id),
    )


def _upsert_wb(
    conn: Connection,
    user_id: int,
    cons_id: int,
    account_id: int,
    *,
    provider_event_id: str | None,
    etag: str | None,
    signature: str | None,
    state: str,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mod_calendar_writeback
              (user_id, consolidated_id, provider_account_id, provider_event_id,
               last_pushed_etag, last_pushed_signature, last_pushed_at, state)
            VALUES (:uid, :cid, :aid, :peid, :etag, :sig, NOW(), :state)
            ON CONFLICT (consolidated_id, provider_account_id) DO UPDATE SET
              provider_event_id = EXCLUDED.provider_event_id,
              last_pushed_etag = EXCLUDED.last_pushed_etag,
              last_pushed_signature = EXCLUDED.last_pushed_signature,
              last_pushed_at = NOW(), state = EXCLUDED.state, error = NULL
            """
        ),
        {
            "uid": user_id,
            "cid": cons_id,
            "aid": account_id,
            "peid": provider_event_id,
            "etag": etag,
            "sig": signature,
            "state": state,
        },
    )


async def run_push(
    user_id: int,
    account_id: int,
    *,
    client: CalendarProvider | None = None,
) -> PushStats:
    """Write-back (egress): empuja la vista CONSOLIDADA del user a una cuenta `write_back`.

    Propaga inter-proveedor (un evento de otra fuente aparece en esta cuenta) SIN duplicar lo que
    el usuario ya tiene ahí. Echo-suppression por FIRMA de contenido: si lo que memex ya pusheó no
    cambió, no re-escribe → no hay loop con el `pull` que reimporta el eco. `client` inyectable.
    """
    stats = PushStats()

    with connection() as conn:
        account = _load_account(conn, user_id, account_id)
    if account is None:
        _log.error("calendar.push.account_not_found", user_id=user_id, account_id=account_id)
        stats.errors += 1
        return stats
    if not account.enabled:
        _log.info("calendar.push.account_disabled", account_id=account_id)
        return stats

    with connection() as conn:
        write_back = bool(
            conn.execute(
                text("SELECT write_back FROM mod_calendar_provider_accounts WHERE id = :id"),
                {"id": account_id},
            ).scalar_one()
        )
        run_id = _start_run(conn, user_id, account_id, direction="egress")
        cons = _load_consolidated(conn, user_id)
    if not write_back:
        _log.info("calendar.push.not_write_back", account_id=account_id)
        with connection() as conn:
            _finish_run(conn, run_id, SyncStats(), status="ok")
        return stats

    owns_client = client is None
    active: CalendarProvider
    if client is not None:
        active = client
    else:
        token_path = oauth.resolve_token_path(account.provider, account.id, account.token_path_env)
        access = oauth.access_token(account.provider, token_path=token_path)
        active = resolve(account.provider)(
            CalendarSyncConfig.from_env(), access, account.calendar_id
        )

    _log.info("calendar.push.start", user_id=user_id, account_id=account_id, n=len(cons))
    try:
        for cv in cons:
            stats.consolidated += 1
            try:
                await _push_one(active, user_id, account_id, account.provider, run_id, cv, stats)
            except CalendarProviderError as e:
                stats.errors += 1
                _log.error(
                    "calendar.push.event_failed",
                    cons_id=cv.cons_id,
                    status=e.status_code,
                    msg=str(e),
                )
    finally:
        if owns_client and isinstance(active, GoogleCalendarClient):
            await active.aclose()

    with connection() as conn:
        _finish_run(
            conn,
            run_id,
            SyncStats(
                pulled=stats.consolidated,
                created=stats.created,
                modified=stats.updated,
                deleted=stats.deleted,
                unchanged=stats.skipped,
                errors=stats.errors,
            ),
            status="error" if stats.errors else "ok",
        )
    _log.info(
        "calendar.push.end",
        user_id=user_id,
        account_id=account_id,
        created=stats.created,
        updated=stats.updated,
        deleted=stats.deleted,
        skipped=stats.skipped,
        errors=stats.errors,
    )
    return stats


async def _push_one(
    client: CalendarProvider,
    user_id: int,
    account_id: int,
    provider: str,
    run_id: int,
    cv: _ConsView,
    stats: PushStats,
) -> None:
    """Empuja UN consolidado a la cuenta. Lee la decisión, hace la llamada de red, persiste el
    estado del write-back + la auditoría. Las firmas iguales se saltean (echo-suppression)."""
    with connection() as conn:
        if _account_has_user_event(conn, cv.cons_id, account_id):
            stats.skipped += 1
            return
        wb = _load_wb(conn, cv.cons_id, account_id)
    sig = _signature(cv)

    if cv.deleted:
        if wb is not None and wb.state != "deleted" and wb.provider_event_id:
            await client.delete_event(
                provider_event_id=wb.provider_event_id, etag=wb.last_pushed_etag
            )
            with connection() as conn:
                _upsert_wb(
                    conn,
                    user_id,
                    cv.cons_id,
                    account_id,
                    provider_event_id=wb.provider_event_id,
                    etag=None,
                    signature=sig,
                    state="deleted",
                )
                _record_change(
                    conn,
                    user_id=user_id,
                    run_id=run_id,
                    event_id=None,
                    consolidated_id=cv.cons_id,
                    provider=provider,
                    provider_event_id=wb.provider_event_id,
                    action="deleted",
                    direction="egress",
                )
            stats.deleted += 1
        else:
            stats.skipped += 1
        return

    needs_create = wb is None or wb.provider_event_id is None
    if needs_create:
        ref = await client.create_event(_to_write(cv))
        action = "created"
        stats.created += 1
    elif wb is not None and wb.state == "synced" and wb.last_pushed_signature == sig:
        stats.skipped += 1  # echo-suppression: el contenido no cambió → no re-escribe (anti-loop)
        return
    else:
        assert wb is not None and wb.provider_event_id is not None
        ref = await client.update_event(
            provider_event_id=wb.provider_event_id, etag=wb.last_pushed_etag, ev=_to_write(cv)
        )
        action = "modified"
        stats.updated += 1

    with connection() as conn:
        _upsert_wb(
            conn,
            user_id,
            cv.cons_id,
            account_id,
            provider_event_id=ref.provider_event_id,
            etag=ref.etag,
            signature=sig,
            state="synced",
        )
        _record_change(
            conn,
            user_id=user_id,
            run_id=run_id,
            event_id=None,
            consolidated_id=cv.cons_id,
            provider=provider,
            provider_event_id=ref.provider_event_id,
            action=action,
            direction="egress",
        )
