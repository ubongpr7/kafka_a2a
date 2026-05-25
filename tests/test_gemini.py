import json

import pytest

from kafka_a2a.llms.gemini import GeminiChatModel
from kafka_a2a.tools import ToolSpec


@pytest.mark.asyncio
async def test_gemini_tool_schema_filters_required_fields_missing_from_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_payload: dict[str, object] = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": "ok",
                                    }
                                ]
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def _fake_urlopen(req, timeout=0):  # noqa: ANN001
        _ = timeout
        captured_payload.update(json.loads(req.data.decode("utf-8")))
        return _FakeResponse()

    monkeypatch.setattr("kafka_a2a.llms.gemini.urlopen", _fake_urlopen)

    model = GeminiChatModel(
        api_key="test-key",
        model="gemini-1.5-flash",
    )
    response = await model.ainvoke(
        [{"role": "user", "content": "use a tool"}],
        tools=[
            ToolSpec(
                name="broken_required_schema",
                description="Test schema cleanup.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "supported": {"type": "string"},
                        "unsupported": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                    "required": ["supported", "unsupported"],
                },
            )
        ],
    )

    assert response.content == "ok"
    assert captured_payload["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "broken_required_schema",
                    "description": "Test schema cleanup.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "supported": {
                                "type": "STRING",
                            },
                            "unsupported": {
                                "type": "STRING",
                            },
                        },
                        "required": ["supported", "unsupported"],
                    },
                }
            ]
        }
    ]


@pytest.mark.asyncio
async def test_gemini_tool_schema_resolves_local_refs_and_nullable_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_payload: dict[str, object] = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": "ok",
                                    }
                                ]
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def _fake_urlopen(req, timeout=0):  # noqa: ANN001
        _ = timeout
        captured_payload.update(json.loads(req.data.decode("utf-8")))
        return _FakeResponse()

    monkeypatch.setattr("kafka_a2a.llms.gemini.urlopen", _fake_urlopen)

    model = GeminiChatModel(
        api_key="test-key",
        model="gemini-1.5-flash",
    )
    response = await model.ainvoke(
        [{"role": "user", "content": "use a tool"}],
        tools=[
            ToolSpec(
                name="create_inventory_item",
                description="Create an inventory item.",
                input_schema={
                    "$defs": {
                        "Payload": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "category_id": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                },
                            },
                            "required": ["name"],
                        }
                    },
                    "type": "object",
                    "properties": {
                        "payload": {"$ref": "#/$defs/Payload"},
                    },
                    "required": ["payload"],
                },
            )
        ],
    )

    assert response.content == "ok"
    assert captured_payload["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "create_inventory_item",
                    "description": "Create an inventory item.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "payload": {
                                "type": "OBJECT",
                                "properties": {
                                    "name": {"type": "STRING"},
                                    "category_id": {"type": "STRING"},
                                },
                                "required": ["name"],
                            }
                        },
                        "required": ["payload"],
                    },
                }
            ]
        }
    ]
