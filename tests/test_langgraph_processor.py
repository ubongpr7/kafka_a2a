from __future__ import annotations

import pytest

from tests import fake_langgraph_components
from kafka_a2a.langgraph_processor import (
    _build_inventory_operation,
    _build_product_operation,
    _classify_failed_operation,
    _interaction_payload_from_text,
    _is_host_capability_picker_query,
    _is_host_introspection_query,
    _onboarding_operation_summary,
    _normalize_tool_call_payload,
    _render_tool_prompt_block,
    _select_host_delegation_agent,
    _select_router_delegation_agent,
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


def test_render_tool_prompt_block_includes_relation_lookup_rules() -> None:
    prompt = _render_tool_prompt_block(
        [
            ToolSpec(
                name="inventory.list_inventory_categories",
                description="List categories.",
                input_schema={"type": "object", "properties": {}, "required": []},
            ),
            ToolSpec(
                name="inventory.create_inventory",
                description="Create inventory.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "payload": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "category_id": {
                                    "type": "string",
                                    "description": "UUID of InventoryCategory",
                                },
                            },
                            "required": ["name"],
                        }
                    },
                    "required": ["payload"],
                },
            ),
        ]
    )

    assert "Never ask the user to manually type backend IDs or UUIDs for relational fields." in prompt
    assert "prefer list/get-all tools over search tools whenever both are available." in prompt
    assert "Do not tell the user the backend requires those parameters." in prompt
    assert "omit optional filters/null values" in prompt
    assert "`inventory.create_inventory.payload.category_id`" in prompt
    assert "`inventory.list_inventory_categories`" in prompt


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


def test_build_product_operation_supports_nested_payload_schema() -> None:
    operation = _build_product_operation(
        tool_specs=[
            ToolSpec(
                name="product.create_product",
                description="Create a product.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "payload": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "category_name": {"type": "string"},
                                "pos_ready": {"type": "boolean"},
                            },
                            "required": ["name"],
                        }
                    },
                    "required": ["payload"],
                },
            )
        ],
        company_context=None,
        product_name="Women's Cotton T-Shirt",
        product_category_id=None,
        product_category="Women's Wear",
        pos_ready=True,
    )

    assert operation["arguments"] == {
        "payload": {
            "name": "Women's Cotton T-Shirt",
            "category_name": "Women's Wear",
            "pos_ready": True,
        }
    }
    assert operation["missing_required"] == []


def test_build_inventory_operation_prefers_relation_ids_in_nested_payload_schema() -> None:
    operation = _build_inventory_operation(
        tool_specs=[
            ToolSpec(
                name="inventory.create_inventory",
                description="Create inventory.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "payload": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "stock_location_id": {"type": "string"},
                                "category_id": {"type": "string"},
                            },
                            "required": ["name"],
                        }
                    },
                    "required": ["payload"],
                },
            )
        ],
        company_context=None,
        inventory_name="Main Inventory",
        inventory_description="Primary sellable stock ledger",
        related_location_id="loc-1",
        related_location_name="Main Warehouse",
        category_id="cat-1",
        category_name="Men's Clothes",
    )

    assert operation["arguments"] == {
        "payload": {
            "name": "Main Inventory",
            "description": "Primary sellable stock ledger",
            "stock_location_id": "loc-1",
            "category_id": "cat-1",
        }
    }
    assert operation["missing_required"] == []


def test_select_router_delegation_agent_prefers_best_matching_subspecialist() -> None:
    selected = _select_router_delegation_agent(
        "i want you to create inventory for me",
        [
            {
                "name": "inventory_visibility",
                "description": "Focused inventory specialist for stock posture, alerts, reservations, and warehouse visibility.",
                "skills": [
                    {
                        "name": "Inventory Visibility",
                        "description": "Search inventories and inspect stock posture.",
                        "tags": ["inventory", "stock", "warehouse"],
                        "examples": ["Show low-stock inventories."],
                    }
                ],
            },
            {
                "name": "inventory_setup",
                "description": "Focused inventory specialist for stock-location, inventory-category, and inventory-ledger setup and maintenance workflows.",
                "skills": [
                    {
                        "name": "Inventory Setup Admin",
                        "description": "Create and update stock locations, inventory categories, and inventory ledgers.",
                        "tags": ["inventory", "setup", "create", "categories"],
                        "examples": ["Create the main inventory ledger for onboarding."],
                    }
                ],
            },
        ],
    )

    assert selected == "inventory_setup"


