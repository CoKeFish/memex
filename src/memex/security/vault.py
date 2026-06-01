"""Vault de credenciales: ciclo de vida del DEK por-usuario + secretos cifrados por cuenta.

Toca `memex.db` (vive fuera de `memex.ingestors`, así que es legal por ADR-001). Nunca loguea
valores sensibles (DEK, password, plaintext). El descifrado solo se expone server-side
(`memex.sources.resolver` para la ingesta, y el health-check del router de cuentas).

La fila `user_credentials` colocaliza el hash de la contraseña de LOGIN (Argon2id) y el DEK
envuelto con la master key del servidor. La contraseña NO deriva el DEK → cambiar/resetear la
contraseña no toca el DEK (lossless).
"""

from __future__ import annotations

from sqlalchemy import Connection, text

from memex.logging import get_logger
from memex.security import crypto

_log = get_logger("memex.security.vault")


class VaultError(Exception):
    """Base de los errores del vault a nivel de fila/DB."""


class UserVaultMissingError(VaultError):
    """El usuario no tiene fila en `user_credentials` (vault no provisionado)."""


class AccountNotFoundError(VaultError):
    """La cuenta no existe — no se puede resolver su dueño para descifrar."""


# ----- user_credentials: login + DEK --------------------------------------- #


def provision_user(conn: Connection, user_id: int, password: str) -> None:
    """Crea la fila de vault del usuario: hash de contraseña + DEK aleatorio envuelto.

    Levanta `crypto.MasterKeyMissingError` si `MEMEX_SECRET_KEY` no está configurada.
    """
    master = crypto.load_master_key()
    dek = crypto.generate_dek()
    wrapped, nonce = crypto.wrap_dek(master, dek)
    conn.execute(
        text(
            """
            INSERT INTO user_credentials
                (user_id, password_hash, wrapped_dek, dek_nonce, key_version)
            VALUES (:uid, :ph, :wd, :nonce, 1)
            """
        ),
        {"uid": user_id, "ph": crypto.hash_password(password), "wd": wrapped, "nonce": nonce},
    )
    _log.info("vault.provisioned", user_id=user_id)


def _password_hash(conn: Connection, user_id: int) -> str | None:
    value = conn.execute(
        text("SELECT password_hash FROM user_credentials WHERE user_id = :uid"),
        {"uid": user_id},
    ).scalar()
    return None if value is None else str(value)


def verify_user_password(conn: Connection, user_id: int, password: str) -> bool:
    """Verifica la contraseña de login (tiempo constante). Re-hashea si los params envejecieron."""
    stored = _password_hash(conn, user_id)
    if stored is None or not crypto.verify_password(password, stored):
        return False
    if crypto.needs_rehash(stored):
        conn.execute(
            text(
                "UPDATE user_credentials SET password_hash = :ph, updated_at = NOW() "
                "WHERE user_id = :uid"
            ),
            {"ph": crypto.hash_password(password), "uid": user_id},
        )
    return True


def change_user_password(conn: Connection, user_id: int, new_password: str) -> None:
    """Cambia la contraseña de login. El DEK NO cambia (lossless: no se re-cifra ningún secreto)."""
    result = conn.execute(
        text(
            "UPDATE user_credentials SET password_hash = :ph, updated_at = NOW() "
            "WHERE user_id = :uid"
        ),
        {"ph": crypto.hash_password(new_password), "uid": user_id},
    )
    if result.rowcount == 0:
        raise UserVaultMissingError(f"user {user_id} has no vault")
    _log.info("vault.password_changed", user_id=user_id)


def get_user_dek(conn: Connection, user_id: int) -> bytes:
    """Desenvuelve el DEK del usuario con la master key del servidor (sin necesitar sesión)."""
    row = (
        conn.execute(
            text("SELECT wrapped_dek, dek_nonce FROM user_credentials WHERE user_id = :uid"),
            {"uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise UserVaultMissingError(f"user {user_id} has no vault")
    master = crypto.load_master_key()
    return crypto.unwrap_dek(master, bytes(row["wrapped_dek"]), bytes(row["dek_nonce"]))


# ----- account_secrets: secretos cifrados por cuenta ----------------------- #


def _account_owner(conn: Connection, account_id: int) -> int:
    owner = conn.execute(
        text("SELECT user_id FROM accounts WHERE id = :aid"),
        {"aid": account_id},
    ).scalar()
    if owner is None:
        raise AccountNotFoundError(f"account {account_id} not found")
    return int(owner)


def set_secret(conn: Connection, account_id: int, secret_name: str, plaintext: str) -> str:
    """Cifra y guarda (upsert) un secreto bajo el DEK del dueño de la cuenta. Devuelve `last4`."""
    dek = get_user_dek(conn, _account_owner(conn, account_id))
    ciphertext, nonce = crypto.encrypt_secret(dek, plaintext)
    tag = crypto.last4(plaintext)
    conn.execute(
        text(
            """
            INSERT INTO account_secrets
                (account_id, secret_name, ciphertext, nonce, enc_version, last4, updated_at)
            VALUES (:aid, :name, :ct, :nonce, 1, :last4, NOW())
            ON CONFLICT (account_id, secret_name) DO UPDATE
                SET ciphertext = EXCLUDED.ciphertext,
                    nonce = EXCLUDED.nonce,
                    enc_version = EXCLUDED.enc_version,
                    last4 = EXCLUDED.last4,
                    updated_at = NOW()
            """
        ),
        {"aid": account_id, "name": secret_name, "ct": ciphertext, "nonce": nonce, "last4": tag},
    )
    _log.info("vault.secret.set", account_id=account_id, secret_name=secret_name)
    return tag


def get_account_secrets(conn: Connection, account_id: int) -> dict[str, str]:
    """Descifra TODOS los secretos de la cuenta. SOLO server-side (ingesta / health-check)."""
    dek = get_user_dek(conn, _account_owner(conn, account_id))
    rows = (
        conn.execute(
            text(
                "SELECT secret_name, ciphertext, nonce FROM account_secrets WHERE account_id = :aid"
            ),
            {"aid": account_id},
        )
        .mappings()
        .all()
    )
    return {
        str(r["secret_name"]): crypto.decrypt_secret(dek, bytes(r["ciphertext"]), bytes(r["nonce"]))
        for r in rows
    }


def list_secret_status(conn: Connection, account_id: int) -> list[dict[str, object]]:
    """Estado de cada secreto SIN descifrar: `{secret_name, configured, last4}` para el API."""
    rows = (
        conn.execute(
            text(
                "SELECT secret_name, last4 FROM account_secrets "
                "WHERE account_id = :aid ORDER BY secret_name"
            ),
            {"aid": account_id},
        )
        .mappings()
        .all()
    )
    return [
        {"secret_name": str(r["secret_name"]), "configured": True, "last4": str(r["last4"])}
        for r in rows
    ]


def delete_secret(conn: Connection, account_id: int, secret_name: str) -> bool:
    """Borra un secreto. Devuelve True si existía."""
    result = conn.execute(
        text("DELETE FROM account_secrets WHERE account_id = :aid AND secret_name = :name"),
        {"aid": account_id, "name": secret_name},
    )
    deleted = result.rowcount > 0
    if deleted:
        _log.info("vault.secret.deleted", account_id=account_id, secret_name=secret_name)
    return deleted
