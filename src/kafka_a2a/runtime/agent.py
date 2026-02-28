from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from kafka_a2a.credentials import strip_principal_secrets_for_storage
from kafka_a2a.errors import A2AError, A2AErrorCode
from kafka_a2a.extensions.kafka_transport import KafkaTransportExtensionParams, build_kafka_transport_extension
from kafka_a2a.models import (
    AgentCapabilities,
    AgentCard,
    Artifact,
    Message,
    PushNotificationConfig,
    Role,
    StreamResponse,
    Task,
    TaskConfiguration,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TaskArtifactUpdateEvent,
    TextPart,
)
from kafka_a2a.protocol import (
    METHOD_AGENT_GET_AUTHENTICATED_EXTENDED_CARD,
    METHOD_AGENT_GET_CARD,
    METHOD_MESSAGE_SEND,
    METHOD_MESSAGE_STREAM,
    METHOD_TASKS_CANCEL,
    METHOD_TASKS_GET,
    METHOD_TASKS_LIST,
    METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_DELETE,
    METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_GET,
    METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_LIST,
    METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_SET,
    METHOD_TASKS_RESUBSCRIBE,
    METHOD_TASKS_SUBSCRIBE,
    MessageSendParams,
    RpcError,
    RpcRequest,
    RpcResponse,
    TaskIdParams,
    TaskListParams,
    TaskListResult,
    TaskQueryParams,
    DeleteTaskPushNotificationConfigParams,
    GetTaskPushNotificationConfigParams,
    ListTaskPushNotificationConfigParams,
)
from kafka_a2a.registry.kafka_registry import KafkaAgentRegistry
from kafka_a2a.runtime.task_store import InMemoryTaskStore, TaskEventRecord
from kafka_a2a.tenancy import KA2A_PRINCIPAL_METADATA_KEY, Principal, PrincipalMatcher, extract_principal, with_principal
from kafka_a2a.transport.kafka import EnvelopeType, KafkaEnvelope, KafkaTransport, TopicNamer
from kafka_a2a.processors import echo_processor


TaskProcessor = Callable[
    [Task, Message, TaskConfiguration | None, dict[str, Any] | None],
    AsyncIterator[TaskStatus | Artifact | TaskStatusUpdateEvent | TaskArtifactUpdateEvent],
]

@dataclass(slots=True)
class Ka2aAgentConfig:
    agent_name: str
    description: str | None = None
    url: str | None = None
    version: str = "0.1.0"
    push_notifications: bool = False
    push_delivery_timeout_s: float = 5.0
    push_queue_maxsize: int = 1000
    tenant_isolation: bool = False
    require_tenant_match: bool = True
    principal_metadata_key: str = KA2A_PRINCIPAL_METADATA_KEY
    store_principal_secrets: bool = False
    registry_heartbeat_s: float | None = 60.0
    request_group_id: str | None = None
    max_concurrency: int = 50


