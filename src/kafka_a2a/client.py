from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator
from uuid import uuid4

from aiokafka import AIOKafkaConsumer

from kafka_a2a.errors import A2AError
from kafka_a2a.models import (
    Message,
    PushNotificationConfig,
    Task,
    TaskArtifactUpdateEvent,
    TaskConfiguration,
    TaskPushNotificationConfig,
    TaskStatusUpdateEvent,
)
from kafka_a2a.protocol import (
    METHOD_AGENT_GET_AUTHENTICATED_EXTENDED_CARD,
    METHOD_MESSAGE_SEND,
    METHOD_MESSAGE_STREAM,
    METHOD_TASKS_CANCEL,
    METHOD_TASKS_GET,
    METHOD_TASKS_LIST,
    METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_DELETE,
    METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_GET,
    METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_LIST,
    METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_SET,
    METHOD_TASKS_SUBSCRIBE,
    METHOD_TASKS_RESUBSCRIBE,
    DeleteTaskPushNotificationConfigParams,
    GetTaskPushNotificationConfigParams,
    ListTaskPushNotificationConfigParams,
    MessageSendParams,
    RpcRequest,
    RpcResponse,
    TaskIdParams,
    TaskListParams,
    TaskQueryParams,
)
from kafka_a2a.transport.kafka import EnvelopeType, KafkaEnvelope, KafkaTransport, TopicNamer


StreamResult = Task | Message | TaskStatusUpdateEvent | TaskArtifactUpdateEvent


def _parse_stream_result(value: Any) -> StreamResult | Any:
    if not isinstance(value, dict):
        return value
    kind = value.get("kind")
    if kind == "task":
        return Task.model_validate(value)
    if kind == "message":
        return Message.model_validate(value)
    if kind == "status-update":
        return TaskStatusUpdateEvent.model_validate(value)
    if kind == "artifact-update":
        return TaskArtifactUpdateEvent.model_validate(value)
    return value


def _is_stream_done(result: StreamResult | Any) -> bool:
    if isinstance(result, TaskStatusUpdateEvent):
        return bool(result.final)
    if isinstance(result, Message):
        return True
    return False


@dataclass(slots=True)
class Ka2aClientConfig:
    client_id: str | None = None
    reply_topic: str | None = None
    reply_group_id: str | None = None
    request_timeout_s: float = 30.0


