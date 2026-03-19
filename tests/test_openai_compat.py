import json

import pytest

from kafka_a2a.credentials import ResolvedLlmCredentials
from kafka_a2a.llms.openai_compat import OpenAICompatChatModel, create_chat_model
from kafka_a2a.tools import ToolSpec


def test_create_chat_model_defaults_openai_base_url_for_chatgpt_provider() -> None:
    model = create_chat_model(
        ResolvedLlmCredentials(
            provider="chatgpt",
            api_key="test-key",
            model="gpt-5-mini",
            base_url=None,
        )
    )

    assert model.base_url == "https://api.openai.com"


def test_create_chat_model_defaults_xai_base_url_for_grok_provider() -> None:
    model = create_chat_model(
        ResolvedLlmCredentials(
            provider="grok",
            api_key="test-key",
            model="grok-4",
            base_url=None,
        )
    )

    assert model.base_url == "https://api.x.ai"


@pytest.mark.asyncio
async def test_openai_compat_ainvoke_sends_and_parses_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_payload: dict[str, object] = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_123",
                                        "type": "function",
                                        "function": {
                                            "name": "create_multiple_choice",
                                            "arguments": json.dumps(
                                                {
                                                    "question": "Pick one",
                                                    "choices": ["Products", "Inventory"],
                                                }
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def _fake_urlopen(req, timeout=0):  # noqa: ANN001
        _ = timeout
        captured_payload.update(json.loads(req.data.decode("utf-8")))
        return _FakeResponse()

    monkeypatch.setattr("kafka_a2a.llms.openai_compat.urlopen", _fake_urlopen)

    model = OpenAICompatChatModel(
        base_url="https://api.openai.com",
        api_key="test-key",
        model="gpt-4.1-mini",
    )
    response = await model.ainvoke(
        [{"role": "user", "content": "use a tool"}],
        tools=[
            ToolSpec(
                name="create_multiple_choice",
                description="Render a pick-one list.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "choices": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["question", "choices"],
                },
            )
        ],
    )

    assert captured_payload["tool_choice"] == "auto"
    assert captured_payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "create_multiple_choice",
                "description": "Render a pick-one list.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "choices": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["question", "choices"],
                },
            },
        }
    ]
    assert response.content == [
        {
            "kind": "tool-call",
            "name": "create_multiple_choice",
            "arguments": {
                "question": "Pick one",
                "choices": ["Products", "Inventory"],
            },
            "tool_call_id": "call_123",
        }
    ]


@pytest.mark.asyncio
async def test_openai_compat_ainvoke_sanitizes_tool_names_for_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_payload: dict[str, object] = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_456",
                                        "type": "function",
                                        "function": {
                                            "name": "product_search_products",
                                            "arguments": json.dumps({"query": "t-shirt"}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def _fake_urlopen(req, timeout=0):  # noqa: ANN001
        _ = timeout
        captured_payload.update(json.loads(req.data.decode("utf-8")))
        return _FakeResponse()

    monkeypatch.setattr("kafka_a2a.llms.openai_compat.urlopen", _fake_urlopen)

    model = OpenAICompatChatModel(
        base_url="https://api.openai.com",
        api_key="test-key",
        model="gpt-4.1-mini",
    )
    response = await model.ainvoke(
        [{"role": "user", "content": "use a product tool"}],
        tools=[
            ToolSpec(
                name="product.search_products",
                description="Search products.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            )
        ],
    )

    assert captured_payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "product_search_products",
                "description": "Search products.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]
    assert response.content == [
        {
            "kind": "tool-call",
            "name": "product.search_products",
            "arguments": {"query": "t-shirt"},
            "tool_call_id": "call_456",
        }
    ]
