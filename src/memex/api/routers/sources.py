import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from memex import sources as source_registry
from memex.api.auth import current_user_id
from memex.api.inprocess_sink import DryRunSink, InProcessSink
from memex.api.schemas import (
    CheckpointBody,
    FetchResponse,
    SocialAccountAdd,
    SourceCreate,
    SourcePatch,
    SourceRow,
)
from memex.core import checkpoint
from memex.core.observability import ingestion_run
from memex.core.sink import MemexSink
from memex.core.source import SourceConfigError, SourceKind
from memex.db import connection
from memex.ingestors.runner import RunStats, run_ingestor
from memex.ingestors.social.config import normalize_account
from memex.logging import get_logger
from memex.sources import kind_for_type
from memex.sources.resolver import build_resolved_env

router = APIRouter(prefix="/sources", tags=["sources"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.sources")

_SOURCE_SELECT = """
    SELECT s.id, s.user_id, s.name, s.type, s.enabled, s.config, s.created_at,
           s.account_id, a.alias AS account_alias
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
                           s.account_id, a.alias AS account_alias
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
    """Edición parcial: vincular/desvincular a una cuenta (`account_id`) y/o togglear `enabled`."""
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

        # Override transitorio de la ventana de fetch (no se persiste en sources.config).
        cfg = dict(row["config"] or {})
        if mode != "incremental":
            cfg["fetch_mode"] = mode
            if since:
                cfg["fetch_since"] = since
            if until:
                cfg["fetch_until"] = until
            if limit is not None:
                cfg["fetch_limit"] = limit

        # Inyecta los secretos del vault de la cuenta (si hay) bajo el nombre de su env var.
        # Usa la master key del servidor → funciona sin sesión. Fallback a os.environ si no hay.
        resolved_env = build_resolved_env(
            conn,
            user_id=user_id,
            source_type=source_type,
            cfg=cfg,
            account_id=row["account_id"],
        )

    try:
        factory = source_registry.resolve(source_type)
    except KeyError as e:
        raise HTTPException(
            status_code=422,
            detail=f"source type {source_type!r} no se puede traer desde el server (sin ingestor)",
        ) from e
    try:
        source = factory(cfg, env=resolved_env)
    except SourceConfigError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    _log.info(
        "fetch.requested",
        user_id=user_id,
        source_id=source_id,
        dry_run=dry_run,
        mode=mode,
        since=since,
        until=until,
        limit=limit,
    )

    if dry_run:
        dry_sink: MemexSink = DryRunSink(user_id)
        stats = await run_in_threadpool(run_ingestor, source, source_id, dry_sink, chunk_sleep_ms=0)
        return _stats_response(stats, dry_run=True)

    # range/last son backfills: insertan pero no avanzan el cursor incremental.
    sink: MemexSink = InProcessSink(user_id, persist_checkpoint=(mode == "incremental"))
    with ingestion_run(user_id=user_id, source_id=source_id, trigger="dashboard") as run:
        try:
            stats = await run_in_threadpool(run_ingestor, source, source_id, sink, chunk_sleep_ms=0)
            run.finalize(stats)
        except Exception as e:
            run.fail(e)
            raise HTTPException(status_code=502, detail=f"fetch falló: {e}") from e
    return _stats_response(stats, dry_run=False)
