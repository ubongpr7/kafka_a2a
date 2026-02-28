from __future__ import annotations

import base64
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(word[:1].upper() + word[1:] for word in parts[1:])


class Ka2aModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="allow",
        frozen=False,
    )


class AgentProvider(Ka2aModel):
    organization: str | None = None
    url: str | None = None


class AgentSecurityScheme(Ka2aModel):
    """
    A2A defines auth schemes; for now K-A2A focuses on unauthenticated Kafka.
    """

    type: str
    description: str | None = None


class AgentCapabilities(Ka2aModel):
    extensions: list["AgentExtension"] | None = None
    streaming: bool | None = None
    push_notifications: bool | None = None
    state_transition_history: bool | None = None


class AgentExtension(Ka2aModel):
    uri: str
    description: str | None = None
    params: dict[str, Any] | None = None
    required: bool | None = None


class AgentSkill(Ka2aModel):
    id: str
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    input_modes: list[str] = Field(default_factory=lambda: ["text"])
    output_modes: list[str] = Field(default_factory=lambda: ["text"])


class AgentInterface(Ka2aModel):
    transport: str
    url: str


class AgentCardSignature(Ka2aModel):
    """
    A2A: AgentCardSignature (JWS signature).

    K-A2A does not currently generate or verify signatures, but supports the field for parity.
    """

    header: dict[str, Any] | None = None
    protected: str
    signature: str


class AgentCard(Ka2aModel):
    protocol_version: str = "0.3.0"
    name: str
    description: str = ""
    url: str = ""
    preferred_transport: str = "kafka"
    additional_interfaces: list[AgentInterface] | None = None

    provider: AgentProvider | None = None
    version: str = "0.0.0"
    documentation_url: str | None = None
    icon_url: str | None = None
    signatures: list[AgentCardSignature] | None = None

    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    security_schemes: dict[str, AgentSecurityScheme] = Field(default_factory=dict)
    security: list[dict[str, list[str]]] = Field(default_factory=list)
    supports_authenticated_extended_card: bool | None = None

    default_input_modes: list[str] = Field(default_factory=lambda: ["text"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["text"])

    skills: list[AgentSkill] = Field(default_factory=list)


class PushNotificationConfig(Ka2aModel):
    """
    A2A: PushNotificationConfig
    """

    id: str | None = None
    url: str
    token: str | None = None
    authentication: PushNotificationAuthenticationInfo | None = None


class PushNotificationAuthenticationInfo(Ka2aModel):
    """
    A2A: PushNotificationAuthenticationInfo
    """

    schemes: list[str]
    credentials: str | None = None


class TaskPushNotificationConfig(Ka2aModel):
    """
    A2A: TaskPushNotificationConfig
    """

    task_id: str
    push_notification_config: PushNotificationConfig


class TaskConfiguration(Ka2aModel):
    accepted_output_modes: list[str] | None = None
    history_length: int | None = None
    push_notification_config: PushNotificationConfig | None = None
    blocking: bool | None = None


class TaskState(str, Enum):
    unknown = "unknown"
    submitted = "submitted"
    working = "working"
    input_required = "input-required"
    auth_required = "auth-required"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"
    rejected = "rejected"


class TextPart(Ka2aModel):
    kind: Literal["text"] = "text"
    text: str
    metadata: dict[str, Any] | None = None


class FileWithUri(Ka2aModel):
    uri: str
    mime_type: str | None = None


class FileWithBytes(Ka2aModel):
    bytes: str
    mime_type: str | None = None

    @field_validator("bytes")
    @classmethod
    def _validate_b64(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except Exception as exc:  # pragma: no cover (pydantic message differs per python)
            raise ValueError("bytes must be base64-encoded") from exc
        return value


class FilePart(Ka2aModel):
    kind: Literal["file"] = "file"
    file: Union[FileWithUri, FileWithBytes]
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _coerce_file_union(self) -> "FilePart":
        # If both `uri` and `bytes` appear, prefer `uri` (common for large payloads).
        file_obj = self.file
        if isinstance(file_obj, Ka2aModel):
            return self
        return self


class DataPart(Ka2aModel):
    kind: Literal["data"] = "data"
    data: dict[str, Any]
    metadata: dict[str, Any] | None = None


Part = Union[TextPart, FilePart, DataPart]


class Role(str, Enum):
    agent = "agent"
    user = "user"


class Message(Ka2aModel):
    kind: Literal["message"] = "message"
    role: Role
    parts: list[Part]
    metadata: dict[str, Any] | None = None
    extensions: list[str] | None = None
    reference_task_ids: list[str] | None = None
    message_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str | None = None
    context_id: str | None = None


class TaskStatus(Ka2aModel):
    state: TaskState
    message: Message | None = None
    timestamp: datetime = Field(default_factory=_utc_now)


class Artifact(Ka2aModel):
    artifact_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str | None = None
    description: str | None = None
    extensions: list[str] | None = None
    parts: list[Part] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


class Task(Ka2aModel):
    kind: Literal["task"] = "task"
    id: str
    context_id: str
    status: TaskStatus
    history: list[Message] | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


class TaskStatusUpdateEvent(Ka2aModel):
    kind: Literal["status-update"] = "status-update"
    task_id: str
    context_id: str
    status: TaskStatus
    final: bool = False
    metadata: dict[str, Any] | None = None


class TaskArtifactUpdateEvent(Ka2aModel):
    kind: Literal["artifact-update"] = "artifact-update"
    task_id: str
    context_id: str
    artifact: Artifact
    append: bool | None = None
    last_chunk: bool | None = None
    metadata: dict[str, Any] | None = None


TaskUpdate = Union[TaskStatusUpdateEvent, TaskArtifactUpdateEvent]
TaskEvent = Union[Task, TaskUpdate]


class StreamResponse(Ka2aModel):
    """
    A2A v1-style push/stream payload: exactly one of the fields is present.

    This is used for webhook push notifications and can also be used as a transport-agnostic
    streaming envelope outside JSON-RPC.
    """

    task: Task | None = None
    message: Message | None = None
    status_update: TaskStatusUpdateEvent | None = None
    artifact_update: TaskArtifactUpdateEvent | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "StreamResponse":
        present = [
            name
            for name in ("task", "message", "status_update", "artifact_update")
            if getattr(self, name) is not None
        ]
        if len(present) != 1:
            raise ValueError("Exactly one of task, message, status_update, artifact_update must be set")
        return self
