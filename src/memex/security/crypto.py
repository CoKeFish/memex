"""Primitivas criptográficas del vault (puro, sin DB).

Envelope encryption (ver memoria credentials-vault-architecture):

    master key del servidor (MEMEX_SECRET_KEY, Doppler)
        │  wrap (AES-256-GCM)
        ▼
    DEK por-usuario (aleatorio, 32 bytes)
        │  encrypt (AES-256-GCM, nonce nuevo por secreto)
        ▼
    account_secrets.ciphertext

- La MASTER KEY es única y global. Se deriva determinísticamente (HKDF-SHA256 → 32 bytes) desde
  `settings.secret_key`, para tolerar cualquier cadena de alta entropía (recomendado
  `openssl rand -base64 48`). Determinista = la misma llave en cada arranque.
- El DEK es aleatorio por usuario; se guarda ENVUELTO con la master key (nunca en claro).
- Argon2id se usa SOLO para verificar la contraseña de login; NO deriva ninguna llave de cifrado
  (por eso el reset de contraseña no pierde credenciales).

Nada en este módulo toca la DB ni loguea valores sensibles.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from typing import Any

from argon2 import PasswordHasher
from argon2 import exceptions as argon2_exc
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from memex.config import settings

_NONCE_BYTES = 12
_KEY_BYTES = 32
_MASTER_INFO = b"memex.vault.master-key.v1"
_STATE_INFO = b"memex.oauth.state-sign.v1"
_STATE_TTL_SECONDS = 600  # 10 min


class CryptoError(Exception):
    """Base de los errores criptográficos del vault."""


class MasterKeyMissingError(CryptoError):
    """`MEMEX_SECRET_KEY` no está configurada — el vault no puede operar."""


class InvalidKeyError(CryptoError):
    """Un descifrado/unwrap falló el tag de autenticación (llave equivocada o datos corruptos)."""


# ----- Contraseña de login (Argon2id) -------------------------------------- #


def _hasher() -> PasswordHasher:
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
    )


def hash_password(password: str) -> str:
    """Devuelve un hash PHC Argon2id (incluye salt + params). Solo para verificar login."""
    return _hasher().hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verifica en tiempo constante. Cualquier mismatch/hash inválido → False (no levanta)."""
    try:
        return _hasher().verify(password_hash, password)
    except (argon2_exc.VerificationError, argon2_exc.InvalidHashError):
        # VerifyMismatchError hereda de VerificationError → cubierto.
        return False


def needs_rehash(password_hash: str) -> bool:
    """True si el hash usa params viejos y conviene re-hashear en el próximo login exitoso."""
    return _hasher().check_needs_rehash(password_hash)


# ----- Master key + envelope ------------------------------------------------ #


def load_master_key() -> bytes:
    """Deriva la master key de 32 bytes desde `settings.secret_key`. Lazy: solo al usar el vault."""
    raw = settings.secret_key.strip()
    if not raw:
        raise MasterKeyMissingError(
            "MEMEX_SECRET_KEY no está configurada; el vault de credenciales no puede operar"
        )
    hkdf = HKDF(algorithm=hashes.SHA256(), length=_KEY_BYTES, salt=None, info=_MASTER_INFO)
    return hkdf.derive(raw.encode("utf-8"))


def generate_dek() -> bytes:
    """DEK aleatorio de 32 bytes (uno por usuario)."""
    return os.urandom(_KEY_BYTES)


def _encrypt(key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return ciphertext, nonce


def _decrypt(key: bytes, ciphertext: bytes, nonce: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise InvalidKeyError(
            "descifrado falló: tag de autenticación inválido (llave equivocada o datos corruptos)"
        ) from exc


def wrap_dek(master_key: bytes, dek: bytes) -> tuple[bytes, bytes]:
    """Envuelve (cifra) el DEK con la master key. Devuelve (wrapped, nonce)."""
    return _encrypt(master_key, dek)


def unwrap_dek(master_key: bytes, wrapped: bytes, nonce: bytes) -> bytes:
    """Desenvuelve el DEK. Levanta `InvalidKeyError` si la master key no corresponde."""
    return _decrypt(master_key, wrapped, nonce)


def encrypt_secret(dek: bytes, plaintext: str) -> tuple[bytes, bytes]:
    """Cifra un secreto con el DEK. Devuelve (ciphertext, nonce)."""
    return _encrypt(dek, plaintext.encode("utf-8"))


def decrypt_secret(dek: bytes, ciphertext: bytes, nonce: bytes) -> str:
    """Descifra un secreto con el DEK. Levanta `InvalidKeyError` si el DEK no corresponde."""
    return _decrypt(dek, ciphertext, nonce).decode("utf-8")


def last4(plaintext: str) -> str:
    """Últimos 4 chars (no-secreto) para máscara en la UI. Cadenas cortas → ''."""
    stripped = plaintext.strip()
    return stripped[-4:] if len(stripped) >= 4 else ""


# ----- State firmado para OAuth (HMAC, sin tabla) --------------------------- #


class StateError(CryptoError):
    """El `state` de OAuth está mal formado, manipulado o expirado."""


def _state_key() -> bytes:
    """Clave HMAC para el state, derivada de la master key con un info distinto al del cifrado."""
    raw = settings.secret_key.strip()
    if not raw:
        raise MasterKeyMissingError(
            "MEMEX_SECRET_KEY no está configurada; no se puede firmar el state de OAuth"
        )
    hkdf = HKDF(algorithm=hashes.SHA256(), length=_KEY_BYTES, salt=None, info=_STATE_INFO)
    return hkdf.derive(raw.encode("utf-8"))


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_state(payload: dict[str, Any], *, now: int, ttl_seconds: int = _STATE_TTL_SECONDS) -> str:
    """Firma un `state` opaco `<body>.<hmac>` con expiración. `now` = epoch en segundos."""
    body = dict(payload)
    body["exp"] = now + ttl_seconds
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_state_key(), raw, hashlib.sha256).digest()
    return f"{_b64encode(raw)}.{_b64encode(sig)}"


def verify_state(token: str, *, now: int) -> dict[str, Any]:
    """Valida firma + expiración y devuelve el payload. Levanta `StateError` si algo no cierra."""
    try:
        raw_b64, sig_b64 = token.split(".", 1)
        raw = _b64decode(raw_b64)
        sig = _b64decode(sig_b64)
    except (ValueError, binascii.Error) as exc:
        raise StateError("state mal formado") from exc
    expected = hmac.new(_state_key(), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise StateError("firma de state inválida")
    payload: dict[str, Any] = json.loads(raw)
    if int(payload.get("exp", 0)) < now:
        raise StateError("state expirado")
    return payload
