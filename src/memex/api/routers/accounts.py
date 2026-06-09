"""Gestión de cuentas y credenciales desde el dashboard.

Una `account` agrupa una o más `sources` y guarda sus credenciales CIFRADAS (vault). El API nunca
devuelve el plaintext de un secreto: solo `configured` + `last4`. El health-check instancia el
`Source` con las credenciales descifradas (server-side, master key) y corre `health_check()`.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memex import sources as source_registry
from memex.api.auth import current_user_id
from memex.api.schemas import (
    AccountCreate,
    AccountPatch,
    AccountRow,
    CredentialSet,
    CredentialStatus,
    HealthCheckResponse,
)
from memex.core.source import HealthResult, SourceConfigError
from memex.db import connection
from memex.logging import get_logger
from memex.security import crypto, vault
from memex.sources.resolver import build_resolved_env, env_satisfied_secrets

router = APIRouter(prefix="/accounts", tags=["accounts"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.accounts")

_ACCOUNT_COLS = (
    "id, user_id, alias, provider, kind, metadata, enabled, "
    "health_status, last_health_check_at, created_at"
)


def _assert_owns_account(conn: Any, user_id: int, account_id: int) -> None:
    owner = conn.execute(
        text("SELECT user_id FROM accounts WHERE id = :aid"), {"aid": account_id}
    ).scalar()
    if owner != user_id:
        raise HTTPException(status_code=404, detail="account not found")


def _linked_source(conn: Any, account_id: int) -> tuple[str, dict[str, Any]] | None:
    """Tipo + config de la primera source vinculada a la cuenta (para resolver env / salud)."""
    src = (
        conn.execute(
            text("SELECT type, config FROM sources WHERE account_id = :aid ORDER BY id LIMIT 1"),
            {"aid": account_id},
        )
        .mappings()
        .first()
    )
    if src is None:
        return None
    return str(src["type"]), dict(src["config"] or {})


def _effective_health(conn: Any, account_id: int, stored: str) -> str:
    """Salud que REFLEJA la realidad: deriva del último `ingestion_run` de las sources de la cuenta
    (ok→healthy, failed/aborted→unhealthy). Sin runs (o 'running'), conserva el estado guardado por
    el health-check manual. Así una fuente que ACABA de ingestar no aparece 'unhealthy' (H-11)."""
    status = conn.execute(
        text(
            """
            SELECT ir.status FROM ingestion_runs ir
            JOIN sources s ON s.id = ir.source_id
            WHERE s.account_id = :aid
            ORDER BY ir.started_at DESC
            LIMIT 1
            """
        ),
        {"aid": account_id},
    ).scalar()
    if status == "ok":
        return "healthy"
    if status in ("failed", "aborted"):
        return "unhealthy"
    return stored


def _account_row(conn: Any, account_id: int) -> dict[str, Any]:
    row = (
        conn.execute(
            text(f"SELECT {_ACCOUNT_COLS} FROM accounts WHERE id = :aid"),
            {"aid": account_id},
        )
        .mappings()
        .first()
    )
    assert row is not None
    data = dict(row)

    # Vault primero; luego suma los secretos que resuelven por entorno (no en el vault) como
    # configurados "vía env" — así el panel no marca "FALTA" lo que funciona por env (H-11).
    secrets: list[dict[str, Any]] = [
        {**s, "source": "vault"} for s in vault.list_secret_status(conn, account_id)
    ]
    linked = _linked_source(conn, account_id)
    if linked is not None:
        source_type, cfg = linked
        have = {str(s["secret_name"]) for s in secrets}
        for name in sorted(env_satisfied_secrets(source_type, cfg)):
            if name not in have:
                secrets.append(
                    {"secret_name": name, "configured": True, "last4": "", "source": "env"}
                )
        data["health_status"] = _effective_health(conn, account_id, str(data["health_status"]))
    data["secrets"] = secrets
    return data


@router.get("", response_model=list[AccountRow])
async def list_accounts(user_id: UserID) -> list[dict[str, Any]]:
    with connection() as conn:
        ids = (
            conn.execute(
                text("SELECT id FROM accounts WHERE user_id = :uid ORDER BY id"),
                {"uid": user_id},
            )
            .scalars()
            .all()
        )
        return [_account_row(conn, int(i)) for i in ids]


@router.post("", response_model=AccountRow, status_code=status.HTTP_201_CREATED)
async def create_account(body: AccountCreate, user_id: UserID) -> dict[str, Any]:
    try:
        with connection() as conn:
            aid = conn.execute(
                text(
                    """
                    INSERT INTO accounts (user_id, alias, provider, kind, metadata)
                    VALUES (:uid, :alias, :provider, :kind, CAST(:meta AS JSONB))
                    RETURNING id
                    """
                ),
                {
                    "uid": user_id,
                    "alias": body.alias,
                    "provider": body.provider,
                    "kind": body.kind,
                    "meta": json.dumps(body.metadata),
                },
            ).scalar()
            assert aid is not None
            row = _account_row(conn, int(aid))
    except IntegrityError as e:
        raise HTTPException(status_code=409, detail="ya existe una cuenta con ese alias") from e
    _log.info("accounts.created", user_id=user_id, account_id=row["id"])
    return row


@router.patch("/{account_id}", response_model=AccountRow)
async def patch_account(account_id: int, body: AccountPatch, user_id: UserID) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    sets: list[str] = []
    params: dict[str, Any] = {"aid": account_id}
    if "alias" in fields:
        sets.append("alias = :alias")
        params["alias"] = fields["alias"]
    if "enabled" in fields:
        sets.append("enabled = :enabled")
        params["enabled"] = fields["enabled"]
    if "metadata" in fields:
        sets.append("metadata = CAST(:meta AS JSONB)")
        params["meta"] = json.dumps(fields["metadata"])
    with connection() as conn:
        _assert_owns_account(conn, user_id, account_id)
        if sets:
            try:
                conn.execute(text(f"UPDATE accounts SET {', '.join(sets)} WHERE id = :aid"), params)
            except IntegrityError as e:
                raise HTTPException(status_code=409, detail="alias duplicado") from e
        row = _account_row(conn, account_id)
    return row


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: int,
    user_id: UserID,
    cascade: Annotated[bool, Query(description="desvincula sources en vez de bloquear")] = False,
) -> Response:
    with connection() as conn:
        _assert_owns_account(conn, user_id, account_id)
        linked = conn.execute(
            text("SELECT COUNT(*) FROM sources WHERE account_id = :aid"), {"aid": account_id}
        ).scalar()
        if linked and not cascade:
            raise HTTPException(
                status_code=409,
                detail="hay sources usando esta cuenta; usá ?cascade=true para desvincularlas",
            )
        # ON DELETE SET NULL desvincula las sources; ON DELETE CASCADE borra los secretos.
        conn.execute(text("DELETE FROM accounts WHERE id = :aid"), {"aid": account_id})
    _log.info("accounts.deleted", user_id=user_id, account_id=account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{account_id}/credentials", response_model=CredentialStatus)
async def set_credential(account_id: int, body: CredentialSet, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        _assert_owns_account(conn, user_id, account_id)
        try:
            last4 = vault.set_secret(conn, account_id, body.secret_name, body.value)
        except crypto.MasterKeyMissingError as e:
            raise HTTPException(
                status_code=503, detail="vault no configurado: falta MEMEX_SECRET_KEY"
            ) from e
        except vault.UserVaultMissingError as e:
            raise HTTPException(
                status_code=409, detail="tu vault no está provisionado (registrate/logueate)"
            ) from e
    return {"secret_name": body.secret_name, "configured": True, "last4": last4, "source": "vault"}


@router.delete("/{account_id}/credentials/{secret_name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(account_id: int, secret_name: str, user_id: UserID) -> Response:
    with connection() as conn:
        _assert_owns_account(conn, user_id, account_id)
        vault.delete_secret(conn, account_id, secret_name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _perform_health_check(account_id: int, user_id: int) -> HealthResult | None:
    """Instancia el Source vinculado con las creds descifradas, corre `health_check()` y persiste el
    estado. Devuelve None si la cuenta no tiene source vinculada. Asume ownership ya verificado.
    Levanta KeyError (tipo sin ingestor) / SourceConfigError (config inválida)."""
    with connection() as conn:
        src = (
            conn.execute(
                text(
                    "SELECT type, config FROM sources WHERE account_id = :aid ORDER BY id LIMIT 1"
                ),
                {"aid": account_id},
            )
            .mappings()
            .first()
        )
        if src is None:
            return None
        source_type = str(src["type"])
        cfg = dict(src["config"] or {})
        resolved_env = build_resolved_env(
            conn, user_id=user_id, source_type=source_type, cfg=cfg, account_id=account_id
        )

    factory = source_registry.resolve(source_type)
    source = factory(cfg, env=resolved_env)
    result = await source.health_check()

    with connection() as conn:
        conn.execute(
            text(
                "UPDATE accounts SET health_status = :s, last_health_check_at = NOW() "
                "WHERE id = :aid"
            ),
            {"s": result.status, "aid": account_id},
        )
    _log.info("accounts.health_check", user_id=user_id, account_id=account_id, status=result.status)
    return result


@router.post("/{account_id}/health-check", response_model=HealthCheckResponse)
async def health_check(account_id: int, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        _assert_owns_account(conn, user_id, account_id)
    try:
        result = await _perform_health_check(account_id, user_id)
    except KeyError as e:
        raise HTTPException(
            status_code=422, detail="tipo de source sin ingestor server-side"
        ) from e
    except SourceConfigError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if result is None:
        raise HTTPException(
            status_code=422, detail="vinculá una source a la cuenta para validar las credenciales"
        )
    return {"status": result.status, "detail": result.detail, "checked_at": result.checked_at}
