"""Sesiones de login del dashboard.

La cookie lleva un token opaco aleatorio; en la DB se guarda `id = sha256(token)` (el token plano
nunca se persiste, así una fuga de la tabla no permite suplantar sesiones). La sesión es SOLO auth:
no guarda ninguna llave de cifrado (el servidor descifra con la master key global), por eso
reiniciar el API no la invalida y la ingesta desatendida no la necesita.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, text

from memex.config import settings
from memex.logging import get_logger

_log = get_logger("memex.security.sessions")
_TOKEN_BYTES = 32


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(
    conn: Connection,
    user_id: int,
    *,
    user_agent: str | None = None,
    client_ip: str | None = None,
) -> str:
    """Crea una sesión y devuelve el TOKEN PLANO (para la cookie). El plano no se persiste."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    expires = datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds)
    conn.execute(
        text(
            """
            INSERT INTO sessions (id, user_id, expires_at, user_agent, client_ip)
            VALUES (:id, :uid, :exp, :ua, :ip)
            """
        ),
        {
            "id": _hash_token(token),
            "uid": user_id,
            "exp": expires,
            "ua": user_agent,
            "ip": client_ip,
        },
    )
    _log.info("session.created", user_id=user_id)
    return token


def validate_session(conn: Connection, token: str) -> int | None:
    """Devuelve el user_id si la sesión existe, no está revocada y no expiró; si no, None."""
    row = (
        conn.execute(
            text("SELECT user_id, expires_at, revoked_at FROM sessions WHERE id = :id"),
            {"id": _hash_token(token)},
        )
        .mappings()
        .first()
    )
    if row is None or row["revoked_at"] is not None:
        return None
    if row["expires_at"] <= datetime.now(UTC):
        return None
    conn.execute(
        text("UPDATE sessions SET last_seen_at = NOW() WHERE id = :id"),
        {"id": _hash_token(token)},
    )
    return int(row["user_id"])


def revoke_session(conn: Connection, token: str) -> None:
    """Revoca la sesión correspondiente al token (idempotente)."""
    conn.execute(
        text("UPDATE sessions SET revoked_at = NOW() WHERE id = :id AND revoked_at IS NULL"),
        {"id": _hash_token(token)},
    )


def revoke_all_for_user(conn: Connection, user_id: int) -> None:
    """Revoca todas las sesiones activas del usuario (ej. tras cambiar la contraseña)."""
    conn.execute(
        text("UPDATE sessions SET revoked_at = NOW() WHERE user_id = :uid AND revoked_at IS NULL"),
        {"uid": user_id},
    )


def revoke_other_sessions(conn: Connection, user_id: int, current_token: str) -> None:
    """Revoca las sesiones del usuario EXCEPTO la actual (cambio de contraseña sin auto-logout)."""
    conn.execute(
        text(
            "UPDATE sessions SET revoked_at = NOW() "
            "WHERE user_id = :uid AND id != :id AND revoked_at IS NULL"
        ),
        {"uid": user_id, "id": _hash_token(current_token)},
    )
