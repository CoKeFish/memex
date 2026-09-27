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

from memex.api.auth import current_user_id
from memex.api.schemas import (
    AccountCreate,
    AccountPatch,
    AccountRow,
    CredentialSet,
    CredentialStatus,
    HealthCheckResponse,
)
from memex.core import account_health
from memex.core.source import SourceConfigError
from memex.db import connection
from memex.logging import get_logger
from memex.security import crypto, vault

router = APIRouter(prefix="/accounts", tags=["accounts"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.accounts")

_ACCOUNT_COLS = (
    "id, user_id, alias, provider, kind, metadata, enabled, "
    "health_status, last_health_check_at, health_detail, health_code, created_at"
)


def _assert_owns_account(conn: Any, user_id: int, account_id: int) -> None:
    owner = conn.execute(
        text("SELECT user_id FROM accounts WHERE id = :aid"), {"aid": account_id}
    ).scalar()
    if owner != user_id:
        raise HTTPException(status_code=404, detail="account not found")


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

    # Las credenciales de ingestor SOLO vienen del vault (el `.env` ya no es fuente): se reportan
    # las que están cifradas en la cuenta; lo que falte se muestra como "falta" hasta cargarlo.
    secrets: list[dict[str, Any]] = [
        {**s, "source": "vault"} for s in vault.list_secret_status(conn, account_id)
    ]
    if account_health.linked_source(conn, account_id) is not None:
        eh = account_health.effective_health(
            conn, account_id, str(data["health_status"]), data["last_health_check_at"]
        )
        data["health_status"], data["health_basis"] = eh.status, eh.basis
        data["health_evidence_at"], data["health_stale"] = eh.evidence_at, eh.stale
    else:
        at = data["last_health_check_at"]
        data["health_basis"] = "check" if at is not None else "none"
        data["health_evidence_at"] = at
        data["health_stale"] = account_health.effective_health(
            conn, account_id, str(data["health_status"]), at
        ).stale
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


@router.post("/{account_id}/health-check", response_model=HealthCheckResponse)
async def health_check(account_id: int, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        _assert_owns_account(conn, user_id, account_id)
    try:
        result = await account_health.check_account(account_id, user_id)
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
    return {
        "status": result.status,
        "detail": result.detail,
        "checked_at": result.checked_at,
        "code": result.code,
    }
