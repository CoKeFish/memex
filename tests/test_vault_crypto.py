"""Tests de `memex.security.crypto` — primitivas del vault (sin DB)."""

from __future__ import annotations

import pytest

from memex.config import settings
from memex.security import crypto


@pytest.fixture
def master_key(monkeypatch: pytest.MonkeyPatch) -> bytes:
    monkeypatch.setattr(settings, "secret_key", "test-master-key-de-alta-entropia-0123456789")
    return crypto.load_master_key()


def test_master_key_is_deterministic_and_32_bytes(master_key: bytes) -> None:
    assert len(master_key) == 32
    assert crypto.load_master_key() == master_key  # determinista entre llamadas


def test_master_key_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", "   ")
    with pytest.raises(crypto.MasterKeyMissingError):
        crypto.load_master_key()


def test_dek_wrap_unwrap_round_trip(master_key: bytes) -> None:
    dek = crypto.generate_dek()
    assert len(dek) == 32
    wrapped, nonce = crypto.wrap_dek(master_key, dek)
    assert wrapped != dek
    assert crypto.unwrap_dek(master_key, wrapped, nonce) == dek


def test_unwrap_with_wrong_master_key_raises(master_key: bytes) -> None:
    dek = crypto.generate_dek()
    wrapped, nonce = crypto.wrap_dek(master_key, dek)
    wrong = crypto.generate_dek()  # 32 bytes aleatorios distintos
    with pytest.raises(crypto.InvalidKeyError):
        crypto.unwrap_dek(wrong, wrapped, nonce)


def test_secret_encrypt_decrypt_round_trip() -> None:
    dek = crypto.generate_dek()
    plaintext = "hunter2-contraseña-de-gmail"
    ciphertext, nonce = crypto.encrypt_secret(dek, plaintext)
    assert plaintext.encode("utf-8") not in ciphertext
    assert crypto.decrypt_secret(dek, ciphertext, nonce) == plaintext


def test_decrypt_with_wrong_dek_raises() -> None:
    dek = crypto.generate_dek()
    ciphertext, nonce = crypto.encrypt_secret(dek, "secreto")
    with pytest.raises(crypto.InvalidKeyError):
        crypto.decrypt_secret(crypto.generate_dek(), ciphertext, nonce)


def test_nonce_is_unique_per_operation() -> None:
    dek = crypto.generate_dek()
    _, nonce_a = crypto.encrypt_secret(dek, "x")
    _, nonce_b = crypto.encrypt_secret(dek, "x")
    assert nonce_a != nonce_b


def test_last4() -> None:
    assert crypto.last4("abcdef") == "cdef"
    assert crypto.last4("ab") == ""
    assert crypto.last4("  longtoken  ") == "oken"


def test_password_hash_and_verify() -> None:
    h = crypto.hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert crypto.verify_password("correct horse battery staple", h) is True
    assert crypto.verify_password("wrong", h) is False


def test_verify_password_with_garbage_hash_is_false() -> None:
    assert crypto.verify_password("whatever", "not-a-valid-phc-hash") is False


# ----- State firmado de OAuth -------------------------------------------------- #


def test_state_sign_verify_round_trip(master_key: bytes) -> None:
    token = crypto.sign_state({"account_id": 5, "user_id": 1, "nonce": "abc"}, now=1000)
    payload = crypto.verify_state(token, now=1000)
    assert payload["account_id"] == 5
    assert payload["user_id"] == 1
    assert payload["nonce"] == "abc"


def test_state_expired_raises(master_key: bytes) -> None:
    token = crypto.sign_state({"x": 1}, now=1000, ttl_seconds=600)
    with pytest.raises(crypto.StateError):
        crypto.verify_state(token, now=1000 + 601)


def test_state_tampered_raises(master_key: bytes) -> None:
    token = crypto.sign_state({"x": 1}, now=1000)
    body, sig = token.split(".", 1)
    flipped = "A" if body[-1] != "A" else "B"
    with pytest.raises(crypto.StateError):
        crypto.verify_state(f"{body[:-1]}{flipped}.{sig}", now=1000)


def test_state_wrong_key_raises(master_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    token = crypto.sign_state({"x": 1}, now=1000)
    monkeypatch.setattr(settings, "secret_key", "wrong-key")
    with pytest.raises(crypto.StateError):
        crypto.verify_state(token, now=1000)