def test_classify_failed_operation_reports_tls_discovery_failure() -> None:
    classified = _classify_failed_operation(
        {
            "label": "stock location 'Main Warehouse'",
            "tool_name": "inventory.create_stock_location",
            "reason": "tool_unavailable",
            "discovery_failures": [
                {
                    "server_id": "inventory",
                    "error": "httpx.ConnectError: [SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error (_ssl.c:1032)",
                }
            ],
        }
    )

    assert classified["error_kind"] == "tls"
    assert classified["retryable"] is True
    assert classified["error_summary"] == "inventory: TLS handshake failed while connecting to the upstream service."


def test_onboarding_operation_summary_prioritizes_blocking_issues() -> None:
    summary = _onboarding_operation_summary(
        created_operations={},
        failed_operations=[
            {
                "label": "stock location 'Main Warehouse'",
                "tool_name": "inventory.create_stock_location",
                "reason": "tool_unavailable",
                "discovery_failures": [
                    {
                        "server_id": "inventory",
                        "error": "httpx.ConnectError: [SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error (_ssl.c:1032)",
                    }
                ],
            },
            {
                "label": "inventory category 'Beverages'",
                "tool_name": "inventory.create_inventory_category",
                "reason": "tool_unavailable",
                "discovery_failures": [
                    {
                        "server_id": "inventory",
                        "error": "httpx.ConnectError: [SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error (_ssl.c:1032)",
                    }
                ],
            },
            {
                "label": "product 'Soda'",
                "tool_name": "product.create_product",
                "reason": "missing_required_arguments",
                "missing": ["payload"],
            },
            {
                "label": "delegated onboarding submission",
                "reason": "tool_error",
                "error": "Requested agent 'inventory' is not registered.",
            },
        ],
    )

    assert "Blocking issues: inventory: TLS handshake failed while connecting to the upstream service." in summary
    assert "The tool schema requires additional fields: payload." in summary
    assert "The required specialist agent is not currently visible in the registry." in summary
    assert "Still pending: 4 onboarding steps are blocked." in summary


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
    assert _is_host_introspection_query("what can u do for me")
    assert _is_host_introspection_query("tell me the agents that you have currently that are register")
    assert not _is_host_introspection_query("help me search for the product t-shirt")


def test_host_capability_picker_detection() -> None:
    assert _is_host_capability_picker_query("what can you do for me?")
    assert _is_host_capability_picker_query("what can u do for me")
    assert _is_host_capability_picker_query("send the list of what you can do so I can choose")
    assert not _is_host_capability_picker_query("help me check stock levels")