class Ka2aClient:
    def __init__(
        self,
        *,
        transport: KafkaTransport,
        config: Ka2aClientConfig | None = None,
        topic_namer: TopicNamer | None = None,
    ):
        self._transport = transport
        self._cfg = config or Ka2aClientConfig()
        self._topics = topic_namer or TopicNamer.from_env()

        self.client_id = self._cfg.client_id or str(uuid4())
        self.reply_topic = self._cfg.reply_topic or self._topics.client_replies(self.client_id)
        self._group_id = self._cfg.reply_group_id or f"ka2a.client.{self.client_id}"

        self._consumer: AIOKafkaConsumer | None = None
        self._consumer_task: asyncio.Task[None] | None = None

        self._pending: dict[str | int, asyncio.Future[RpcResponse]] = {}
        self._streams: dict[str | int, asyncio.Queue[RpcResponse]] = {}

    async def start(self) -> None:
        await self._transport.start()
        if self._consumer is not None:
            return
        self._consumer = self._transport.create_consumer(
            topics=[self.reply_topic],
            group_id=self._group_id,
            auto_offset_reset="latest",
        )
        await self._consumer.start()

        async def _loop() -> None:
            assert self._consumer is not None
            async for msg in self._consumer:
                try:
                    env = KafkaEnvelope.from_bytes(msg.value)
                except Exception as exc:
                    await self._transport.publish_dlq(
                        reason="envelope_decode_error",
                        error=str(exc),
                        topic=str(getattr(msg, "topic", "")),
                        partition=getattr(msg, "partition", None),
                        offset=getattr(msg, "offset", None),
                        timestamp_ms=getattr(msg, "timestamp", None),
                        key=getattr(msg, "key", None),
                        headers=list(getattr(msg, "headers", None) or []),
                        value=getattr(msg, "value", None),
                    )
                    continue
                if env.correlation_id is None:
                    continue

                if env.type == EnvelopeType.response:
                    try:
                        resp = RpcResponse.model_validate(env.payload)
                    except Exception:
                        continue
                    queue = self._streams.get(env.correlation_id)
                    if queue is not None:
                        queue.put_nowait(resp)
                    fut = self._pending.pop(env.correlation_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(resp)
                    continue

        self._consumer_task = asyncio.create_task(_loop())

    async def stop(self) -> None:
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            self._consumer_task = None

        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None

        await self._transport.stop()

    async def call(
        self,
        *,
        agent_name: str,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        request_id = str(uuid4())
        fut: asyncio.Future[RpcResponse] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut

        req = RpcRequest(id=request_id, method=method, params=params)
        env = KafkaEnvelope(
            type=EnvelopeType.request,
            correlation_id=request_id,
            sender=self.client_id,
            recipient=agent_name,
            reply_to=self.reply_topic,
            payload=req.model_dump(by_alias=True, exclude_none=True),
        )
        await self._transport.send(topic=self._topics.agent_requests(agent_name), envelope=env)

        timeout = timeout_s if timeout_s is not None else self._cfg.request_timeout_s
        resp = await asyncio.wait_for(fut, timeout=timeout)
        if resp.error is not None:
            raise A2AError(code=resp.error.code, message=resp.error.message, data=resp.error.data)
        return resp.result

    async def get_agent_card(self, *, agent_name: str) -> dict[str, Any]:
        return await self.call(agent_name=agent_name, method=METHOD_AGENT_GET_AUTHENTICATED_EXTENDED_CARD, params={})

    async def send_message(
        self,
        *,
        agent_name: str,
        message: Message,
        configuration: TaskConfiguration | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        params = MessageSendParams(message=message, configuration=configuration, metadata=metadata).model_dump(
            by_alias=True, exclude_none=True
        )
        result = await self.call(agent_name=agent_name, method=METHOD_MESSAGE_SEND, params=params)
        return Task.model_validate(result)

    async def get_task(
        self,
        *,
        agent_name: str,
        task_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        params = TaskQueryParams(id=task_id, metadata=metadata).model_dump(by_alias=True, exclude_none=True)
        result = await self.call(agent_name=agent_name, method=METHOD_TASKS_GET, params=params)
        return Task.model_validate(result)

    async def list_tasks(
        self,
        *,
        agent_name: str,
        limit: int | None = None,
        offset: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[Task]:
        params = TaskListParams(limit=limit, offset=offset, metadata=metadata).model_dump(
            by_alias=True, exclude_none=True
        )
        result = await self.call(agent_name=agent_name, method=METHOD_TASKS_LIST, params=params)
        tasks = result.get("tasks", [])
        return [Task.model_validate(t) for t in tasks]

    async def cancel_task(
        self,
        *,
        agent_name: str,
        task_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        params = TaskIdParams(id=task_id, metadata=metadata).model_dump(by_alias=True, exclude_none=True)
        result = await self.call(agent_name=agent_name, method=METHOD_TASKS_CANCEL, params=params)
        return Task.model_validate(result)

    async def set_task_push_notification_config(
        self,
        *,
        agent_name: str,
        task_id: str,
        config: PushNotificationConfig,
        metadata: dict[str, Any] | None = None,
    ) -> TaskPushNotificationConfig:
        params = TaskPushNotificationConfig(task_id=task_id, push_notification_config=config).model_dump(
            by_alias=True, exclude_none=True
        )
        if metadata is not None:
            params["metadata"] = metadata
        result = await self.call(
            agent_name=agent_name, method=METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_SET, params=params
        )
        return TaskPushNotificationConfig.model_validate(result)

    async def get_task_push_notification_config(
        self,
        *,
        agent_name: str,
        task_id: str,
        config_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskPushNotificationConfig:
        params = GetTaskPushNotificationConfigParams(
            id=task_id, metadata=metadata, push_notification_config_id=config_id
        ).model_dump(by_alias=True, exclude_none=True)
        result = await self.call(
            agent_name=agent_name, method=METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_GET, params=params
        )
        return TaskPushNotificationConfig.model_validate(result)

    async def list_task_push_notification_configs(
        self,
        *,
        agent_name: str,
        task_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[TaskPushNotificationConfig]:
        params = ListTaskPushNotificationConfigParams(id=task_id, metadata=metadata).model_dump(
            by_alias=True, exclude_none=True
        )
        result = await self.call(
            agent_name=agent_name, method=METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_LIST, params=params
        )
        return [TaskPushNotificationConfig.model_validate(item) for item in result]

    async def delete_task_push_notification_config(
        self,
        *,
        agent_name: str,
        task_id: str,
        config_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        params = DeleteTaskPushNotificationConfigParams(
            id=task_id, metadata=metadata, push_notification_config_id=config_id
        ).model_dump(by_alias=True, exclude_none=True)
        await self.call(
            agent_name=agent_name, method=METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_DELETE, params=params
        )

    async def stream_message(
        self,
        *,
        agent_name: str,
        message: Message,
        configuration: TaskConfiguration | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamResult]:
        request_id = str(uuid4())
        self._streams[request_id] = asyncio.Queue()
        fut: asyncio.Future[RpcResponse] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut

        params = MessageSendParams(message=message, configuration=configuration, metadata=metadata).model_dump(
            by_alias=True, exclude_none=True
        )
        req = RpcRequest(id=request_id, method=METHOD_MESSAGE_STREAM, params=params)
        env = KafkaEnvelope(
            type=EnvelopeType.request,
            correlation_id=request_id,
            sender=self.client_id,
            recipient=agent_name,
            reply_to=self.reply_topic,
            payload=req.model_dump(by_alias=True, exclude_none=True),
        )
        await self._transport.send(topic=self._topics.agent_requests(agent_name), envelope=env)

        timeout = self._cfg.request_timeout_s
        resp = await asyncio.wait_for(fut, timeout=timeout)
        if resp.error is not None:
            self._streams.pop(request_id, None)
            raise A2AError(code=resp.error.code, message=resp.error.message, data=resp.error.data)

        queue = self._streams[request_id]

        async def _iter() -> AsyncIterator[StreamResult]:
            try:
                while True:
                    item = await queue.get()
                    if item.error is not None:
                        raise A2AError(code=item.error.code, message=item.error.message, data=item.error.data)
                    result = _parse_stream_result(item.result)
                    yield result
                    if _is_stream_done(result):
                        break
            finally:
                self._streams.pop(request_id, None)

        return _iter()

    async def subscribe_task(
        self,
        *,
        agent_name: str,
        task_id: str,
        resubscribe: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamResult]:
        request_id = str(uuid4())
        self._streams[request_id] = asyncio.Queue()
        fut: asyncio.Future[RpcResponse] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut

        params = TaskQueryParams(id=task_id, metadata=metadata).model_dump(by_alias=True, exclude_none=True)
        method = METHOD_TASKS_RESUBSCRIBE if resubscribe else METHOD_TASKS_SUBSCRIBE
        req = RpcRequest(id=request_id, method=method, params=params)
        env = KafkaEnvelope(
            type=EnvelopeType.request,
            correlation_id=request_id,
            sender=self.client_id,
            recipient=agent_name,
            reply_to=self.reply_topic,
            payload=req.model_dump(by_alias=True, exclude_none=True),
        )
        await self._transport.send(topic=self._topics.agent_requests(agent_name), envelope=env)

        timeout = self._cfg.request_timeout_s
        resp = await asyncio.wait_for(fut, timeout=timeout)
        if resp.error is not None:
            self._streams.pop(request_id, None)
            raise A2AError(code=resp.error.code, message=resp.error.message, data=resp.error.data)

        queue = self._streams[request_id]

        async def _iter() -> AsyncIterator[StreamResult]:
            try:
                while True:
                    item = await queue.get()
                    if item.error is not None:
                        raise A2AError(code=item.error.code, message=item.error.message, data=item.error.data)
                    result = _parse_stream_result(item.result)
                    yield result
                    if _is_stream_done(result):
                        break
            finally:
                self._streams.pop(request_id, None)

        return _iter()
