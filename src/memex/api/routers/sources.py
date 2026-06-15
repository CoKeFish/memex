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
from memex.security import vault
from memex.sources import fetch_mode_caveats_for_type, fetch_modes_for_type, kind_for_type

router = APIRouter(prefix="/sources", tags=["sources"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.sources")

# Piso del intervalo de agendado: por debajo de esto un fetch_schedule martillaría la fuente (y
# gastaría API de paga en redes). El daemon además tiene su tick; esto corta el vector por API.
MIN_FETCH_INTERVAL_S = 60.0

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


#: Tipos cuyo token de Apify se reporta en `SourceRow.token_source`.
_APIFY_TYPES = ("instagram", "facebook", "x")


def _token_source(conn: Any, row: dict[str, Any]) -> str | None:
    """De dónde resuelve el token de Apify una fuente social (sin descifrar nada).

    "vault" = la cuenta vinculada tiene el secreto cifrado; "missing" = no hay credencial en el
    vault → el fetch fallará (el `.env` ya NO es fuente de credenciales). None para tipos sin token
    reportable. Presencia en el vault ≈ resolverá: si falta la master key el resolver no inyecta y
    lo loguea, acá solo se reporta estado.
    """
    if row.get("type") not in _APIFY_TYPES:
        return None
    account_id = row.get("account_id")
    if account_id is not None:
        names = {str(s["secret_name"]) for s in vault.list_secret_status(conn, account_id)}
        if "apify_token" in names:
            return "vault"
    return "missing"


def _capabilities(conn: Any, row: dict[str, Any]) -> dict[str, Any]:
    """Campos derivados de SourceRow que la UI consume (token + modos honrados + avisos + kind)."""
    stype = str(row.get("type") or "")
    try:
        kind: str | None = kind_for_type(stype).value
    except KeyError:
        kind = None
    return {
        "token_source": _token_source(conn, row),
        "fetch_modes": fetch_modes_for_type(stype),
        "mode_caveats": fetch_mode_caveats_for_type(stype),
        "kind": kind,
    }


def _source_row(conn: Any, source_id: int) -> dict[str, Any]:
    row = conn.execute(text(_SOURCE_SELECT), {"sid": source_id}).mappings().first()
    assert row is not None
    out = dict(row)
    out.update(_capabilities(conn, out))
    return out


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
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d.update(_capabilities(conn, d))
            out.append(d)
    return out


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
            assert row is not None
            out = dict(row)
            out.update(_capabilities(conn, out))
    except IntegrityError as e:
        raise HTTPException(
            status_code=409, detail="source with that name already exists for this user"
        ) from e
    _log.info(
        "sources.created",
        user_id=user_id,
        source_id=out["id"],
        name=out["name"],
        source_type=out["type"],
    )
    return out


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
            out = dict(existing)
            out.update(_capabilities(conn, out))
            return out
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
        out = dict(row)
        out.update(_capabilities(conn, out))
    _log.info(
        "sources.ensured",
        user_id=user_id,
        source_id=out["id"],
        name=out["name"],
        source_type=out["type"],
        action="created",
    )
    return out


@router.patch("/{source_id}", response_model=SourceRow)
async def patch_source(source_id: int, body: SourcePatch, user_id: UserID) -> dict[str, Any]:
    """Edición parcial: `account_id`, `enabled`, `fetch_schedule`, `name` y/o `config`.

    `fetch_schedule` como string se valida con `parse_duration` (422 si es ISO-8601 inválido);
    `null` limpia el agendado; ausente no lo toca. El daemon `memex-ingest-scheduler` relee esta
    columna cada tick. `name`/`config` permiten corregir una source mal configurada sin recrearla
    (409 si el nuevo `name` colisiona con otra fuente del usuario).
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
                    interval = parse_duration(str(schedule))
                except ValueError as e:
                    raise HTTPException(
                        status_code=422,
                        detail=f"fetch_schedule inválido (ISO-8601, ej. PT1H): {schedule!r}",
                    ) from e
                if interval < MIN_FETCH_INTERVAL_S:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"fetch_schedule mínimo {MIN_FETCH_INTERVAL_S:.0f}s — un intervalo más "
                            "corto martillaría la fuente (y gasta API de paga en redes)"
                        ),
                    )
            sets.append("fetch_schedule = :fetch_schedule")
            params["fetch_schedule"] = schedule
        if fields.get("name") is not None:
            sets.append("name = :name")
            params["name"] = fields["name"]
        if fields.get("config") is not None:
            sets.append("config = CAST(:config AS JSONB)")
            params["config"] = json.dumps(fields["config"])
        if sets:
            try:
                conn.execute(text(f"UPDATE sources SET {', '.join(sets)} WHERE id = :sid"), params)
            except IntegrityError as e:
                raise HTTPException(
                    status_code=409, detail="source with that name already exists for this user"
                ) from e
        row = _source_row(conn, source_id)
    _log.info("sources.patched", user_id=user_id, source_id=source_id, fields=list(fields.keys()))
    return row


@router.get("/{source_id}", response_model=SourceRow)
async def get_source(source_id: int, user_id: UserID) -> dict[str, Any]:
    """Lectura individual de una source por id (la lista `GET /sources` trae todas)."""
    with connection() as conn:
        _assert_owns_source(conn, user_id, source_id)
        return _source_row(conn, source_id)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: int,
    user_id: UserID,
    cascade: Annotated[bool, Query(description="confirma el borrado del inbox/historial")] = False,
) -> None:
    """Borra una source. Las FKs ON DELETE CASCADE arrastran inbox/checkpoints/runs/dedupe_keys.

    Guard 409 si la fuente ya ingirió algo (evita perder el historial sin querer): reintentá con
    `?cascade=true` para confirmar el borrado destructivo.
    """
    with connection() as conn:
        _assert_owns_source(conn, user_id, source_id)
        if not cascade:
            n = conn.execute(
                text("SELECT COUNT(*) FROM inbox WHERE source_id = :sid"), {"sid": source_id}
            ).scalar()
            if n:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"la fuente tiene {n} registros en inbox; borrarla arrastra su historial. "
                        "Reintentá con ?cascade=true para confirmar."
                    ),
                )
        conn.execute(text("DELETE FROM sources WHERE id = :sid"), {"sid": source_id})
    _log.info("sources.deleted", user_id=user_id, source_id=source_id, cascade=cascade)


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


@router.delete("/{source_id}/checkpoint", status_code=status.HTTP_204_NO_CONTENT)
async def delete_checkpoint(source_id: int, user_id: UserID) -> None:
    """Resetea el cursor: borra la fila de source_checkpoints → el próximo fetch incremental
    arranca desde cero. El dedup UNIQUE(source_id, external_id) evita re-guardar lo ya ingerido."""
    with connection() as conn:
        _assert_owns_source(conn, user_id, source_id)
        conn.execute(
            text("DELETE FROM source_checkpoints WHERE source_id = :sid"), {"sid": source_id}
        )
    _log.info("sources.checkpoint.reset", user_id=user_id, source_id=source_id)


def _stats_response(stats: RunStats, *, dry_run: bool) -> dict[str, Any]:
    return {
        "posted": stats.posted,
        "inserted": stats.inserted,
        "duplicates": stats.duplicates,
        "errors": stats.errors,
        "filtered": stats.filtered,
        "dry_run": dry_run,
        "ms_elapsed": stats.ms_elapsed,
        "api_cost_usd": stats.api_cost_usd,
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
    accounts: Annotated[
        list[str] | None,
        Query(description="social: traer SOLO estas cuentas seguidas (subset de la allowlist)"),
    ] = None,
) -> dict[str, Any]:
    """Dispara una corrida de ingesta a demanda DENTRO del proceso API (sin CLI).

    Corre `run_ingestor` en un threadpool (es sync + I/O bloqueante). En `dry_run` cuenta
    nuevos/duplicados/filtrados sin escribir. Modos:
      - `incremental`: trae lo nuevo desde el checkpoint y lo AVANZA.
      - `range`: ventana `since`..`until` (backfill). NO toca el checkpoint.
      - `last`: los `limit` más recientes (backfill). NO toca el checkpoint.

    `accounts` (solo redes) restringe la corrida a esas cuentas seguidas: una corrida de actor por
    cuenta pedida en vez de toda la allowlist. El cursor social es por-cuenta, así que el avance de
    las cuentas no pedidas queda intacto.
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

    # Un modo que el ingestor no honra fallaría SILENCIOSO (correría incremental disfrazado):
    # mejor 422 explícito. `incremental` pasa siempre (cualquier tipo resolvible lo corre).
    if mode != "incremental" and mode not in fetch_modes_for_type(source_type):
        raise HTTPException(
            status_code=422,
            detail=f"la fuente tipo {source_type!r} no soporta mode={mode!r}",
        )

    if accounts:
        _assert_social(source_type)
        allow = {normalize_account(str(a.get("account", ""))): a for a in _config_accounts(cfg)}
        wanted: list[str] = []
        for raw in accounts:
            h = normalize_account(raw)
            if h not in allow:
                raise HTTPException(
                    status_code=422,
                    detail=f"la cuenta {raw!r} no está en la allowlist de esta fuente",
                )
            if h not in wanted:
                wanted.append(h)
        # Override transitorio (igual que fetch_mode): NO se persiste en sources.config.
        cfg["accounts"] = [allow[h] for h in wanted]
        _log.info(
            "sources.fetch.account_subset",
            user_id=user_id,
            source_id=source_id,
            accounts=wanted,
        )

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