def test_select_host_delegation_agent_prefers_best_matching_specialist() -> None:
    agents = [
        {
            "name": "onboarding",
            "description": "Onboarding workflow specialist.",
            "skills": [{"name": "Inventory Onboarding", "description": "Guide setup", "tags": ["onboarding", "setup"]}],
        },
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

    assert _select_host_delegation_agent("help me set up my inventory workspace from scratch", agents) == "onboarding"
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
                    {"value": "onboarding", "label": "Inventory Onboarding"},
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
            {"value": "onboarding", "label": "Inventory Onboarding"},
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
async def test_host_capability_selection_routes_onboarding_to_guided_flow_request() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Choose What You Need Help With",
        "description": "Select the area you want help with. I can continue from your choice.",
        "options": [
            {"value": "onboarding", "label": "Inventory Onboarding"},
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
        id="task-capability-onboarding",
        context_id="ctx-capability-onboarding",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"onboarding","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="hello, what can you do for me")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"onboarding","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "delegate_to_agent",
            {
                "request": (
                    "Start a guided inventory onboarding flow. Ask the user what setup they want to complete first, "
                    "then collect the required details step by step using structured interactions."
                ),
                "agent_name": "onboarding",
            },
        ),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "onboarding"
    assert delegation_artifact.parts[0].data["finalState"] == "input-required"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"
    assert result_artifact.parts[0].data["workflow_stage"] == "scope_picker"
    assert result_artifact.parts[0].data["delegated_agent"] == "onboarding"
    assert result_artifact.parts[0].data["delegated_task_id"] == "delegated-onboarding-scope"


@pytest.mark.asyncio
async def test_host_capability_selection_inventory_opens_domain_area_picker() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Choose What You Need Help With",
        "description": "Select the area you want help with. I can continue from your choice.",
        "options": [
            {"value": "onboarding", "label": "Inventory Onboarding"},
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
        id="task-capability-inventory-menu",
        context_id="ctx-capability-inventory-menu",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="hello, what can you do for me")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "create_multiple_choice",
            {
                "title": "Inventory Management",
                "description": (
                    "Choose the inventory area you want help with. You can also type a specific inventory question."
                ),
                "options": [
                    {"value": "inventory_setup", "label": "Set Up Inventory"},
                    {"value": "inventory_visibility", "label": "Stock and Warehouse Visibility"},
                    {"value": "inventory_procurement", "label": "Purchase Orders and Receiving"},
                    {"value": "inventory_fulfillment", "label": "Transfers, Adjustments, and Fulfillment"},
                ],
                "multiple": False,
                "allow_input": True,
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["interaction_type"] == "multiple_choice"
    assert payload["workflow"] == "host_domain_area_picker"
    assert payload["workflow_stage"] == "area_picker"
    assert payload["domain_agent"] == "inventory"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_host_inventory_domain_picker_selection_delegates_to_inventory_router() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    domain_picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Inventory Management",
        "description": "Choose the inventory area you want help with. You can also type a specific inventory question.",
        "options": [
            {"value": "inventory_setup", "label": "Set Up Inventory"},
            {"value": "inventory_visibility", "label": "Stock and Warehouse Visibility"},
            {"value": "inventory_procurement", "label": "Purchase Orders and Receiving"},
            {"value": "inventory_fulfillment", "label": "Transfers, Adjustments, and Fulfillment"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "host_domain_area_picker",
        "workflow_stage": "area_picker",
        "domain_agent": "inventory",
    }
    task = Task(
        id="task-capability-inventory-route",
        context_id="ctx-capability-inventory-route",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[
                    TextPart(
                        text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}'
                    )
                ],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="hello, what can you do for me")]),
            Message(role=Role.agent, parts=[DataPart(data=domain_picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "delegate_to_agent",
            {
                "request": (
                    "The user selected Set Up Inventory from the Inventory Management menu. "
                    "Help them create or configure stock locations, inventory categories, or inventory ledgers. "
                    "Start with a short structured choice or the next required setup step. "
                    "Never ask for raw internal ids when lookups or selections can be used instead."
                ),
                "agent_name": "inventory",
            },
        ),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "inventory"


@pytest.mark.asyncio
async def test_host_inventory_domain_picker_free_text_stays_in_inventory_domain() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    domain_picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Inventory Management",
        "description": "Choose the inventory area you want help with. You can also type a specific inventory question.",
        "options": [
            {"value": "inventory_setup", "label": "Set Up Inventory"},
            {"value": "inventory_visibility", "label": "Stock and Warehouse Visibility"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "host_domain_area_picker",
        "workflow_stage": "area_picker",
        "domain_agent": "inventory",
    }
    task = Task(
        id="task-capability-inventory-free-text",
        context_id="ctx-capability-inventory-free-text",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="show low stock")]),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="hello, what can you do for me")]),
            Message(role=Role.agent, parts=[DataPart(data=domain_picker_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text="show low stock")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "delegate_to_agent",
            {
                "request": "show low stock",
                "agent_name": "inventory",
            },
        ),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "inventory"


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
async def test_host_direct_setup_query_routes_to_onboarding() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-onboarding",
        context_id="ctx-onboarding",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="help me set up my inventory workspace from scratch")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="help me set up my inventory workspace from scratch")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "delegate_to_agent",
            {"request": "help me set up my inventory workspace from scratch", "agent_name": "onboarding"},
        ),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "onboarding"
    assert delegation_artifact.parts[0].data["finalState"] == "input-required"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"
    assert result_artifact.parts[0].data["workflow_stage"] == "scope_picker"
    assert result_artifact.parts[0].data["delegated_agent"] == "onboarding"
    assert result_artifact.parts[0].data["delegated_task_id"] == "delegated-onboarding-scope"


