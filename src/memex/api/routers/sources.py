import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memex.api.auth import current_user_id
from memex.api.fetch_runner import run_fetch_window
from memex.api.schemas import (
    CheckpointBody,
    FetchResponse,
    SocialAccountAdd,
    SourceCreate,
    SourcePatch,
    SourceRow,
)
from memex.core import checkpoint
from memex.core.schedule import parse_duration
from memex.core.source import SourceKind
from memex.db import connection
from memex.ingestors.runner import RunStats
from memex.ingestors.social.config import normalize_account
from memex.logging import get_logger
from memex.sources import kind_for_type

router = APIRouter(prefix="/sources", tags=["sources"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.sources")

_SOURCE_SELECT = """
    SELECT s.id, s.user_id, s.name, s.type, s.enabled, s.config, s.created_at,
           s.account_id, s.fetch_schedule, a.alias AS account_alias
    FROM sources s LEFT JOIN accounts a ON a.id = s.account_id
    WHERE s.id = :sid
"""


def _assert_owns_source(conn: Any, user_id: int, source_id: int) -> None:
    owner = conn.execute(
        text("SELECT user_id FROM sources WHERE id = :sid"),
        {"sid": source_id},
    ).scalar()
    if owner != user_id:
        raise HTTPException(status_code=404, detail="source not found")


def _source_row(conn: Any, source_id: int) -> dict[str, Any]:
    row = conn.execute(text(_SOURCE_SELECT), {"sid": source_id}).mappings().first()
    assert row is not None
    return dict(row)


def _assert_social(source_type: str) -> None:
    try:
        kind = kind_for_type(source_type)
    except KeyError:
        kind = None
    if kind is not SourceKind.SOCIAL:
        raise HTTPException(
            status_code=422,
            detail=f"source type {source_type!r} no es social (solo instagram/facebook/x)",
        )


def _config_accounts(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in (cfg.get("accounts") or []) if isinstance(a, dict)]


@router.get("", response_model=list[SourceRow])
async def list_sources(user_id: UserID) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT s.id, s.user_id, s.name, s.type, s.enabled, s.config, s.created_at,
                           s.account_id, s.fetch_schedule, a.alias AS account_alias
                    FROM sources s LEFT JOIN accounts a ON a.id = s.account_id
                    WHERE s.user_id = :uid ORDER BY s.id
                    """
                ),
                {"uid": user_id},
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


@router.post("", response_model=SourceRow, status_code=status.HTTP_201_CREATED)
async def create_source(body: SourceCreate, user_id: UserID) -> dict[str, Any]:
    try:
        with connection() as conn:
            row = (
                conn.execute(
                    text(
                        """
                        INSERT INTO sources (user_id, name, type, config)
                        VALUES (:uid, :name, :type, CAST(:cfg AS JSONB))
                        RETURNING id, user_id, name, type, enabled, config, created_at
                        """
                    ),
                    {
                        "uid": user_id,
                        "name": body.name,
                        "type": body.type,
                        "cfg": json.dumps(body.config),
                    },
                )
                .mappings()
                .first()
            )
    except IntegrityError as e:
        raise HTTPException(
            status_code=409, detail="source with that name already exists for this user"
        ) from e
    assert row is not None
    _log.info(
        "sources.created",
        user_id=user_id,
        source_id=row["id"],
        name=row["name"],
        source_type=row["type"],
    )
    return dict(row)


@router.post("/ensure", response_model=SourceRow)
async def ensure_source(body: SourceCreate, user_id: UserID) -> dict[str, Any]:
    """Get-or-create idempotente por (user_id, name).

    Si la fuente ya existe para este usuario con ese nombre, la devuelve sin
    tocarla. Si no existe, la crea con `type` y `config` provistos.
    """
    with connection() as conn:
        existing = (
            conn.execute(
                text(
                    """
                    SELECT id, user_id, name, type, enabled, config, created_at
                    FROM sources WHERE user_id = :uid AND name = :name
                    """
                ),
                {"uid": user_id, "name": body.name},
            )
            .mappings()
            .first()
        )
        if existing is not None:
            _log.info(
                "sources.ensured",
                user_id=user_id,
                source_id=existing["id"],
                name=existing["name"],
                source_type=existing["type"],
                action="existed",
            )
            return dict(existing)
        row = (
            conn.execute(
                text(
                    """
                    INSERT INTO sources (user_id, name, type, config)
                    VALUES (:uid, :name, :type, CAST(:cfg AS JSONB))
                    RETURNING id, user_id, name, type, enabled, config, created_at
                    """
                ),
                {
                    "uid": user_id,
                    "name": body.name,
                    "type": body.type,
                    "cfg": json.dumps(body.config),
                },
            )
            .mappings()
            .first()
        )
    assert row is not None
    _log.info(
        "sources.ensured",
        user_id=user_id,
        source_id=row["id"],
        name=row["name"],
        source_type=row["type"],
        action="created",
    )
    return dict(row)


@router.patch("/{source_id}", response_model=SourceRow)
async def patch_source(source_id: int, body: SourcePatch, user_id: UserID) -> dict[str, Any]:
    """Edición parcial: cuenta (`account_id`), toggle `enabled` y/o `fetch_schedule`.

    `fetch_schedule` como string se valida con `parse_duration` (422 si es ISO-8601 inválido);
    `null` limpia el agendado; ausente no lo toca. El daemon `memex-ingest-scheduler` relee esta
    columna cada tick.
    """
    fields = body.model_dump(exclude_unset=True)
    sets: list[str] = []
    params: dict[str, Any] = {"sid": source_id}
    with connection() as conn:
        _assert_owns_source(conn, user_id, source_id)
        if "account_id" in fields:
            target = fields["account_id"]
            if target is not None:
                owner = conn.execute(
                    text("SELECT user_id FROM accounts WHERE id = :aid"), {"aid": target}
                ).scalar()
                if owner != user_id:
                    raise HTTPException(status_code=404, detail="account not found")
            sets.append("account_id = :account_id")
            params["account_id"] = target
        if "enabled" in fields:
            sets.append("enabled = :enabled")
            params["enabled"] = fields["enabled"]
        if "fetch_schedule" in fields:
            schedule = fields["fetch_schedule"]
            if schedule is not None:
                try:
                    parse_duration(str(schedule))
                except ValueError as e:
                    raise HTTPException(
                        status_code=422,
                        detail=f"fetch_schedule inválido (ISO-8601, ej. PT1H): {schedule!r}",
                    ) from e
            sets.append("fetch_schedule = :fetch_schedule")
            params["fetch_schedule"] = schedule
        if sets:
            conn.execute(text(f"UPDATE sources SET {', '.join(sets)} WHERE id = :sid"), params)
        row = _source_row(conn, source_id)
    _log.info("sources.patched", user_id=user_id, source_id=source_id, fields=list(fields.keys()))
    return row


@router.post("/{source_id}/social/accounts", response_model=SourceRow)
async def add_social_account(
    source_id: int, body: SocialAccountAdd, user_id: UserID
) -> dict[str, Any]:
    """Agrega una cuenta seguida al allowlist (`config.accounts`) de una source social.

    Normaliza el handle con la misma función del ingestor (para que matchee la key del
    cursor). 409 si ya está; 422 si la source no es social o el handle queda vacío.
    """
    handle = normalize_account(body.handle)
    if not handle:
        raise HTTPException(status_code=422, detail="handle vacío tras normalizar")
    with connection() as conn:
        _assert_owns_source(conn, user_id, source_id)
        current = _source_row(conn, source_id)
        _assert_social(str(current["type"]))
        cfg = dict(current["config"] or {})
        accounts = _config_accounts(cfg)
        if any(normalize_account(str(a.get("account", ""))) == handle for a in accounts):
            raise HTTPException(status_code=409, detail=f"la cuenta {handle!r} ya está en la lista")
        accounts.append({"account": handle, "priority": bool(body.priority)})
        cfg["accounts"] = accounts
        conn.execute(
            text("UPDATE sources SET config = CAST(:cfg AS JSONB) WHERE id = :sid"),
            {"cfg": json.dumps(cfg), "sid": source_id},
        )
        row = _source_row(conn, source_id)
    _log.info("sources.social.account_added", user_id=user_id, source_id=source_id, account=handle)
    return row


@router.delete("/{source_id}/social/accounts/{handle}", response_model=SourceRow)
async def remove_social_account(source_id: int, handle: str, user_id: UserID) -> dict[str, Any]:
    """Quita una cuenta seguida del allowlist (`config.accounts`). 404 si no estaba."""
    target = normalize_account(handle)
    with connection() as conn:
        _assert_owns_source(conn, user_id, source_id)
        current = _source_row(conn, source_id)
        _assert_social(str(current["type"]))
        cfg = dict(current["config"] or {})
        accounts = _config_accounts(cfg)
        kept = [a for a in accounts if normalize_account(str(a.get("account", ""))) != target]
        if len(kept) == len(accounts):
            raise HTTPException(status_code=404, detail=f"la cuenta {target!r} no está en la lista")
        cfg["accounts"] = kept
        conn.execute(
            text("UPDATE sources SET config = CAST(:cfg AS JSONB) WHERE id = :sid"),
            {"cfg": json.dumps(cfg), "sid": source_id},
        )
        row = _source_row(conn, source_id)
    _log.info(
        "sources.social.account_removed", user_id=user_id, source_id=source_id, account=target
    )
    return row


@router.get("/{source_id}/checkpoint")
async def get_checkpoint(source_id: int, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        _assert_owns_source(conn, user_id, source_id)
        cur = checkpoint.get_cursor(conn, source_id)
    return {"cursor": cur}


@router.put("/{source_id}/checkpoint")
async def put_checkpoint(source_id: int, body: CheckpointBody, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        _assert_owns_source(conn, user_id, source_id)
        checkpoint.save_cursor(conn, source_id, body.cursor)
    _log.info(
        "sources.checkpoint.updated",
        user_id=user_id,
        source_id=source_id,
    )
    return {"cursor": body.cursor}


def _stats_response(stats: RunStats, *, dry_run: bool) -> dict[str, Any]:
    return {
        "posted": stats.posted,
        "inserted": stats.inserted,
        "duplicates": stats.duplicates,
        "errors": stats.errors,
        "filtered": stats.filtered,
        "dry_run": dry_run,
        "ms_elapsed": stats.ms_elapsed,
    }


@router.post("/{source_id}/fetch", response_model=FetchResponse)
async def fetch_source(
    source_id: int,
    user_id: UserID,
    dry_run: Annotated[bool, Query()] = False,
    mode: Annotated[str, Query()] = "incremental",
    since: Annotated[str | None, Query(description="range: YYYY-MM-DD inclusive")] = None,
    until: Annotated[str | None, Query(description="range: YYYY-MM-DD exclusiva")] = None,
    limit: Annotated[
        int | None, Query(ge=1, le=1000, description="last/range: tope de mensajes")
    ] = None,
) -> dict[str, Any]:
    """Dispara una corrida de ingesta a demanda DENTRO del proceso API (sin CLI).

    Corre `run_ingestor` en un threadpool (es sync + I/O bloqueante). En `dry_run` cuenta
    nuevos/duplicados/filtrados sin escribir. Modos:
      - `incremental`: trae lo nuevo desde el checkpoint y lo AVANZA.
      - `range`: ventana `since`..`until` (backfill). NO toca el checkpoint.
      - `last`: los `limit` más recientes (backfill). NO toca el checkpoint.
    """
    if mode not in ("incremental", "range", "last"):
        raise HTTPException(status_code=422, detail=f"mode {mode!r} inválido")
    if mode == "range" and not since:
        raise HTTPException(status_code=422, detail="mode 'range' requiere el parámetro 'since'")

    with connection() as conn:
        _assert_owns_source(conn, user_id, source_id)
        row = (
            conn.execute(
                text("SELECT type, config, account_id FROM sources WHERE id = :sid"),
                {"sid": source_id},
            )
            .mappings()
            .first()
        )
        assert row is not None
        source_type = str(row["type"])
        cfg = dict(row["config"] or {})
        account_id = row["account_id"]

    # El camino resolve → sink → runner vive en `run_fetch_window` (lo comparte el backfill).
    stats = await run_fetch_window(
        user_id=user_id,
        source_id=source_id,
        source_type=source_type,
        cfg=cfg,
        account_id=account_id,
        mode=mode,
        since=since,
        until=until,
        limit=limit,
        dry_run=dry_run,
        trigger="manual",
    )
    return _stats_response(stats, dry_run=dry_run)
