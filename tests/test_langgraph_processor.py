from __future__ import annotations

import pytest

from tests import fake_langgraph_components
from kafka_a2a.langgraph_processor import (
    _interaction_payload_from_text,
    _is_host_capability_picker_query,
    _is_host_introspection_query,
    _normalize_tool_call_payload,
    _render_tool_prompt_block,
    _select_host_delegation_agent,
    _text_from_parts,
    make_langgraph_chat_processor_from_env,
)
from kafka_a2a.models import Artifact, DataPart, Message, Role, Task, TaskState, TaskStatus, TextPart
from kafka_a2a.tools import ToolSpec


@pytest.fixture(autouse=True)
def _reset_test_state(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_langgraph_components.reset_fake_components()

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

    assert "For greetings or small talk, answer normally in plain text." in prompt
    assert "If the user asks what you can do, what help is available, or wants a list of options to choose from, prefer an interaction tool such as create_multiple_choice." in prompt
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


def test_normalize_tool_call_payload_supports_legacy_tool_code_wrapper() -> None:
    payload = {
        "tool_code": "print(delegate_to_agent(agent_name='users', user_query='How many staff members do we have in total?'))"
    }

    normalized = _normalize_tool_call_payload(payload, tool_names={"delegate_to_agent"})

    assert normalized == {
        "kind": "tool-call",
        "name": "delegate_to_agent",
        "arguments": {
            "agent_name": "users",
            "user_query": "How many staff members do we have in total?",
        },
    }


def test_interaction_payload_from_text_detects_legacy_tool_code_in_json_code_block() -> None:
    text = """
Certainly! Please choose one:

```json
{
  "tool_code": "print(create_multiple_choice(title='Make a selection', description='Pick an area.', options=[{'value':'users','label':'Users'}], multiple=False, allow_input=False))"
}
```
""".strip()

    payload = _interaction_payload_from_text(text)

    assert payload == {
        "interaction_type": "legacy_tool_code",
        "tool_code": "print(create_multiple_choice(title='Make a selection', description='Pick an area.', options=[{'value':'users','label':'Users'}], multiple=False, allow_input=False))",
    }


def test_host_introspection_detection_preserves_domain_requests() -> None:
    assert _is_host_introspection_query("how can you help")
    assert _is_host_introspection_query("how many agents do you have?")
    assert _is_host_introspection_query("hi there!")
    assert not _is_host_introspection_query("help me search for the product t-shirt")


def test_host_capability_picker_detection() -> None:
    assert _is_host_capability_picker_query("what can you do for me?")
    assert _is_host_capability_picker_query("send the list of what you can do so I can choose")
    assert not _is_host_capability_picker_query("help me check stock levels")


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
        {
            "name": "pos",
            "description": "Point of sale specialist.",
            "skills": [{"name": "POS Operations", "description": "Inspect sessions and orders", "tags": ["pos", "session"]}],
        },
    ]

    assert _select_host_delegation_agent("search for a t-shirt product", agents) == "product"
    assert _select_host_delegation_agent("check stock alerts for the warehouse", agents) == "inventory"
    assert _select_host_delegation_agent("show my open cashier session", agents) == "pos"


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

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
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


