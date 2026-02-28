from __future__ import annotations

import base64

import pytest

from kafka_a2a.models import (
    DataPart,
    FilePart,
    FileWithBytes,
    Message,
    TaskState,
    TaskStatus,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)


def test_message_alias_roundtrip() -> None:
    msg = Message(
        role="user",
        parts=[
            TextPart(text="hello"),
            DataPart(data={"x": 1}),
        ],
    )

    dumped = msg.model_dump(by_alias=True, exclude_none=True)
    assert dumped["role"] == "user"
    assert dumped["parts"][0]["kind"] == "text"
    assert dumped["parts"][0]["text"] == "hello"
    assert dumped["parts"][1]["kind"] == "data"
    assert dumped["parts"][1]["data"]["x"] == 1

    loaded = Message.model_validate(dumped)
    assert loaded.role == "user"
    assert isinstance(loaded.parts[0], TextPart)
    assert isinstance(loaded.parts[1], DataPart)


def test_file_with_bytes_requires_base64() -> None:
    with pytest.raises(Exception):
        FileWithBytes(bytes="not-base64", mime_type="text/plain")

    b64 = base64.b64encode(b"abc").decode("utf-8")
    ok = FileWithBytes(bytes=b64, mime_type="text/plain")
    part = FilePart(file=ok)
    assert part.file.mime_type == "text/plain"


def test_task_state_enum_values() -> None:
    status = TaskStatus(state=TaskState.working)
    assert status.state.value == "working"


def test_tool_parts_roundtrip() -> None:
    msg = Message(
        role="agent",
        parts=[
            ToolCallPart(name="inventory.search", arguments={"q": "bolts"}),
            ToolResultPart(tool_call_id="call-1", output={"ok": True}),
        ],
    )
    dumped = msg.model_dump(by_alias=True, exclude_none=True)
    assert dumped["parts"][0]["kind"] == "tool-call"
    assert dumped["parts"][0]["name"] == "inventory.search"
    assert dumped["parts"][1]["kind"] == "tool-result"
    assert dumped["parts"][1]["toolCallId"] == "call-1"
    loaded = Message.model_validate(dumped)
    assert isinstance(loaded.parts[0], ToolCallPart)
    assert isinstance(loaded.parts[1], ToolResultPart)
