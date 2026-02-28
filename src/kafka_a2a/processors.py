from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Callable

from kafka_a2a.models import (
    Artifact,
    Message,
    Task,
    TaskArtifactUpdateEvent,
    TaskConfiguration,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)


TaskEvent = TaskStatus | Artifact | TaskStatusUpdateEvent | TaskArtifactUpdateEvent
TaskProcessor = Callable[[Task, Message, TaskConfiguration | None, dict[str, Any] | None], AsyncIterator[TaskEvent]]


async def echo_processor(
    task: Task,
    message: Message,
    configuration: TaskConfiguration | None,
    metadata: dict[str, Any] | None,
) -> AsyncIterator[TaskEvent]:
    text = "\n".join([part.text for part in message.parts if isinstance(part, TextPart)])
    artifact = Artifact(name="result", parts=[TextPart(text=f"echo: {text}")])
    yield artifact


def make_prompted_echo_processor(*, system_prompt: str | None) -> TaskProcessor:
    system_text = (system_prompt or "").strip()

    async def _proc(
        task: Task,
        message: Message,
        configuration: TaskConfiguration | None,
        metadata: dict[str, Any] | None,
    ) -> AsyncIterator[TaskEvent]:
        user_text = "\n".join([part.text for part in message.parts if isinstance(part, TextPart)]).strip()
        combined = user_text
        if system_text:
            combined = f"{system_text}\n\n{user_text}" if user_text else system_text
        artifact = Artifact(name="result", parts=[TextPart(text=f"echo: {combined}")])
        yield artifact

    return _proc