@pytest.mark.asyncio
async def test_host_continues_delegated_onboarding_interaction_with_same_task() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")

    first_task = Task(
        id="task-onboarding-follow-up-start",
        context_id="ctx-onboarding-follow-up",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="help me set up my inventory workspace from scratch")]),
        ),
    )
    first_message = Message(role=Role.user, parts=[TextPart(text="help me set up my inventory workspace from scratch")])

    first_events = [event async for event in processor(first_task, first_message, None, None)]
    first_payload = next(
        event.parts[0].data for event in first_events if isinstance(event, Artifact) and event.name == "result"
    )

    fake_langgraph_components.reset_fake_components()

    second_task = Task(
        id="task-onboarding-follow-up-continue",
        context_id="ctx-onboarding-follow-up",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"full_setup","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me set up my inventory workspace from scratch")]),
            Message(role=Role.agent, parts=[DataPart(data=first_payload)]),
        ],
    )
    second_message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"full_setup","additional_input":null}')],
    )

    second_events = [event async for event in processor(second_task, second_message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "delegate_to_agent",
            {
                "request": '{"type":"multiple_choice_response","selected":"full_setup","additional_input":null}',
                "agent_name": "onboarding",
                "delegated_task_id": "delegated-onboarding-scope",
            },
        ),
    ]

    delegation_artifact = next(event for event in second_events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "onboarding"
    assert delegation_artifact.parts[0].data["delegatedTaskId"] == "delegated-onboarding-wizard"
    assert delegation_artifact.parts[0].data["finalState"] == "input-required"

    result_artifact = next(event for event in second_events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "wizard_flow"
    assert result_artifact.parts[0].data["workflow_stage"] == "wizard"
    assert result_artifact.parts[0].data["delegated_agent"] == "onboarding"
    assert result_artifact.parts[0].data["delegated_task_id"] == "delegated-onboarding-wizard"

    status_events = [event for event in second_events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_host_registered_agents_query_stays_with_host() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-host-agents-list",
        context_id="ctx-host-agents-list",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text="tell me the agents that you have currently that are register")],
            ),
        ),
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text="tell me the agents that you have currently that are register")],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == (
        "Currently registered specialist agents: Inventory Management, Inventory Onboarding, "
        "Point of Sale (POS), Product Management, User and Workspace Management."
    )


@pytest.mark.asyncio
async def test_host_registered_agents_query_reports_hidden_agents_when_not_host_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KA2A_TOOL_EXECUTOR",
        "tests.fake_langgraph_components:build_fake_tool_executor_without_users_or_onboarding",
    )

    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-host-agents-list-hidden",
        context_id="ctx-host-agents-list-hidden",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text="tell me the agents that you have currently that are register")],
            ),
        ),
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text="tell me the agents that you have currently that are register")],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == (
        "Currently registered specialist agents: Inventory Management, Inventory Onboarding, "
        "Point of Sale (POS), Product Management, User and Workspace Management. "
        "The host is currently configured to route to: Inventory Management, Point of Sale (POS), Product Management."
    )