class Ka2aAgent:
    def __init__(
        self,
        *,
        config: Ka2aAgentConfig,
        transport: KafkaTransport,
        topic_namer: TopicNamer | None = None,
        registry: KafkaAgentRegistry | None = None,
        task_store: InMemoryTaskStore | None = None,
        processor: TaskProcessor | None = None,
        card: AgentCard | None = None,
    ):
        self._cfg = config
        self._transport = transport
        self._topics = topic_namer or TopicNamer()
        self._registry = registry
        self._store = task_store or InMemoryTaskStore()
        self._processor = processor or echo_processor
        self._consumer_task: asyncio.Task[None] | None = None
        self._registry_task: asyncio.Task[None] | None = None
        self._push_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._sem = asyncio.Semaphore(config.max_concurrency)
        self._processing: dict[str, asyncio.Task[None]] = {}
        self._push_queue: asyncio.Queue[tuple[str, TaskStatusUpdateEvent | TaskArtifactUpdateEvent]] | None = None
        self._principal_matcher = PrincipalMatcher(require_tenant_match=config.require_tenant_match)

        kafka_ext = build_kafka_transport_extension(
            params=KafkaTransportExtensionParams(
                bootstrap_servers=(
                    [self._transport.config.bootstrap_servers]
                    if isinstance(self._transport.config.bootstrap_servers, str)
                    else list(self._transport.config.bootstrap_servers)
                ),
                request_topic=self._topics.agent_requests(config.agent_name),
                registry_topic=(self._registry.topic if self._registry else "ka2a.agent_cards"),
                replies_prefix=self._topics.replies_prefix,
                events_prefix=self._topics.events_prefix,
            )
        )

        if card is not None:
            if card.name != config.agent_name:
                raise ValueError("AgentCard.name must match Ka2aAgentConfig.agent_name")
            if not card.url:
                card.url = config.url or f"kafka://{config.agent_name}"
            card.preferred_transport = "kafka"
            card.capabilities.streaming = True
            card.capabilities.push_notifications = config.push_notifications
            exts = list(card.capabilities.extensions or [])
            exts = [e for e in exts if e.uri != kafka_ext.uri]
            exts.append(kafka_ext)
            card.capabilities.extensions = exts
            self.card = card
        else:
            self.card = AgentCard(
                name=config.agent_name,
                description=config.description or "",
                url=config.url or f"kafka://{config.agent_name}",
                version=config.version,
                preferred_transport="kafka",
                capabilities=AgentCapabilities(
                    streaming=True,
                    push_notifications=config.push_notifications,
                    extensions=[kafka_ext],
                ),
            )

    @property
    def request_topic(self) -> str:
        return self._topics.agent_requests(self._cfg.agent_name)

    async def start(self) -> None:
        await self._transport.start()

        if self.card.capabilities.push_notifications:
            self._push_queue = asyncio.Queue(maxsize=self._cfg.push_queue_maxsize)
            self._push_task = asyncio.create_task(self._push_loop())

        if self._registry is not None:
            await self._registry.publish(agent_name=self.card.name, card=self.card)
            if self._cfg.registry_heartbeat_s is not None and self._cfg.registry_heartbeat_s > 0:
                async def _heartbeat() -> None:
                    try:
                        while not self._stop.is_set():
                            try:
                                await asyncio.wait_for(self._stop.wait(), timeout=self._cfg.registry_heartbeat_s)
                                break
                            except asyncio.TimeoutError:
                                await self._registry.publish(agent_name=self.card.name, card=self.card)
                    except asyncio.CancelledError:
                        raise

                self._registry_task = asyncio.create_task(_heartbeat())

        group_id = self._cfg.request_group_id or f"ka2a.agent.{self._cfg.agent_name}"
        consumer = self._transport.create_consumer(
            topics=[self.request_topic],
            group_id=group_id,
            auto_offset_reset="latest",
        )
        await consumer.start()

        async def _loop() -> None:
            try:
                async for msg in consumer:
                    try:
                        env = KafkaEnvelope.from_bytes(msg.value)
                    except Exception:
                        continue
                    if env.type != EnvelopeType.request:
                        continue
                    asyncio.create_task(self._dispatch_request(env))
                    if self._stop.is_set():
                        break
            finally:
                await consumer.stop()

        self._consumer_task = asyncio.create_task(_loop())

    async def stop(self) -> None:
        self._stop.set()
        for task in list(self._processing.values()):
            task.cancel()
        if self._push_task is not None:
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass
            self._push_task = None
        self._push_queue = None
        if self._registry_task is not None:
            self._registry_task.cancel()
            try:
                await self._registry_task
            except asyncio.CancelledError:
                pass
            self._registry_task = None
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            self._consumer_task = None
        await self._transport.stop()

    async def _dispatch_request(self, env: KafkaEnvelope) -> None:
        async with self._sem:
            await self._handle_request(env)

    async def _handle_request(self, env: KafkaEnvelope) -> None:
        reply_to = env.reply_to
        if not reply_to:
            return
        try:
            req = RpcRequest.model_validate(env.payload)
        except Exception as exc:
            # Mirror JSON-RPC: invalid request -> id is null when it cannot be determined.
            response = RpcResponse(
                id=None,
                error=RpcError(code=A2AErrorCode.INVALID_REQUEST, message="Invalid Request", data={"detail": str(exc)}),
            )
            await self._transport.send(
                topic=reply_to,
                envelope=KafkaEnvelope(
                    type=EnvelopeType.response,
                    correlation_id=env.correlation_id,
                    sender=self.card.name,
                    recipient=env.sender,
                    payload=response.to_jsonrpc_dict(),
                ),
                key=str(env.correlation_id).encode("utf-8") if env.correlation_id is not None else None,
            )
            return

        try:
            result = await self._route(req, env)
            response = RpcResponse(id=req.id, result=result)
        except Exception as exc:
            response = RpcResponse(id=req.id, error=RpcError.from_exc(exc))

        await self._transport.send(
            topic=reply_to,
            envelope=KafkaEnvelope(
                type=EnvelopeType.response,
                correlation_id=req.id,
                sender=self.card.name,
                recipient=env.sender,
                payload=response.to_jsonrpc_dict(),
            ),
            key=str(req.id).encode("utf-8"),
        )

    async def _route(self, req: RpcRequest, env: KafkaEnvelope) -> Any:
        method = req.method
        params = req.params or {}

        if method == METHOD_AGENT_GET_CARD:
            return self.card.model_dump(by_alias=True, exclude_none=True)
        if method == METHOD_AGENT_GET_AUTHENTICATED_EXTENDED_CARD:
            return self.card.model_dump(by_alias=True, exclude_none=True)

        if method == METHOD_MESSAGE_SEND:
            p = MessageSendParams.model_validate(params)
            request_metadata = p.metadata or {}
            if self._cfg.tenant_isolation:
                principal = self._require_principal(request_metadata)
                request_metadata = with_principal(
                    request_metadata, principal, key=self._cfg.principal_metadata_key
                )
            if p.configuration and p.configuration.push_notification_config is not None:
                if not self.card.capabilities.push_notifications:
                    raise A2AError(
                        A2AErrorCode.PUSH_NOTIFICATION_NOT_SUPPORTED, "Push Notification is not supported"
                    )
            stored_metadata = request_metadata
            if not self._cfg.store_principal_secrets:
                stored_metadata = strip_principal_secrets_for_storage(
                    metadata=request_metadata,
                    principal_metadata_key=self._cfg.principal_metadata_key,
                ) or {}
            task = await self._store.create_task(initial_message=p.message, metadata=stored_metadata or None)
            if p.configuration and p.configuration.push_notification_config is not None:
                await self._store.set_push_notification_config(
                    task_id=task.id, config=p.configuration.push_notification_config
                )
            self._start_processing(
                task=task,
                message=p.message,
                configuration=p.configuration,
                metadata=request_metadata or None,
            )
            return task.model_dump(by_alias=True, exclude_none=True)

        if method == METHOD_MESSAGE_STREAM:
            p = MessageSendParams.model_validate(params)
            if not env.reply_to:
                raise A2AError(A2AErrorCode.INVALID_PARAMS, "replyTo is required for streaming")
            request_metadata = p.metadata or {}
            if self._cfg.tenant_isolation:
                principal = self._require_principal(request_metadata)
                request_metadata = with_principal(
                    request_metadata, principal, key=self._cfg.principal_metadata_key
                )
            if p.configuration and p.configuration.push_notification_config is not None:
                if not self.card.capabilities.push_notifications:
                    raise A2AError(
                        A2AErrorCode.PUSH_NOTIFICATION_NOT_SUPPORTED, "Push Notification is not supported"
                    )
            stored_metadata = request_metadata
            if not self._cfg.store_principal_secrets:
                stored_metadata = strip_principal_secrets_for_storage(
                    metadata=request_metadata,
                    principal_metadata_key=self._cfg.principal_metadata_key,
                ) or {}
            task = await self._store.create_task(initial_message=p.message, metadata=stored_metadata or None)
            if p.configuration and p.configuration.push_notification_config is not None:
                await self._store.set_push_notification_config(
                    task_id=task.id, config=p.configuration.push_notification_config
                )
            await self._begin_stream(
                request_id=req.id,
                reply_to=env.reply_to,
                task_id=task.id,
                replay_history=True,
                include_task=False,
            )
            self._start_processing(
                task=task,
                message=p.message,
                configuration=p.configuration,
                metadata=request_metadata or None,
            )
            return task.model_dump(by_alias=True, exclude_none=True)

        if method == METHOD_TASKS_GET:
            p = TaskQueryParams.model_validate(params)
            task = await self._store.get_task(p.id)
            if task is None:
                raise A2AError(A2AErrorCode.TASK_NOT_FOUND, "Task not found", {"id": p.id})
            if self._cfg.tenant_isolation:
                principal = self._require_principal(p.metadata or {})
                self._enforce_task_access(task, principal)
            return task.model_dump(by_alias=True, exclude_none=True)

        if method == METHOD_TASKS_LIST:
            p = TaskListParams.model_validate(params)
            tasks = await self._store.list_tasks()
            if self._cfg.tenant_isolation:
                principal = self._require_principal(p.metadata or {})
                tasks = [
                    t
                    for t in tasks
                    if (stored := self._task_principal(t))
                    and self._principal_matcher.matches(stored=stored, request=principal)
                ]
            if p.status:
                tasks = [t for t in tasks if t.status.state.value == p.status]
            total = len(tasks)
            if p.offset:
                tasks = tasks[p.offset :]
            if p.limit:
                tasks = tasks[: p.limit]
            return TaskListResult(tasks=tasks, total=total).model_dump(by_alias=True, exclude_none=True)

        if method == METHOD_TASKS_CANCEL:
            p = TaskIdParams.model_validate(params)
            task = await self._store.get_task(p.id)
            if task is None:
                raise A2AError(A2AErrorCode.TASK_NOT_FOUND, "Task not found", {"id": p.id})
            if self._cfg.tenant_isolation:
                principal = self._require_principal(p.metadata or {})
                self._enforce_task_access(task, principal)
            proc = self._processing.get(p.id)
            if proc is not None and not proc.done():
                proc.cancel()
            status = TaskStatus(state=TaskState.canceled, message=task.status.message)
            cancel_event = await self._store.append_status(task_id=p.id, status=status)
            self._enqueue_push(task_id=p.id, event=cancel_event)
            updated = await self._store.get_task(p.id)
            assert updated is not None
            return updated.model_dump(by_alias=True, exclude_none=True)

        if method in (METHOD_TASKS_SUBSCRIBE, METHOD_TASKS_RESUBSCRIBE):
            p = TaskIdParams.model_validate(params)
            if not env.reply_to:
                raise A2AError(A2AErrorCode.INVALID_PARAMS, "replyTo is required for subscribe")
            task = await self._store.get_task(p.id)
            if task is None:
                raise A2AError(A2AErrorCode.TASK_NOT_FOUND, "Task not found", {"id": p.id})
            if self._cfg.tenant_isolation:
                principal = self._require_principal(p.metadata or {})
                self._enforce_task_access(task, principal)
            await self._begin_stream(
                request_id=req.id,
                reply_to=env.reply_to,
                task_id=p.id,
                replay_history=True,
                include_task=False,
            )
            return task.model_dump(by_alias=True, exclude_none=True)

        if method in (
            METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_SET,
            METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_GET,
            METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_LIST,
            METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_DELETE,
        ):
            if not self.card.capabilities.push_notifications:
                raise A2AError(
                    A2AErrorCode.PUSH_NOTIFICATION_NOT_SUPPORTED, "Push Notification is not supported"
                )

            if method == METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_SET:
                # Spec: params = TaskPushNotificationConfig
                try:
                    task_id = params.get("taskId") or params.get("task_id")  # type: ignore[union-attr]
                    push_cfg = params.get("pushNotificationConfig") or params.get("push_notification_config")  # type: ignore[union-attr]
                    request_metadata = params.get("metadata") if isinstance(params, dict) else None
                except Exception:
                    raise A2AError(A2AErrorCode.INVALID_PARAMS, "Invalid params")
                if not task_id or not push_cfg:
                    raise A2AError(A2AErrorCode.INVALID_PARAMS, "taskId and pushNotificationConfig are required")
                if self._cfg.tenant_isolation:
                    principal = self._require_principal(request_metadata or {})
                    task = await self._store.get_task(str(task_id))
                    if task is None:
                        raise A2AError(A2AErrorCode.TASK_NOT_FOUND, "Task not found", {"id": str(task_id)})
                    self._enforce_task_access(task, principal)
                cfg = PushNotificationConfig.model_validate(push_cfg)
                try:
                    result = await self._store.set_push_notification_config(task_id=str(task_id), config=cfg)
                except KeyError:
                    raise A2AError(A2AErrorCode.TASK_NOT_FOUND, "Task not found", {"id": str(task_id)})
                return result.model_dump(by_alias=True, exclude_none=True)

            if method == METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_GET:
                p = GetTaskPushNotificationConfigParams.model_validate(params)
                task = await self._store.get_task(p.id)
                if task is None:
                    raise A2AError(A2AErrorCode.TASK_NOT_FOUND, "Task not found", {"id": p.id})
                if self._cfg.tenant_isolation:
                    principal = self._require_principal(p.metadata or {})
                    self._enforce_task_access(task, principal)
                result = await self._store.get_push_notification_config(
                    task_id=p.id, config_id=p.push_notification_config_id
                )
                if result is None:
                    raise A2AError(A2AErrorCode.INVALID_PARAMS, "Push notification config not found")
                return result.model_dump(by_alias=True, exclude_none=True)

            if method == METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_LIST:
                p = ListTaskPushNotificationConfigParams.model_validate(params)
                task = await self._store.get_task(p.id)
                if task is None:
                    raise A2AError(A2AErrorCode.TASK_NOT_FOUND, "Task not found", {"id": p.id})
                if self._cfg.tenant_isolation:
                    principal = self._require_principal(p.metadata or {})
                    self._enforce_task_access(task, principal)
                result = await self._store.list_push_notification_configs(task_id=p.id)
                return [r.model_dump(by_alias=True, exclude_none=True) for r in result]

            if method == METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_DELETE:
                p = DeleteTaskPushNotificationConfigParams.model_validate(params)
                task = await self._store.get_task(p.id)
                if task is None:
                    raise A2AError(A2AErrorCode.TASK_NOT_FOUND, "Task not found", {"id": p.id})
                if self._cfg.tenant_isolation:
                    principal = self._require_principal(p.metadata or {})
                    self._enforce_task_access(task, principal)
                await self._store.delete_push_notification_config(
                    task_id=p.id, config_id=p.push_notification_config_id
                )
                return None

            raise A2AError(A2AErrorCode.METHOD_NOT_FOUND, f"Unknown method: {method}")

        raise A2AError(A2AErrorCode.METHOD_NOT_FOUND, f"Unknown method: {method}")

    def _require_principal(self, metadata: dict[str, Any]) -> Principal:
        principal = extract_principal(metadata, key=self._cfg.principal_metadata_key)
        if principal is None:
            raise A2AError(A2AErrorCode.UNAUTHENTICATED, "Unauthenticated")
        return principal

    def _task_principal(self, task: Task) -> Principal | None:
        return extract_principal(task.metadata, key=self._cfg.principal_metadata_key)

    def _enforce_task_access(self, task: Task, principal: Principal) -> None:
        stored = self._task_principal(task)
        if stored is None:
            raise A2AError(A2AErrorCode.PERMISSION_DENIED, "Permission denied")
        if not self._principal_matcher.matches(stored=stored, request=principal):
            raise A2AError(A2AErrorCode.PERMISSION_DENIED, "Permission denied")

    def _start_processing(
        self,
        *,
        task: Task,
        message: Message,
        configuration: TaskConfiguration | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        async def _run() -> None:
            try:
                working_event = await self._store.append_status(
                    task_id=task.id,
                    status=TaskStatus(
                        state=TaskState.working,
                        message=Message(role=Role.agent, parts=[TextPart(text="working")]),
                    ),
                )
                self._enqueue_push(task_id=task.id, event=working_event)
                async for event in self._processor(task, message, configuration, metadata):
                    if isinstance(event, TaskStatus):
                        status_event = await self._store.append_status(task_id=task.id, status=event)
                        self._enqueue_push(task_id=task.id, event=status_event)
                        continue
                    if isinstance(event, Artifact):
                        artifact_event = await self._store.append_artifact(task_id=task.id, artifact=event)
                        self._enqueue_push(task_id=task.id, event=artifact_event)
                        continue
                    if isinstance(event, TaskStatusUpdateEvent):
                        status_event = await self._store.append_status(task_id=task.id, status=event.status)
                        self._enqueue_push(task_id=task.id, event=status_event)
                        continue
                    if isinstance(event, TaskArtifactUpdateEvent):
                        artifact_event = await self._store.append_artifact(task_id=task.id, artifact=event.artifact)
                        self._enqueue_push(task_id=task.id, event=artifact_event)
                        continue
                    raise TypeError(f"Unsupported task event type: {type(event).__name__}")

                completed_event = await self._store.append_status(
                    task_id=task.id,
                    status=TaskStatus(
                        state=TaskState.completed,
                        message=Message(role=Role.agent, parts=[TextPart(text="completed")]),
                    ),
                )
                self._enqueue_push(task_id=task.id, event=completed_event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed_event = await self._store.append_status(
                    task_id=task.id,
                    status=TaskStatus(
                        state=TaskState.failed,
                        message=Message(role=Role.agent, parts=[TextPart(text=str(exc))]),
                    ),
                )
                self._enqueue_push(task_id=task.id, event=failed_event)
            finally:
                self._processing.pop(task.id, None)

        self._processing[task.id] = asyncio.create_task(_run())

    async def _begin_stream(
        self,
        *,
        request_id: str | int,
        reply_to: str,
        task_id: str,
        replay_history: bool,
        include_task: bool,
    ) -> None:
        async def _forward() -> None:
            queue, history = await self._store.subscribe(task_id)
            try:
                records = history if replay_history else []
                for record in records:
                    if not include_task and isinstance(record.event, Task):
                        continue
                    await self._send_stream_result(reply_to, request_id, record)
                    if isinstance(record.event, TaskStatusUpdateEvent) and record.event.final:
                        return

                while True:
                    record = await queue.get()
                    if not include_task and isinstance(record.event, Task):
                        continue
                    await self._send_stream_result(reply_to, request_id, record)
                    if isinstance(record.event, TaskStatusUpdateEvent) and record.event.final:
                        break
            finally:
                await self._store.unsubscribe(task_id, queue)

        asyncio.create_task(_forward())

    async def _send_stream_result(
        self, reply_to: str, request_id: str | int, record: TaskEventRecord
    ) -> None:
        result = record.event.model_dump(mode="json", by_alias=True, exclude_none=True)
        response = RpcResponse(id=request_id, result=result)
        await self._transport.send(
            topic=reply_to,
            envelope=KafkaEnvelope(
                type=EnvelopeType.response,
                correlation_id=request_id,
                sender=self.card.name,
                recipient=None,
                payload=response.to_jsonrpc_dict(),
            ),
            key=str(request_id).encode("utf-8"),
        )

    def _enqueue_push(
        self,
        *,
        task_id: str,
        event: TaskStatusUpdateEvent | TaskArtifactUpdateEvent,
    ) -> None:
        queue = self._push_queue
        if queue is None:
            return
        try:
            queue.put_nowait((task_id, event))
        except asyncio.QueueFull:
            # Best-effort; drop on overload.
            pass

    async def _push_loop(self) -> None:
        assert self._push_queue is not None
        while True:
            task_id, event = await self._push_queue.get()
            try:
                configs = await self._store.list_push_notification_configs(task_id=task_id)
                if not configs:
                    continue
                stream = (
                    StreamResponse(status_update=event)
                    if isinstance(event, TaskStatusUpdateEvent)
                    else StreamResponse(artifact_update=event)
                )
                body_obj = stream.model_dump(mode="json", by_alias=True, exclude_none=True)
                body = json.dumps(body_obj, separators=(",", ":")).encode("utf-8")
                for item in configs:
                    await self._deliver_push(
                        task_id=task_id,
                        config=item.push_notification_config,
                        body_obj=body_obj,
                        body=body,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Push notifications are best-effort; ignore failures.
                continue

    async def _deliver_push(
        self,
        *,
        task_id: str,
        config: PushNotificationConfig,
        body_obj: dict[str, Any],
        body: bytes,
    ) -> None:
        parsed = urlparse(config.url)
        if parsed.scheme in ("http", "https"):
            headers = {"content-type": "application/json"}
            if config.token:
                headers["x-a2a-token"] = config.token
            if config.authentication and config.authentication.credentials and config.authentication.schemes:
                scheme = config.authentication.schemes[0]
                headers["authorization"] = f"{scheme} {config.authentication.credentials}"

            timeout = max(0.1, float(self._cfg.push_delivery_timeout_s))

            def _post() -> None:
                req = Request(config.url, data=body, headers=headers, method="POST")
                with urlopen(req, timeout=timeout) as resp:  # noqa: S310
                    resp.read()

            await asyncio.to_thread(_post)
            return

        if parsed.scheme == "kafka":
            topic = parsed.netloc or parsed.path.lstrip("/")
            if not topic:
                return
            await self._transport.send(
                topic=topic,
                envelope=KafkaEnvelope(
                    type=EnvelopeType.event,
                    correlation_id=task_id,
                    sender=self.card.name,
                    recipient=None,
                    content_type="application/a2a-stream+json",
                    payload=body_obj,
                ),
                key=task_id.encode("utf-8"),
            )

    async def _send_error(
        self,
        *,
        reply_to: str,
        request_id: str | int | None,
        rpc_id: str | int | None,
        exc: Exception,
        recipient: str | None,
    ) -> None:
        response = RpcResponse(id=rpc_id, error=RpcError.from_exc(exc))
        await self._transport.send(
            topic=reply_to,
            envelope=KafkaEnvelope(
                type=EnvelopeType.response,
                correlation_id=request_id,
                sender=self.card.name,
                recipient=recipient,
                payload=response.to_jsonrpc_dict(),
            ),
        )
