from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(str(raw).strip())
    except Exception:
        return default


_GLOBAL_LLM_SEMAPHORE: asyncio.Semaphore | None = None


def llm_semaphore() -> asyncio.Semaphore | None:
    """
    Per-process concurrency limiter for upstream LLM calls.

    Configure with:
      - `KA2A_LLM_MAX_CONCURRENCY` (preferred)
      - `KA2A_LLM_MAX_INFLIGHT` (alias)
    """

    limit = _env_int("KA2A_LLM_MAX_CONCURRENCY", _env_int("KA2A_LLM_MAX_INFLIGHT", 5))
    if limit <= 0:
        return None

    global _GLOBAL_LLM_SEMAPHORE
    if _GLOBAL_LLM_SEMAPHORE is None:
        _GLOBAL_LLM_SEMAPHORE = asyncio.Semaphore(limit)
    return _GLOBAL_LLM_SEMAPHORE


@dataclass(slots=True)
class RetryConfig:
    max_retries: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0

    @classmethod
    def from_env(cls) -> "RetryConfig":
        return cls(
            max_retries=_env_int("KA2A_LLM_RETRY_MAX_RETRIES", 3),
            base_delay_s=_env_float("KA2A_LLM_RETRY_BASE_DELAY_S", 0.5),
            max_delay_s=_env_float("KA2A_LLM_RETRY_MAX_DELAY_S", 8.0),
        )


def backoff_delay_s(*, attempt: int, cfg: RetryConfig, retry_after_s: float | None = None) -> float:
    if retry_after_s is not None and retry_after_s > 0:
        # Honor Retry-After when provided.
        return float(min(retry_after_s, cfg.max_delay_s))

    # Full jitter exponential backoff.
    exp = min(cfg.max_delay_s, cfg.base_delay_s * (2**max(0, attempt)))
    return random.random() * float(exp)