@pytest.mark.asyncio
async def test_onboarding_agent_starts_with_scope_picker() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    task = Task(
        id="task-onboarding-start",
        context_id="ctx-onboarding-start",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "users.get_active_company_profile",
        "create_multiple_choice",
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"
    assert result_artifact.parts[0].data["workflow_stage"] == "scope_picker"
    assert result_artifact.parts[0].data["description"].startswith("Current company: Intera Demo Company")

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_onboarding_agent_scope_selection_opens_wizard() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Start Inventory Onboarding",
        "description": "Choose the setup area you want to complete first. I will guide you step by step.",
        "options": [
            {"value": "full_setup", "label": "Full Inventory Setup"},
            {"value": "stock_locations", "label": "Stock Locations"},
            {"value": "inventory_categories", "label": "Inventory Categories"},
            {"value": "inventory_setup", "label": "Inventory Setup"},
            {"value": "product_onboarding", "label": "Product Onboarding"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "inventory_onboarding",
        "workflow_stage": "scope_picker",
    }
    task = Task(
        id="task-onboarding-scope",
        context_id="ctx-onboarding-scope",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"full_setup","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"full_setup","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "create_wizard_flow",
        "users.get_active_company_profile",
    ]
    assert fake_langgraph_components.FAKE_TOOL_CALLS[0][1]["title"] == "Full Inventory Setup Wizard"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "wizard_flow"
    assert result_artifact.parts[0].data["workflow_stage"] == "wizard"
    assert result_artifact.parts[0].data["onboarding_scope"] == "full_setup"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_onboarding_agent_inventory_setup_scope_populates_relation_selects() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Start Inventory Onboarding",
        "description": "Choose the setup area you want to complete first. I will guide you step by step.",
        "options": [
            {"value": "full_setup", "label": "Full Inventory Setup"},
            {"value": "stock_locations", "label": "Stock Locations"},
            {"value": "inventory_categories", "label": "Inventory Categories"},
            {"value": "inventory_setup", "label": "Inventory Setup"},
            {"value": "product_onboarding", "label": "Product Onboarding"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "inventory_onboarding",
        "workflow_stage": "scope_picker",
    }
    task = Task(
        id="task-onboarding-scope-relations",
        context_id="ctx-onboarding-scope-relations",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me set up inventory")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "create_wizard_flow",
        "inventory.search_stock_locations",
        "inventory.list_inventory_categories",
        "users.get_active_company_profile",
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["interaction_type"] == "wizard_flow"
    assert payload["workflow_stage"] == "wizard"
    assert payload["onboarding_scope"] == "inventory_setup"

    fields = {field["name"]: field for field in payload["steps"][0]["fields"]}
    assert fields["default_inventory_name"]["type"] == "text"
    assert fields["related_stock_location_id"]["type"] == "select"
    assert fields["related_stock_location_id"]["options"][0]["label"] == "Main Warehouse"
    assert fields["related_stock_location_id"]["options"][0]["value"] == "loc-1"
    assert fields["inventory_category_id"]["type"] == "select"
    assert fields["inventory_category_id"]["options"][0]["label"] == "Men's Clothes"
    assert fields["inventory_category_id"]["options"][0]["value"] == "cat-1"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_onboarding_agent_inventory_setup_scope_populates_relation_selects_from_wrapped_lookup_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KA2A_TOOL_EXECUTOR",
        "tests.fake_langgraph_components:build_fake_wrapped_lookup_tool_executor",
    )

    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Start Inventory Onboarding",
        "description": "Choose the setup area you want to complete first. I will guide you step by step.",
        "options": [
            {"value": "full_setup", "label": "Full Inventory Setup"},
            {"value": "stock_locations", "label": "Stock Locations"},
            {"value": "inventory_categories", "label": "Inventory Categories"},
            {"value": "inventory_setup", "label": "Inventory Setup"},
            {"value": "product_onboarding", "label": "Product Onboarding"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "inventory_onboarding",
        "workflow_stage": "scope_picker",
    }
    task = Task(
        id="task-onboarding-scope-relations-wrapped",
        context_id="ctx-onboarding-scope-relations-wrapped",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me set up inventory")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    fields = {field["name"]: field for field in payload["steps"][0]["fields"]}

    assert fields["default_inventory_name"]["type"] == "text"
    assert fields["related_stock_location_id"]["type"] == "select"
    assert fields["related_stock_location_id"]["options"][0]["label"] == "Main Warehouse"
    assert fields["related_stock_location_id"]["options"][0]["value"] == "loc-1"
    assert fields["inventory_category_id"]["type"] == "select"
    assert fields["inventory_category_id"]["options"][0]["label"] == "Men's Clothes"
    assert fields["inventory_category_id"]["options"][0]["value"] == "cat-1"


@pytest.mark.asyncio
async def test_onboarding_agent_inventory_setup_scope_populates_category_options_from_text_wrapped_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KA2A_TOOL_EXECUTOR",
        "tests.fake_langgraph_components:build_fake_category_text_wrapped_tool_executor",
    )

    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Start Inventory Onboarding",
        "description": "Choose the setup area you want to complete first. I will guide you step by step.",
        "options": [
            {"value": "full_setup", "label": "Full Inventory Setup"},
            {"value": "stock_locations", "label": "Stock Locations"},
            {"value": "inventory_categories", "label": "Inventory Categories"},
            {"value": "inventory_setup", "label": "Inventory Setup"},
            {"value": "product_onboarding", "label": "Product Onboarding"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "inventory_onboarding",
        "workflow_stage": "scope_picker",
    }
    task = Task(
        id="task-onboarding-scope-relations-category-text-wrapped",
        context_id="ctx-onboarding-scope-relations-category-text-wrapped",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me set up inventory")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    fields = {field["name"]: field for field in payload["steps"][0]["fields"]}

    assert fields["related_stock_location_id"]["type"] == "select"
    assert fields["related_stock_location_id"]["options"][0]["label"] == "Main Warehouse"
    assert fields["inventory_category_id"]["type"] == "select"
    assert fields["inventory_category_id"]["options"][0]["label"] == "Men's Clothes"
    assert fields["inventory_category_id"]["options"][0]["value"] == "cat-1"


@pytest.mark.asyncio
async def test_onboarding_agent_wizard_completion_prompts_for_review() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    wizard_payload = {
        "interaction_type": "wizard_flow",
        "title": "Full Inventory Setup Wizard",
        "description": "Fill in the setup details and I will prepare the onboarding action plan.",
        "steps": [],
        "allow_back": True,
        "show_progress": True,
        "workflow": "inventory_onboarding",
        "workflow_stage": "wizard",
        "onboarding_scope": "full_setup",
    }
    response_text = (
        '{"type":"wizard_flow_response","completed":true,"all_responses":'
        '{"step_0":{"primary_location_name":"Main Warehouse","primary_location_type":"warehouse","additional_locations":"Front Store"},'
        '"step_1":{"category_names":"Beverages\\nSnacks\\nCleaning Supplies"},'
        '"step_2":{"default_inventory_name":"Main Inventory","inventory_description":"Primary sellable stock ledger"},'
        '"step_3":{"continue_to_product_onboarding":true,"initial_product_names":"Coca-Cola 50cl\\nFanta 50cl"}}}'
    )
    task = Task(
        id="task-onboarding-review",
        context_id="ctx-onboarding-review",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=response_text)]),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")]),
            Message(role=Role.agent, parts=[DataPart(data=wizard_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=response_text)])

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "users.get_active_company_profile",
        "create_multiple_choice",
    ]
    assert fake_langgraph_components.FAKE_TOOL_CALLS[1][1]["title"] == "Review Onboarding Plan"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"
    assert result_artifact.parts[0].data["workflow_stage"] == "review"
    assert result_artifact.parts[0].data["onboarding_data"]["flat"]["default_inventory_name"] == "Main Inventory"
    assert "Main Warehouse" in result_artifact.parts[0].data["onboarding_summary"]
    assert result_artifact.parts[0].data["onboarding_data"]["company_context"]["name"] == "Intera Demo Company"


@pytest.mark.asyncio
async def test_onboarding_agent_review_uses_relation_labels_from_wizard_selection() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    wizard_payload = {
        "interaction_type": "wizard_flow",
        "title": "Inventory Setup Wizard",
        "description": "Fill in the setup details and I will prepare the onboarding action plan.",
        "steps": [
            {
                "id": "inventory",
                "title": "Inventory Setup",
                "description": "Define the first inventory ledger you want to create.",
                "fields": [
                    {
                        "name": "default_inventory_name",
                        "type": "text",
                        "label": "Inventory Name",
                        "required": True,
                    },
                    {
                        "name": "related_stock_location_id",
                        "type": "select",
                        "label": "Primary Location for This Inventory",
                        "required": False,
                        "options": [
                            {"value": "loc-1", "label": "Main Warehouse"},
                            {"value": "loc-2", "label": "Front Store"},
                        ],
                    },
                    {
                        "name": "inventory_category_id",
                        "type": "select",
                        "label": "Default Category",
                        "required": False,
                        "options": [
                            {"value": "cat-1", "label": "Men's Clothes"},
                            {"value": "cat-2", "label": "Shoes"},
                        ],
                    },
                ],
            }
        ],
        "allow_back": True,
        "show_progress": True,
        "workflow": "inventory_onboarding",
        "workflow_stage": "wizard",
        "onboarding_scope": "inventory_setup",
    }
    response_text = (
        '{"type":"wizard_flow_response","completed":true,"all_responses":'
        '{"step_0":{"default_inventory_name":"Main Inventory","related_stock_location_id":"loc-1","inventory_category_id":"cat-2"}}}'
    )
    task = Task(
        id="task-onboarding-review-relations",
        context_id="ctx-onboarding-review-relations",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=response_text)]),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me set up inventory")]),
            Message(role=Role.agent, parts=[DataPart(data=wizard_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=response_text)])

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "users.get_active_company_profile",
        "create_multiple_choice",
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["interaction_type"] == "multiple_choice"
    assert payload["workflow_stage"] == "review"
    assert payload["onboarding_data"]["flat"]["related_stock_location_id"] == "loc-1"
    assert payload["onboarding_data"]["flat"]["related_stock_location_label"] == "Main Warehouse"
    assert payload["onboarding_data"]["flat"]["inventory_category_id"] == "cat-2"
    assert payload["onboarding_data"]["flat"]["inventory_category_label"] == "Shoes"
    assert "Ledger location: Main Warehouse" in payload["onboarding_summary"]
    assert "Default category: Shoes" in payload["onboarding_summary"]
    assert "loc-1" not in payload["onboarding_summary"]
    assert "cat-2" not in payload["onboarding_summary"]


@pytest.mark.asyncio
async def test_onboarding_agent_review_confirmation_creates_inventory_setup_directly() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    review_payload = {
        "interaction_type": "multiple_choice",
        "title": "Review Onboarding Plan",
        "description": "Review your onboarding plan.",
        "options": [
            {"value": "create_now", "label": "Create This Setup"},
            {"value": "revise_answers", "label": "Revise My Answers"},
            {"value": "cancel_onboarding", "label": "Cancel For Now"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "inventory_onboarding",
        "workflow_stage": "review",
        "onboarding_scope": "full_setup",
        "onboarding_data": {
            "scope": "full_setup",
            "steps": {},
            "flat": {
                "primary_location_name": "Main Warehouse",
                "primary_location_type": "warehouse",
                "category_names": "Beverages\nSnacks\nCleaning Supplies",
                "default_inventory_name": "Main Inventory",
                "continue_to_product_onboarding": True,
            },
            "raw_response": {},
        },
        "onboarding_summary": "Scope: Full Inventory Setup",
    }
    task = Task(
        id="task-onboarding-confirm",
        context_id="ctx-onboarding-confirm",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"create_now","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")]),
            Message(role=Role.agent, parts=[DataPart(data=review_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"create_now","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "users.get_active_company_profile",
        "inventory.create_stock_location",
        "inventory.create_inventory_category",
        "inventory.create_inventory_category",
        "inventory.create_inventory_category",
        "inventory.create_inventory",
    ]
    assert not any(isinstance(event, Artifact) and event.name == "delegation" for event in events)

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == (
        "Created 1 stock location, 3 inventory categories, and 1 inventory ledger for onboarding."
    )
    created_artifact = next(
        event for event in events if isinstance(event, Artifact) and event.name == "onboarding.created_operations"
    )
    assert len(created_artifact.parts[0].data["operations"]) == 5


@pytest.mark.asyncio
async def test_onboarding_agent_resume_prompt_appears_for_saved_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_CONTEXT_MEMORY_STORE", "memory")

    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    first_task = Task(
        id="task-onboarding-save",
        context_id="ctx-onboarding-save",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")]),
        ),
    )
    first_message = Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")])

    _ = [event async for event in processor(first_task, first_message, None, None)]

    fake_langgraph_components.reset_fake_components()

    second_task = Task(
        id="task-onboarding-resume",
        context_id="ctx-onboarding-save",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="continue onboarding")]),
        ),
    )
    second_message = Message(role=Role.user, parts=[TextPart(text="continue onboarding")])

    events = [event async for event in processor(second_task, second_message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "create_multiple_choice",
            {
                "title": "Resume Inventory Onboarding",
                "description": (
                    "You have an unfinished Full Inventory Setup workflow.\n\n"
                    "Choose whether to resume it or start a new onboarding flow."
                ),
                "options": [
                    {"value": "resume_saved", "label": "Resume Saved Onboarding"},
                    {"value": "start_over", "label": "Start Over"},
                    {"value": "cancel_saved", "label": "Cancel Saved Onboarding"},
                ],
                "multiple": False,
                "allow_input": True,
            },
        )
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["workflow_stage"] == "resume_prompt"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_onboarding_agent_partial_failures_prompt_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KA2A_TOOL_EXECUTOR",
        "tests.fake_langgraph_components:build_fake_tool_executor_with_category_failures",
    )

    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    review_payload = {
        "interaction_type": "multiple_choice",
        "title": "Review Onboarding Plan",
        "description": "Review your onboarding plan.",
        "options": [
            {"value": "create_now", "label": "Create This Setup"},
            {"value": "revise_answers", "label": "Revise My Answers"},
            {"value": "cancel_onboarding", "label": "Cancel For Now"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "inventory_onboarding",
        "workflow_stage": "review",
        "onboarding_scope": "full_setup",
        "onboarding_data": {
            "scope": "full_setup",
            "steps": {},
            "flat": {
                "primary_location_name": "Main Warehouse",
                "primary_location_type": "warehouse",
                "category_names": "Beverages\nSnacks\nCleaning Supplies",
                "default_inventory_name": "Main Inventory",
            },
            "raw_response": {},
        },
        "onboarding_summary": "Scope: Full Inventory Setup",
    }
    task = Task(
        id="task-onboarding-retry",
        context_id="ctx-onboarding-retry",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"create_now","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")]),
            Message(role=Role.agent, parts=[DataPart(data=review_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"create_now","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "users.get_active_company_profile",
        "inventory.create_stock_location",
        "inventory.create_inventory_category",
        "inventory.create_inventory_category",
        "inventory.create_inventory_category",
        "inventory.create_inventory",
        "create_multiple_choice",
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"
    assert result_artifact.parts[0].data["workflow_stage"] == "retry"
    assert len(result_artifact.parts[0].data["created_operations"]) == 2
    assert len(result_artifact.parts[0].data["failed_operations"]) == 3

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


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
            {"value": "onboarding", "label": "Inventory Onboarding"},
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
                    {"value": "onboarding", "label": "Inventory Onboarding"},
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
async def test_host_free_text_onboarding_reply_after_picker_reprompts_when_onboarding_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KA2A_TOOL_EXECUTOR",
        "tests.fake_langgraph_components:build_fake_tool_executor_without_users_or_onboarding",
    )

    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Choose What You Need Help With",
        "description": "Select the area you want help with. I can continue from your choice.",
        "options": [
            {"value": "product", "label": "Product Management"},
            {"value": "inventory", "label": "Inventory Management"},
            {"value": "pos", "label": "Point of Sale (POS)"},
            {"value": "general", "label": "General Question"},
        ],
        "multiple": False,
        "allow_input": True,
    }
    task = Task(
        id="task-capability-free-text-onboarding",
        context_id="ctx-capability-free-text-onboarding",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="i want to do inventory onoarding")]),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="what can you do?")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text="i want to do inventory onoarding")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "create_multiple_choice",
            {
                "title": "Choose What You Need Help With",
                "description": (
                    "Inventory Onboarding is not currently available. "
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
async def test_host_answers_unavailable_agent_diagnostics_without_misrouting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KA2A_TOOL_EXECUTOR",
        "tests.fake_langgraph_components:build_fake_tool_executor_without_users_or_onboarding",
    )

    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-capability-unavailable-diagnostics",
        context_id="ctx-capability-unavailable-diagnostics",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text="why is the onboarding agent not active and can I see the error message")],
            ),
        ),
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text="why is the onboarding agent not active and can I see the error message")],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == (
        "Inventory Onboarding is registered in the current agent directory, but it is not currently exposed to the host "
        "for routing. The host currently routes to these available areas: Inventory Management, Point of Sale (POS), "
        "Product Management. "
        "There is no specialist error message to show here because the host did not delegate this request. "
        "This looks like a host or gateway configuration issue, such as the downstream allowlist, not a downstream "
        "task failure."
    )

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.completed


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
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"
    assert result_artifact.parts[0].data["delegated_agent"] == "product"
    assert result_artifact.parts[0].data["delegated_task_id"] == "delegated-2"


