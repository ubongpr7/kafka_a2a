from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator
from uuid import uuid4

from kafka_a2a.models import (
    Artifact,
    Message,
    PushNotificationConfig,
    Task,
    TaskArtifactUpdateEvent,
    TaskEvent,
    TaskPushNotificationConfig,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from kafka_a2a.runtime.task_store import TaskEventRecord


def _require_redis() -> Any:
    try:
        import redis.asyncio as redis_async  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Redis task store requires the `redis` extra (e.g. `uv sync --extra redis`)."
        ) from exc
    return redis_async


def _parse_task_event(value: str) -> TaskEvent:
    obj: Any
    try:
        obj = Task.model_validate_json(value)
        return obj
    except Exception:
        pass
    try:
        obj = TaskStatusUpdateEvent.model_validate_json(value)
        return obj
    except Exception:
        pass
    return TaskArtifactUpdateEvent.model_validate_json(value)


@dataclass(slots=True)
class RedisTaskStoreConfig:
    url: str = "redis://localhost:6379/0"
    namespace: str = "ka2a"
    block_ms: int = 1000
    read_count: int = 100


class RedisTaskStore:
    def __init__(self, *, redis: Any, config: RedisTaskStoreConfig | None = None) -> None:
        self._redis = redis
        self._cfg = config or RedisTaskStoreConfig()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RedisTaskStore":
        import os

        env_map = env or os.environ
        defaults = RedisTaskStoreConfig()
        cfg = RedisTaskStoreConfig(
            url=(env_map.get("KA2A_REDIS_URL") or defaults.url).strip(),
            namespace=(env_map.get("KA2A_REDIS_NAMESPACE") or defaults.namespace).strip(),
            block_ms=int(env_map.get("KA2A_REDIS_BLOCK_MS") or str(defaults.block_ms)),
            read_count=int(env_map.get("KA2A_REDIS_READ_COUNT") or str(defaults.read_count)),
        )
        redis_async = _require_redis()
        client = redis_async.from_url(cfg.url, decode_responses=True)
        return cls(redis=client, config=cfg)

    def _tasks_index_key(self) -> str:
        return f"{self._cfg.namespace}:tasks"

    def _context_tasks_index_key(self, context_id: str) -> str:
        return f"{self._cfg.namespace}:context:{context_id}:tasks"

    def _task_key(self, task_id: str) -> str:
        return f"{self._cfg.namespace}:task:{task_id}"

    def _seq_key(self, task_id: str) -> str:
        return f"{self._cfg.namespace}:task:{task_id}:seq"

    def _events_key(self, task_id: str) -> str:
        return f"{self._cfg.namespace}:task:{task_id}:events"

    def _push_cfgs_key(self, task_id: str) -> str:
        return f"{self._cfg.namespace}:task:{task_id}:push_cfgs"

    async def create_task(
        self,
        *,
        initial_message: Message,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        task_id = str(uuid4())
        context_id = context_id or initial_message.context_id or str(uuid4())
        # TaskStore owns task ids; do not accept client-provided task ids.
        initial_message.task_id = task_id
        initial_message.context_id = context_id
        task = Task(
            id=task_id,
            context_id=context_id,
            status=TaskStatus(state=TaskState.submitted, message=initial_message),
            history=[initial_message],
            artifacts=[],
            metadata=dict(metadata) if metadata else None,
        )

        created_score = float(task.status.timestamp.timestamp())
        task_json = task.model_dump_json(by_alias=True, exclude_none=True)

        pipe = self._redis.pipeline(transaction=True)
        pipe.set(self._task_key(task_id), task_json)
        pipe.zadd(self._tasks_index_key(), {task_id: created_score})
        pipe.zadd(self._context_tasks_index_key(context_id), {task_id: created_score})
        pipe.set(self._seq_key(task_id), 0)
        pipe.xadd(self._events_key(task_id), {"seq": "0", "event": task_json})
        await pipe.execute()
        return task

    async def get_task(self, task_id: str) -> Task | None:
        raw = await self._redis.get(self._task_key(task_id))
        if raw is None:
            return None
        return Task.model_validate_json(raw)

    async def list_tasks(self) -> list[Task]:
        task_ids = await self._redis.zrange(self._tasks_index_key(), 0, -1)
        if not task_ids:
            return []
        keys = [self._task_key(tid) for tid in task_ids]
        raws = await self._redis.mget(keys)
        tasks: list[Task] = []
        for raw in raws:
            if raw is None:
                continue
            try:
                tasks.append(Task.model_validate_json(raw))
            except Exception:
                continue
        return tasks

    async def list_tasks_by_context(self, context_id: str, *, limit: int | None = None) -> list[Task]:
        index_key = self._context_tasks_index_key(context_id)
        if limit is None:
            task_ids = await self._redis.zrange(index_key, 0, -1)
        else:
            if limit <= 0:
                return []
            # Get most recent tasks then return them in chronological order.
            task_ids = await self._redis.zrevrange(index_key, 0, limit - 1)
            task_ids = list(reversed(task_ids))

        if not task_ids:
            return []

        keys = [self._task_key(tid) for tid in task_ids]
        raws = await self._redis.mget(keys)
        tasks: list[Task] = []
        for raw in raws:
            if raw is None:
                continue
            try:
                tasks.append(Task.model_validate_json(raw))
            except Exception:
                continue
        return tasks

    async def set_push_notification_config(
        self, *, task_id: str, config: PushNotificationConfig
    ) -> TaskPushNotificationConfig:
        config_id = config.id or "default"
        if not await self._redis.exists(self._task_key(task_id)):
            raise KeyError(task_id)
        await self._redis.hset(
            self._push_cfgs_key(task_id),
            config_id,
            config.model_dump_json(by_alias=True, exclude_none=True),
        )
        return TaskPushNotificationConfig(task_id=task_id, push_notification_config=config)

    async def get_push_notification_config(
        self, *, task_id: str, config_id: str | None = None
    ) -> TaskPushNotificationConfig | None:
        if config_id is not None:
            raw = await self._redis.hget(self._push_cfgs_key(task_id), config_id)
            if raw is None:
                return None
            cfg = PushNotificationConfig.model_validate_json(raw)
            return TaskPushNotificationConfig(task_id=task_id, push_notification_config=cfg)

        configs = await self._redis.hgetall(self._push_cfgs_key(task_id))
        if not configs:
            return None
        if (default_raw := configs.get("default")) is not None:
            cfg = PushNotificationConfig.model_validate_json(default_raw)
            return TaskPushNotificationConfig(task_id=task_id, push_notification_config=cfg)
        if len(configs) == 1:
            cfg = PushNotificationConfig.model_validate_json(next(iter(configs.values())))
            return TaskPushNotificationConfig(task_id=task_id, push_notification_config=cfg)
        return None

    async def list_push_notification_configs(self, *, task_id: str) -> list[TaskPushNotificationConfig]:
        configs = await self._redis.hgetall(self._push_cfgs_key(task_id))
        out: list[TaskPushNotificationConfig] = []
        for raw in configs.values():
            try:
                cfg = PushNotificationConfig.model_validate_json(raw)
            except Exception:
                continue
            out.append(TaskPushNotificationConfig(task_id=task_id, push_notification_config=cfg))
        return out

    async def delete_push_notification_config(self, *, task_id: str, config_id: str | None = None) -> None:
        key = self._push_cfgs_key(task_id)
        if config_id is None:
            await self._redis.delete(key)
            return
        await self._redis.hdel(key, config_id)

    async def append_status(self, *, task_id: str, status: TaskStatus) -> TaskStatusUpdateEvent:
        task = await self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)

        if status.message is not None:
            status.message.task_id = status.message.task_id or task_id
            status.message.context_id = status.message.context_id or task.context_id
            if task.history is None:
                task.history = []
            task.history.append(status.message)

        event = TaskStatusUpdateEvent(
            task_id=task_id,
            context_id=task.context_id,
            status=status,
            final=status.state
            in (
                TaskState.completed,
                TaskState.failed,
                TaskState.canceled,
                TaskState.rejected,
                TaskState.input_required,
                TaskState.auth_required,
            ),
        )
        task.status = status

        seq = int(await self._redis.incr(self._seq_key(task_id)))
        pipe = self._redis.pipeline(transaction=True)
        pipe.set(self._task_key(task_id), task.model_dump_json(by_alias=True, exclude_none=True))
        pipe.xadd(
            self._events_key(task_id),
            {"seq": str(seq), "event": event.model_dump_json(by_alias=True, exclude_none=True)},
        )
        await pipe.execute()
        return event

    async def append_artifact(self, *, task_id: str, artifact: Artifact) -> TaskArtifactUpdateEvent:
        task = await self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)

        event = TaskArtifactUpdateEvent(task_id=task_id, context_id=task.context_id, artifact=artifact)
        task.artifacts.append(artifact)

        seq = int(await self._redis.incr(self._seq_key(task_id)))
        pipe = self._redis.pipeline(transaction=True)
        pipe.set(self._task_key(task_id), task.model_dump_json(by_alias=True, exclude_none=True))
        pipe.xadd(
            self._events_key(task_id),
            {"seq": str(seq), "event": event.model_dump_json(by_alias=True, exclude_none=True)},
        )
        await pipe.execute()
        return event

    async def iter_events(self, task_id: str, *, replay_history: bool = True) -> AsyncIterator[TaskEventRecord]:
        stream = self._events_key(task_id)
        last_id = "0-0" if replay_history else "$"
        while True:
            items = await self._redis.xread({stream: last_id}, block=self._cfg.block_ms, count=self._cfg.read_count)
            if not items:
                await asyncio.sleep(0)
                continue
            for _name, entries in items:
                for entry_id, fields in entries:
                    last_id = entry_id
                    try:
                        seq = int(fields.get("seq") or "0")
                        event_raw = fields.get("event") or ""
                        event = _parse_task_event(event_raw)
                    except Exception:
                        continue
                    yield TaskEventRecord(sequence=seq, event=event)

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
