from __future__ import annotations

import pytest

from tests.fake_langgraph_components import FAKE_LLM_CALL_COUNT, FAKE_TOOL_CALLS, reset_fake_components
from kafka_a2a.langgraph_processor import (
    _is_host_introspection_query,
    _normalize_tool_call_payload,
    _render_tool_prompt_block,
    _select_host_delegation_agent,
    _text_from_parts,
    make_langgraph_chat_processor_from_env,
)
from kafka_a2a.models import Artifact, Message, Role, Task, TaskState, TaskStatus, TextPart
from kafka_a2a.tools import ToolSpec


@pytest.fixture(autouse=True)
def _reset_test_state(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_fake_components()

    monkeypatch.setenv("KA2A_LLM_CREDENTIALS_SOURCE", "env")
    monkeypatch.setenv("KA2A_LLM_PROVIDER", "openai_compat")
    monkeypatch.setenv("KA2A_LLM_API_KEY", "test-key")
    monkeypatch.setenv("KA2A_LLM_BASE_URL", "https://example.com")
    monkeypatch.setenv("KA2A_LLM_FACTORY", "tests.fake_langgraph_components:fake_llm_factory")
    monkeypatch.setenv("KA2A_TOOLS_ENABLED", "true")
    monkeypatch.setenv("KA2A_TOOLS_SOURCE", "custom")
    monkeypatch.setenv("KA2A_TOOL_EXECUTOR", "tests.fake_langgraph_components:build_fake_tool_executor")
    monkeypatch.setenv("KA2A_CONTEXT_MEMORY_STORE", "off")


def test_render_tool_prompt_block_discourages_tool_calls_for_plain_conversation() -> None:
    prompt = _render_tool_prompt_block(
        [
            ToolSpec(
                name="list_available_agents",
                description="List downstream specialist agents.",
                input_schema={"type": "object", "properties": {}, "required": []},
            )
        ]
    )

    assert "For greetings, small talk, capability questions, or simple summaries, answer normally in plain text." in prompt
    assert 'Never output bare tool names or pseudo-tool JSON such as {"kind":"list_available_agents"}' in prompt


def test_normalize_tool_call_payload_promotes_bare_kind_tool_call() -> None:
    payload = {"kind": "list_available_agents"}

    normalized = _normalize_tool_call_payload(payload, tool_names={"list_available_agents"})

    assert normalized == {
        "kind": "tool-call",
        "name": "list_available_agents",
        "arguments": {},
    }


def test_normalize_tool_call_payload_promotes_wrong_kind_with_arguments() -> None:
    payload = {
        "kind": "create_dynamic_form",
        "name": "create_dynamic_form",
        "arguments": {"title": "Inventory Management"},
    }

    normalized = _normalize_tool_call_payload(payload, tool_names={"create_dynamic_form"})

    assert normalized == {
        "kind": "tool-call",
        "name": "create_dynamic_form",
        "arguments": {"title": "Inventory Management"},
    }


def test_normalize_tool_call_payload_leaves_interaction_payload_untouched() -> None:
    payload = {
        "interaction_type": "dynamic_form",
        "title": "Inventory Management",
        "fields": [{"name": "quantity", "type": "number"}],
    }

    normalized = _normalize_tool_call_payload(payload, tool_names={"create_dynamic_form"})

    assert normalized == payload


def test_host_introspection_detection_preserves_domain_requests() -> None:
    assert _is_host_introspection_query("how can you help")
    assert _is_host_introspection_query("how many agents do you have?")
    assert not _is_host_introspection_query("help me search for the product t-shirt")


def test_select_host_delegation_agent_prefers_best_matching_specialist() -> None:
    agents = [
        {
            "name": "product",
            "description": "Product catalog specialist.",
            "skills": [{"name": "Product Search", "description": "Search products", "tags": ["product"]}],
        },
        {
            "name": "inventory",
            "description": "Inventory stock specialist.",
            "skills": [{"name": "Inventory Lookup", "description": "Check stock", "tags": ["inventory"]}],
        },
    ]

    assert _select_host_delegation_agent("search for a t-shirt product", agents) == "product"


@pytest.mark.asyncio
async def test_host_auto_delegates_and_waits_for_specialist_result() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="help me search for the product t-shirt")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="help me search for the product t-shirt")])

    events = [event async for event in processor(task, message, None, None)]

    assert FAKE_LLM_CALL_COUNT == 0
    assert FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        ("delegate_to_agent", {"request": "help me search for the product t-shirt", "agent_name": "product"}),
    ]

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert len(status_events) == 4
    assert status_events[0].state == TaskState.working
    assert _text_from_parts(status_events[0].message.parts) == "Delegating this request to the product specialist agent."
    assert _text_from_parts(status_events[1].message.parts) == "product agent: delegated task submitted"
    assert _text_from_parts(status_events[2].message.parts) == "product agent: searching catalog"
    assert status_events[-1].state == TaskState.completed
    assert _text_from_parts(status_events[-1].message.parts) == "Found 3 products matching t-shirt."

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    delegation_payload = delegation_artifact.parts[0].data
    assert delegation_payload["selectedAgent"] == "product"
    assert delegation_payload["delegatedTaskId"] == "delegated-1"
    assert delegation_payload["finalState"] == "completed"
    assert len(delegation_payload["statusUpdates"]) == 3

    child_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "product.matches")
    assert child_artifact.parts[0].data["count"] == 3

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == "Found 3 products matching t-shirt."