@pytest.mark.asyncio
async def test_inventory_router_auto_delegates_to_inventory_setup_subspecialist() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory")
    task = Task(
        id="task-router-inventory-create",
        context_id="ctx-router-inventory-create",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="I want you to create an inventory for me")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="I want you to create an inventory for me")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "delegate_to_agent",
            {
                "request": "I want you to create an inventory for me",
                "agent_name": "inventory_setup",
            },
        ),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "inventory_setup"


@pytest.mark.asyncio
async def test_inventory_setup_rewrites_relation_text_fields_to_select_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_LLM_FACTORY", "tests.fake_langgraph_components:fake_relation_interaction_llm_factory")

    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_setup")
    task = Task(
        id="task-relation-form",
        context_id="ctx-relation-form",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="help me set up an inventory")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="help me set up an inventory")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("inventory.list_inventory_categories", {"query": "", "limit": 25}),
        ("inventory.search_stock_locations", {"query": "", "limit": 25}),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["interaction_type"] == "dynamic_form"
    assert "IDs" not in payload["description"]

    fields = {field["name"]: field for field in payload["fields"]}
    assert fields["inventory_category"]["type"] == "select"
    assert fields["inventory_category"]["options"][0]["label"] == "Men's Clothes"
    assert fields["inventory_category"]["options"][0]["value"] == "cat-1"
    assert fields["stock_location"]["type"] == "select"
    assert fields["stock_location"]["options"][0]["label"] == "Main Warehouse"
    assert fields["stock_location"]["options"][0]["value"] == "loc-1"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


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
        "create_wizard_flow",
    }

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == "This should not be used for delegated host requests."
