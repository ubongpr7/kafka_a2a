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
    TaskPushNotificationConfig,
    TaskArtifactUpdateEvent,
    TaskEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)


@dataclass(slots=True)
class TaskEventRecord:
    sequence: int
    event: TaskEvent


class InMemoryTaskStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tasks: dict[str, Task] = {}
        self._events: dict[str, list[TaskEventRecord]] = {}
        self._subscribers: dict[str, list[asyncio.Queue[TaskEventRecord]]] = {}
        self._push_configs: dict[str, dict[str, PushNotificationConfig]] = {}

    async def create_task(
        self,
        *,
        initial_message: Message,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        task_id = str(uuid4())
        context_id = context_id or str(uuid4())
        initial_message.task_id = initial_message.task_id or task_id
        initial_message.context_id = initial_message.context_id or context_id
        task = Task(
            id=task_id,
            context_id=context_id,
            status=TaskStatus(state=TaskState.submitted, message=initial_message),
            history=[initial_message],
            artifacts=[],
            metadata=dict(metadata) if metadata else None,
        )
        async with self._lock:
            self._tasks[task_id] = task
            self._events[task_id] = [TaskEventRecord(sequence=0, event=task)]
            self._subscribers.setdefault(task_id, [])
        return task

    async def get_task(self, task_id: str) -> Task | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            return task

    async def list_tasks(self) -> list[Task]:
        async with self._lock:
            return list(self._tasks.values())

    async def set_push_notification_config(
        self, *, task_id: str, config: PushNotificationConfig
    ) -> TaskPushNotificationConfig:
        config_id = config.id or "default"
        async with self._lock:
            if task_id not in self._tasks:
                raise KeyError(task_id)
            self._push_configs.setdefault(task_id, {})[config_id] = config
        return TaskPushNotificationConfig(task_id=task_id, push_notification_config=config)

    async def get_push_notification_config(
        self, *, task_id: str, config_id: str | None = None
    ) -> TaskPushNotificationConfig | None:
        async with self._lock:
            configs = self._push_configs.get(task_id, {})
            if not configs:
                return None
            if config_id is not None:
                cfg = configs.get(config_id)
                return TaskPushNotificationConfig(task_id=task_id, push_notification_config=cfg) if cfg else None
            # No id requested: prefer "default", else return the single config if only one exists.
            if (default_cfg := configs.get("default")) is not None:
                return TaskPushNotificationConfig(task_id=task_id, push_notification_config=default_cfg)
            if len(configs) == 1:
                cfg = next(iter(configs.values()))
                return TaskPushNotificationConfig(task_id=task_id, push_notification_config=cfg)
            return None

    async def list_push_notification_configs(self, *, task_id: str) -> list[TaskPushNotificationConfig]:
        async with self._lock:
            configs = self._push_configs.get(task_id, {})
            return [
                TaskPushNotificationConfig(task_id=task_id, push_notification_config=cfg)
                for cfg in configs.values()
            ]

    async def delete_push_notification_config(
        self, *, task_id: str, config_id: str | None = None
    ) -> None:
        async with self._lock:
            if config_id is None:
                self._push_configs.pop(task_id, None)
                return
            configs = self._push_configs.get(task_id)
            if not configs:
                return
            configs.pop(config_id, None)
            if not configs:
                self._push_configs.pop(task_id, None)

    async def append_status(self, *, task_id: str, status: TaskStatus) -> TaskStatusUpdateEvent:
        async with self._lock:
            task = self._tasks[task_id]
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
            record = self._append_event_locked(task_id, event)
            self._fanout_locked(task_id, record)
        return event

    async def append_artifact(self, *, task_id: str, artifact: Artifact) -> TaskArtifactUpdateEvent:
        async with self._lock:
            task = self._tasks[task_id]
            event = TaskArtifactUpdateEvent(task_id=task_id, context_id=task.context_id, artifact=artifact)
            task.artifacts.append(artifact)
            record = self._append_event_locked(task_id, event)
            self._fanout_locked(task_id, record)
        return event

    async def subscribe(self, task_id: str) -> tuple[asyncio.Queue[TaskEventRecord], list[TaskEventRecord]]:
        queue: asyncio.Queue[TaskEventRecord] = asyncio.Queue()
        async with self._lock:
            self._subscribers.setdefault(task_id, []).append(queue)
            history = list(self._events.get(task_id, []))
        return queue, history

    async def unsubscribe(self, task_id: str, queue: asyncio.Queue[TaskEventRecord]) -> None:
        async with self._lock:
            queues = self._subscribers.get(task_id)
            if not queues:
                return
            try:
                queues.remove(queue)
            except ValueError:
                return

    async def iter_events(self, task_id: str) -> AsyncIterator[TaskEventRecord]:
        queue, history = await self.subscribe(task_id)
        try:
            for record in history:
                yield record
            while True:
                record = await queue.get()
                yield record
        finally:
            await self.unsubscribe(task_id, queue)

    def _append_event_locked(self, task_id: str, event: TaskEvent) -> TaskEventRecord:
        records = self._events.setdefault(task_id, [])
        seq = records[-1].sequence + 1 if records else 0
        record = TaskEventRecord(sequence=seq, event=event)
        records.append(record)
        return record

    def _fanout_locked(self, task_id: str, record: TaskEventRecord) -> None:
        for queue in list(self._subscribers.get(task_id, [])):
            try:
                queue.put_nowait(record)
            except asyncio.QueueFull:
                # Best-effort fanout; slow subscribers may miss updates.
                pass
