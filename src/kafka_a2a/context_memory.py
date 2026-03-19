from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from kafka_a2a.tenancy import Principal


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(slots=True)
class ContextMemory:
    summary: str | None = None
    profile: dict[str, Any] | None = None
    workflow_state: dict[str, Any] | None = None
    updated_at: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "summary": self.summary,
                "profile": self.profile,
                "workflowState": self.workflow_state,
                "updatedAt": self.updated_at,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "ContextMemory":
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            return cls()
        summary = obj.get("summary")
        profile = obj.get("profile")
        workflow_state = obj.get("workflowState") or obj.get("workflow_state")
        updated_at = obj.get("updatedAt") or obj.get("updated_at")
        return cls(
            summary=str(summary) if isinstance(summary, str) and summary.strip() else None,
            profile=profile if isinstance(profile, dict) else None,
            workflow_state=workflow_state if isinstance(workflow_state, dict) else None,
            updated_at=str(updated_at) if isinstance(updated_at, str) and updated_at.strip() else None,
        )


class ContextMemoryStore(Protocol):
    async def get(self, *, context_id: str, principal: Principal | None) -> ContextMemory | None: ...

    async def set(self, *, context_id: str, principal: Principal | None, memory: ContextMemory) -> None: ...

    async def aclose(self) -> None: ...


def context_memory_key(*, namespace: str, context_id: str, principal: Principal | None) -> str:
    # Ensure multi-tenant isolation even if `context_id` is user-controlled.
    if principal is None:
        return f"{namespace}:context:{context_id}:memory"
    tenant = principal.tenant_id or "-"
    return f"{namespace}:tenant:{tenant}:user:{principal.user_id}:context:{context_id}:memory"


class InMemoryContextMemoryStore:
    def __init__(self, *, namespace: str = "ka2a") -> None:
        self._lock = asyncio.Lock()
        self._namespace = namespace
        self._mem: dict[str, ContextMemory] = {}

    async def get(self, *, context_id: str, principal: Principal | None) -> ContextMemory | None:
        key = context_memory_key(namespace=self._namespace, context_id=context_id, principal=principal)
        async with self._lock:
            return self._mem.get(key)

    async def set(self, *, context_id: str, principal: Principal | None, memory: ContextMemory) -> None:
        key = context_memory_key(namespace=self._namespace, context_id=context_id, principal=principal)
        if memory.updated_at is None:
            memory.updated_at = _utc_now_iso()
        async with self._lock:
            self._mem[key] = memory

    async def aclose(self) -> None:
        return None


def _require_redis() -> Any:
    try:
        import redis.asyncio as redis_async  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Redis context memory store requires the `redis` extra (e.g. `uv sync --extra redis`)."
        ) from exc
    return redis_async


@dataclass(slots=True)
class RedisContextMemoryStoreConfig:
    url: str = "redis://localhost:6379/0"
    namespace: str = "ka2a"
    ttl_s: int | None = None


class RedisContextMemoryStore:
    def __init__(self, *, redis: Any, config: RedisContextMemoryStoreConfig | None = None) -> None:
        self._redis = redis
        self._cfg = config or RedisContextMemoryStoreConfig()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RedisContextMemoryStore":
        import os

        env_map = env or os.environ
        defaults = RedisContextMemoryStoreConfig()
        cfg = RedisContextMemoryStoreConfig(
            url=(env_map.get("KA2A_REDIS_URL") or defaults.url).strip(),
            namespace=(env_map.get("KA2A_REDIS_NAMESPACE") or defaults.namespace).strip(),
            ttl_s=int(env_map["KA2A_CONTEXT_MEMORY_TTL_S"]) if env_map.get("KA2A_CONTEXT_MEMORY_TTL_S") else None,
        )
        redis_async = _require_redis()
        client = redis_async.from_url(cfg.url, decode_responses=True)
        return cls(redis=client, config=cfg)

    async def get(self, *, context_id: str, principal: Principal | None) -> ContextMemory | None:
        key = context_memory_key(namespace=self._cfg.namespace, context_id=context_id, principal=principal)
        raw = await self._redis.get(key)
        if raw is None:
            return None
        try:
            return ContextMemory.from_json(raw)
        except Exception:
            return None

    async def set(self, *, context_id: str, principal: Principal | None, memory: ContextMemory) -> None:
        key = context_memory_key(namespace=self._cfg.namespace, context_id=context_id, principal=principal)
        if memory.updated_at is None:
            memory.updated_at = _utc_now_iso()
        raw = memory.to_json()
        if self._cfg.ttl_s is not None and self._cfg.ttl_s > 0:
            await self._redis.setex(key, int(self._cfg.ttl_s), raw)
        else:
            await self._redis.set(key, raw)

    async def aclose(self) -> None:
        try:
            close = getattr(self._redis, "close", None)
            if close is not None:
                res = close()
                if asyncio.iscoroutine(res):
                    await res
            pool = getattr(self._redis, "connection_pool", None)
            if pool is not None:
                disconnect = getattr(pool, "disconnect", None)
                if disconnect is not None:
                    res = disconnect()
                    if asyncio.iscoroutine(res):
                        await res
        except Exception:
            return None
