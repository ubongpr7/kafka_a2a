from __future__ import annotations

import os
from functools import lru_cache

from kafka_a2a.credentials import EncryptedSecret


def _split_keys(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _load_fernet_keys() -> list[str]:
    keys: list[str] = []
    primary = (os.getenv("KA2A_FERNET_KEY") or os.getenv("FERNET_KEY") or "").strip()
    if primary:
        keys.append(primary)
    keys.extend(_split_keys(os.getenv("KA2A_FERNET_KEYS")))
    return keys


@lru_cache(maxsize=1)
def _build_fernet_instances() -> tuple[object, ...]:
    try:
        from cryptography.fernet import Fernet
    except Exception as exc:
        raise RuntimeError(
            "cryptography is required for Fernet decryption. Install kafka-a2a with the auth extra."
        ) from exc

    keys = _load_fernet_keys()
    if not keys:
        raise ValueError("Set KA2A_FERNET_KEY (or FERNET_KEY) to decrypt encrypted JWT secrets.")

    instances: list[object] = []
    for key in keys:
        try:
            instances.append(Fernet(key))
        except Exception as exc:
            raise ValueError("Invalid Fernet key in KA2A_FERNET_KEY / KA2A_FERNET_KEYS.") from exc
    return tuple(instances)


def clear_fernet_cache() -> None:
    _build_fernet_instances.cache_clear()


def decrypt_fernet_secret(secret: EncryptedSecret) -> str:
    alg = (secret.alg or "fernet").strip().lower()
    if alg and alg != "fernet":
        raise ValueError(f"Unsupported encrypted secret algorithm: {secret.alg}")

    try:
        from cryptography.fernet import InvalidToken
    except Exception as exc:
        raise RuntimeError(
            "cryptography is required for Fernet decryption. Install kafka-a2a with the auth extra."
        ) from exc

    ciphertext = secret.ciphertext.encode()
    last_error: Exception | None = None

    for fernet in _build_fernet_instances():
        try:
            plaintext = fernet.decrypt(ciphertext)
            return plaintext.decode()
        except InvalidToken as exc:
            last_error = exc
            continue

    raise ValueError("Unable to decrypt JWT encrypted secret with configured Fernet keys.") from last_error

