"""Endpoints de autenticación: signup / login / logout / me / change-password.

La contraseña solo autoriza el dashboard (Argon2id). NO deriva ninguna llave de cifrado: el vault
se descifra con la master key del servidor, así que la sesión es independiente del vault y el reset
de contraseña es lossless. La sesión viaja en una cookie httpOnly (token opaco).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memex.api.auth import current_user_id
from memex.api.schemas import ChangePasswordRequest, LoginRequest, MeResponse, SignupRequest
from memex.config import settings
from memex.db import connection
from memex.logging import get_logger
from memex.security import crypto, sessions, vault

router = APIRouter(prefix="/auth", tags=["auth"])

UserID = Annotated[int, Depends(current_user_id)]
SessionCookie = Annotated[str | None, Cookie(alias=settings.cookie_name)]

_log = get_logger("memex.api.auth.router")

# Hash dummy para igualar el tiempo de verificación cuando el email no existe (anti-enumeración).
_DUMMY_HASH = crypto.hash_password("memex-dummy-password-for-constant-time")


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=cast("Literal['lax', 'strict', 'none']", settings.cookie_samesite.lower()),
        path="/",
    )


def _me_payload(user_id: int, email: str, display_name: str | None) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "email": email,
        "display_name": display_name,
        "auth_enforced": settings.auth_enforced,
    }


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    ua = request.headers.get("user-agent")
    ip = request.client.host if request.client else None
    return ua, ip


@router.post("/signup", response_model=MeResponse, status_code=status.HTTP_201_CREATED)
async def signup(body: SignupRequest, request: Request, response: Response) -> dict[str, Any]:
    email = body.email.strip().lower()
    if "@" not in email or "." not in email:
        raise HTTPException(status_code=422, detail="email inválido")
    ua, ip = _client_meta(request)
    try:
        with connection() as conn:
            uid = conn.execute(
                text("INSERT INTO users (email, display_name) VALUES (:e, :d) RETURNING id"),
                {"e": email, "d": body.display_name},
            ).scalar()
            assert uid is not None
            user_id = int(uid)
            try:
                vault.provision_user(conn, user_id, body.password)
            except crypto.MasterKeyMissingError as e:
                raise HTTPException(
                    status_code=503, detail="vault no configurado: falta MEMEX_SECRET_KEY"
                ) from e
            token = sessions.create_session(conn, user_id, user_agent=ua, client_ip=ip)
    except IntegrityError as e:
        raise HTTPException(status_code=409, detail="ese email ya está registrado") from e
    _set_session_cookie(response, token)
    _log.info("auth.signup", user_id=user_id)
    return _me_payload(user_id, email, body.display_name)


@router.post("/login", response_model=MeResponse)
async def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    email = body.email.strip().lower()
    ua, ip = _client_meta(request)
    with connection() as conn:
        row = (
            conn.execute(
                text("SELECT id, display_name FROM users WHERE email = :e"),
                {"e": email},
            )
            .mappings()
            .first()
        )
        if row is None:
            crypto.verify_password(body.password, _DUMMY_HASH)  # tiempo constante anti-enumeración
            _log.info("auth.login.rejected", reason="no_user")
            raise HTTPException(status_code=401, detail="credenciales inválidas")
        user_id = int(row["id"])
        if not vault.verify_user_password(conn, user_id, body.password):
            _log.info("auth.login.rejected", reason="bad_password", user_id=user_id)
            raise HTTPException(status_code=401, detail="credenciales inválidas")
        token = sessions.create_session(conn, user_id, user_agent=ua, client_ip=ip)
    _set_session_cookie(response, token)
    _log.info("auth.login", user_id=user_id)
    return _me_payload(user_id, email, row["display_name"])


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(session_cookie: SessionCookie = None) -> Response:
    if session_cookie:
        with connection() as conn:
            sessions.revoke_session(conn, session_cookie)
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    resp.delete_cookie(key=settings.cookie_name, path="/")
    return resp


@router.get("/me", response_model=MeResponse)
async def me(user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        row = (
            conn.execute(
                text("SELECT email, display_name FROM users WHERE id = :id"),
                {"id": user_id},
            )
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(status_code=404, detail="usuario no encontrado")
    return _me_payload(user_id, str(row["email"]), row["display_name"])


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest,
    user_id: UserID,
    session_cookie: SessionCookie = None,
) -> Response:
    with connection() as conn:
        if not vault.verify_user_password(conn, user_id, body.current_password):
            raise HTTPException(status_code=403, detail="contraseña actual incorrecta")
        vault.change_user_password(conn, user_id, body.new_password)
        # Revoca otras sesiones (mantiene la actual); la contraseña vieja deja de servir.
        if session_cookie:
            sessions.revoke_other_sessions(conn, user_id, session_cookie)
        else:
            sessions.revoke_all_for_user(conn, user_id)
    _log.info("auth.password_changed", user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
