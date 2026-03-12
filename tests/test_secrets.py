from __future__ import annotations

import pytest

from kafka_a2a.credentials import EncryptedSecret
from kafka_a2a.secrets import clear_fernet_cache, decrypt_fernet_secret


def test_decrypt_fernet_secret_with_primary_env(monkeypatch: pytest.MonkeyPatch) -> None:
    fernet_mod = pytest.importorskip("cryptography.fernet")
    key = fernet_mod.Fernet.generate_key().decode()
    cipher = fernet_mod.Fernet(key.encode())
    ciphertext = cipher.encrypt(b"my-secret").decode()

    monkeypatch.setenv("KA2A_FERNET_KEY", key)
    clear_fernet_cache()
    try:
        value = decrypt_fernet_secret(EncryptedSecret(ciphertext=ciphertext, alg="fernet"))
    finally:
        clear_fernet_cache()

    assert value == "my-secret"


def test_decrypt_fernet_secret_tries_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    fernet_mod = pytest.importorskip("cryptography.fernet")
    key1 = fernet_mod.Fernet.generate_key().decode()
    key2 = fernet_mod.Fernet.generate_key().decode()
    cipher = fernet_mod.Fernet(key2.encode())
    ciphertext = cipher.encrypt(b"my-secret").decode()

    monkeypatch.setenv("KA2A_FERNET_KEY", key1)
    monkeypatch.setenv("KA2A_FERNET_KEYS", key2)
    clear_fernet_cache()
    try:
        value = decrypt_fernet_secret(EncryptedSecret(ciphertext=ciphertext, alg="fernet"))
    finally:
        clear_fernet_cache()

    assert value == "my-secret"


def test_decrypt_fernet_secret_rejects_unknown_alg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KA2A_FERNET_KEY", "x")
    clear_fernet_cache()
    try:
        with pytest.raises(ValueError):
            decrypt_fernet_secret(EncryptedSecret(ciphertext="abc", alg="A256GCM"))
    finally:
        clear_fernet_cache()

