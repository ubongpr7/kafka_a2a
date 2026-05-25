from __future__ import annotations

from enum import Enum
from typing import Any

from kafka_a2a.models import Ka2aModel


class ConversationStatus(str, Enum):
    active = "active"
    archived = "archived"
    closed = "closed"


class ConversationMessageRole(str, Enum):
    user = "user"
    assistant = "assistant"
    system = "system"


class ConversationMessageKind(str, Enum):
    text = "text"
    structured = "structured"
    status = "status"


class ConversationMessageRecord(Ka2aModel):
    id: str
    conversation_id: str
    sequence: int
    role: str
    kind: str
    content: str = ""
    structured_payload: dict[str, Any] = {}
    task_id: str | None = None
    context_id: str | None = None
    server_message_id: str | None = None
    metadata: dict[str, Any] = {}
    created_at: str
    updated_at: str


class ConversationActivityRecord(Ka2aModel):
    id: str
    conversation_id: str
    kind: str
    label: str
    detail: str | None = None
    state: str | None = None
    task_id: str | None = None
    context_id: str | None = None
    specialist_slug: str | None = None
    metadata: dict[str, Any] = {}
    received_at: str


class ConversationRecord(Ka2aModel):
    id: str
    profile_id: str
    user_id: str
    title: str = ""
    status: str = ConversationStatus.active.value
    agent_slug: str
    agent_name: str
    agent_icon_url: str = ""
    runtime_agent_name: str
    message_count: int = 0
    history_length: int = 10
    last_message_preview: str = ""
    last_message_at: str | None = None
    last_task_id: str | None = None
    last_context_id: str | None = None
    current_task_state: str | None = None
    active_specialist_slug: str | None = None
    awaiting_input: bool = False
    resume_task_id: str | None = None
    metadata: dict[str, Any] = {}
    created_at: str
    updated_at: str


class ConversationDetail(Ka2aModel):
    conversation: ConversationRecord
    messages: list[ConversationMessageRecord]
    activities: list[ConversationActivityRecord] = []
