from __future__ import annotations

from typing import Any

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kafka_a2a.errors import A2AError
from kafka_a2a.models import (
    AgentCard,
    Message,
    PushNotificationConfig,
    Task,
    TaskConfiguration,
    TaskEvent,
)


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(word[:1].upper() + word[1:] for word in parts[1:])


class Ka2aProtocolModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="allow",
    )


# Method names (mirrors A2A JSON-RPC method naming)
METHOD_AGENT_GET_CARD = "agent/getCard"
METHOD_AGENT_GET_AUTHENTICATED_EXTENDED_CARD = "agent/getAuthenticatedExtendedCard"
METHOD_MESSAGE_SEND = "message/send"
METHOD_MESSAGE_STREAM = "message/stream"
METHOD_TASKS_GET = "tasks/get"
METHOD_TASKS_LIST = "tasks/list"
METHOD_TASKS_CANCEL = "tasks/cancel"
METHOD_TASKS_CONTINUE = "tasks/continue"
METHOD_TASKS_CONTINUE_STREAM = "tasks/continueStream"
# NOTE: `tasks/subscribe` is not in the A2A v0.3.0 JSON-RPC surface. Kept as a
# transport extension alias for early K-A2A experimentation.
METHOD_TASKS_SUBSCRIBE = "tasks/subscribe"
METHOD_TASKS_RESUBSCRIBE = "tasks/resubscribe"
METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_SET = "tasks/pushNotificationConfig/set"
METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_GET = "tasks/pushNotificationConfig/get"
METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_LIST = "tasks/pushNotificationConfig/list"
METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_DELETE = "tasks/pushNotificationConfig/delete"


class RpcRequest(Ka2aProtocolModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int
    method: str
    params: dict[str, Any] | None = None


class RpcError(Ka2aProtocolModel):
    code: int
    message: str
    data: Any | None = None

    @classmethod
    def from_exc(cls, exc: Exception) -> "RpcError":
        if isinstance(exc, A2AError):
            return cls(code=exc.code, message=exc.message, data=exc.data)
        return cls(code=-32603, message="Internal error", data={"detail": str(exc)})


class RpcResponse(Ka2aProtocolModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    result: Any | None = None
    error: RpcError | None = None

    @model_validator(mode="after")
    def _validate_one_of(self) -> "RpcResponse":
        has_result = "result" in self.model_fields_set
        has_error = "error" in self.model_fields_set
        if has_result == has_error:
            raise ValueError("Exactly one of result or error must be set")
        if has_error and self.error is None:
            raise ValueError("error must be set when provided")
        return self

    def to_jsonrpc_dict(self) -> dict[str, Any]:
        """
        JSON-RPC responses must include exactly one of `result` or `error`.

        `exclude_none=True` cannot be used directly because `result` is allowed to be null.
        """

        payload: dict[str, Any] = {"jsonrpc": self.jsonrpc, "id": self.id}
        if "error" in self.model_fields_set:
            assert self.error is not None
            payload["error"] = self.error.model_dump(by_alias=True, exclude_none=True)
            return payload
        payload["result"] = self.result
        return payload


class AgentGetCardResult(Ka2aProtocolModel):
    card: AgentCard


class MessageSendParams(Ka2aProtocolModel):
    message: Message
    configuration: TaskConfiguration | None = None
    metadata: dict[str, Any] | None = None


class MessageSendResult(Ka2aProtocolModel):
    task: Task


class MessageStreamParams(MessageSendParams):
    pass


class TaskIdParams(Ka2aProtocolModel):
    id: str
    metadata: dict[str, Any] | None = None


class TaskQueryParams(TaskIdParams):
    history_length: int | None = None
    include_artifacts: bool | None = None


class TaskGetResult(Task):
    pass


class TaskListParams(Ka2aProtocolModel):
    limit: int | None = None
    offset: int | None = None
    status: str | None = None
    context_id: str | None = None
    include_artifacts: bool | None = None
    metadata: dict[str, Any] | None = None


class TaskListResult(Ka2aProtocolModel):
    tasks: list[Task] = Field(default_factory=list)
    total: int | None = None


class TaskCancelResult(Task):
    pass


class TaskContinueParams(Ka2aProtocolModel):
    id: str
    message: Message
    configuration: TaskConfiguration | None = None
    metadata: dict[str, Any] | None = None


class TaskSubscribeParams(TaskQueryParams):
    pass


class TaskResubscribeParams(TaskQueryParams):
    pass


class TaskStreamAck(Ka2aProtocolModel):
    """
    A Kafka-transport acknowledgement for streaming operations.

    The actual stream items are delivered as Kafka `event` envelopes (TaskEvent payloads)
    correlated by `request_id`.
    """

    request_id: str
    task: Task


class StreamEvent(Ka2aProtocolModel):
    request_id: str
    task_id: str
    sequence: int
    event: TaskEvent
    done: bool = False


class PushNotificationConfigSetParams(Ka2aProtocolModel):
    task_id: str
    push_notification_config: PushNotificationConfig


class GetTaskPushNotificationConfigParams(Ka2aProtocolModel):
    id: str
    metadata: dict[str, Any] | None = None
    push_notification_config_id: str | None = None


class ListTaskPushNotificationConfigParams(Ka2aProtocolModel):
    id: str
    metadata: dict[str, Any] | None = None


class DeleteTaskPushNotificationConfigParams(Ka2aProtocolModel):
    id: str
    metadata: dict[str, Any] | None = None
    push_notification_config_id: str | None = None