@pytest.mark.asyncio
async def test_host_capability_query_uses_multiple_choice_tool() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-capability",
        context_id="ctx-capability",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text="what can you do for me? I need to choose from a list")],
            ),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="what can you do for me? I need to choose from a list")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "create_multiple_choice",
            {
                "title": "Choose What You Need Help With",
                "description": "Select the area you want help with. I can continue from your choice.",
                "options": [
                    {"value": "product", "label": "Product Management"},
                    {"value": "inventory", "label": "Inventory Management"},
                    {"value": "pos", "label": "Point of Sale (POS)"},
                    {"value": "users", "label": "User and Workspace Management"},
                    {"value": "general", "label": "General Question"},
                ],
                "multiple": False,
                "allow_input": True,
            },
        )
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_host_capability_selection_routes_to_selected_agent() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Choose What You Need Help With",
        "description": "Select the area you want help with. I can continue from your choice.",
        "options": [
            {"value": "product", "label": "Product Management"},
            {"value": "inventory", "label": "Inventory Management"},
            {"value": "pos", "label": "Point of Sale (POS)"},
            {"value": "users", "label": "User and Workspace Management"},
            {"value": "general", "label": "General Question"},
        ],
        "multiple": False,
        "allow_input": True,
    }
    task = Task(
        id="task-capability-response",
        context_id="ctx-capability-response",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"users","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="hello, what can you do for me")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"users","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "delegate_to_agent",
            {
                "request": "The user selected User and Workspace Management from the host menu. Briefly explain what kinds of tasks you can help with in this domain, using a concise user-facing summary.",
                "agent_name": "users",
            },
        ),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    delegation_payload = delegation_artifact.parts[0].data
    assert delegation_payload["selectedAgent"] == "users"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == (
        "I can help with staff lookup, invitations, roles, groups, permissions, and workspace access."
    )


@pytest.mark.asyncio
async def test_host_direct_staff_query_routes_to_users() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-staff-count",
        context_id="ctx-staff-count",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="how many staff do i have")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="how many staff do i have")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        ("delegate_to_agent", {"request": "how many staff do i have", "agent_name": "users"}),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "users"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == "You have 12 staff members in the current workspace."


@pytest.mark.asyncio
async def test_host_unavailable_selected_agent_reprompts_instead_of_misrouting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_TOOL_EXECUTOR", "tests.fake_langgraph_components:build_fake_tool_executor_without_users")

    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Choose What You Need Help With",
        "description": "Select the area you want help with. I can continue from your choice.",
        "options": [
            {"value": "product", "label": "Product Management"},
            {"value": "inventory", "label": "Inventory Management"},
            {"value": "pos", "label": "Point of Sale (POS)"},
            {"value": "users", "label": "User and Workspace Management"},
            {"value": "general", "label": "General Question"},
        ],
        "multiple": False,
        "allow_input": True,
    }
    task = Task(
        id="task-capability-missing-users",
        context_id="ctx-capability-missing-users",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"users","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="hello, what can you do for me")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"users","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "create_multiple_choice",
            {
                "title": "Choose What You Need Help With",
                "description": (
                    "User and Workspace Management is not currently available. "
                    "Choose one of the areas that is available right now."
                ),
                "options": [
                    {"value": "product", "label": "Product Management"},
                    {"value": "inventory", "label": "Inventory Management"},
                    {"value": "pos", "label": "Point of Sale (POS)"},
                    {"value": "general", "label": "General Question"},
                ],
                "multiple": False,
                "allow_input": True,
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_host_propagates_input_required_from_specialist() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-2",
        context_id="ctx-2",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="ambiguous product stock question")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="ambiguous product stock question")])

    events = [event async for event in processor(task, message, None, None)]

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    delegation_payload = delegation_artifact.parts[0].data
    assert delegation_payload["selectedAgent"] == "product"
    assert delegation_payload["finalState"] == "input-required"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert "interaction_type" in _text_from_parts(result_artifact.parts)


@pytest.mark.asyncio
async def test_specialist_interaction_payload_yields_input_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KA2A_TOOLS_ENABLED", "false")
    monkeypatch.setenv("KA2A_LLM_FACTORY", "tests.fake_langgraph_components:fake_interaction_llm_factory")

    processor = make_langgraph_chat_processor_from_env(agent_name="product")
    task = Task(
        id="task-3",
        context_id="ctx-3",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="help me pick inventory")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="help me pick inventory")])

    events = [event async for event in processor(task, message, None, None)]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert "interaction_type" in _text_from_parts(result_artifact.parts)

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_specialist_tool_loop_passes_tool_specs_to_model() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="product")
    task = Task(
        id="task-4",
        context_id="ctx-4",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="summarize your available tooling")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="summarize your available tooling")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 1
    assert set(fake_langgraph_components.FAKE_LLM_LAST_TOOLS) == {
        "list_available_agents",
        "delegate_to_agent",
        "create_multiple_choice",
    }

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == "This should not be used for delegated host requests."
