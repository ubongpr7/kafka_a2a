from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from kafka_a2a.tools import ToolContext, ToolExecutor, ToolSpec


FAKE_LLM_CALL_COUNT = 0
FAKE_TOOL_CALLS: list[tuple[str, dict[str, Any]]] = []
FAKE_LLM_LAST_TOOLS: list[str] = []


class FakeLlm:
    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> Any:
        global FAKE_LLM_CALL_COUNT
        global FAKE_LLM_LAST_TOOLS
        _ = messages
        FAKE_LLM_CALL_COUNT += 1
        tools = kwargs.get("tools")
        FAKE_LLM_LAST_TOOLS = [tool.name for tool in tools] if isinstance(tools, list) else []
        return SimpleNamespace(content="This should not be used for delegated host requests.")


class FakeInteractionLlm:
    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> Any:
        global FAKE_LLM_CALL_COUNT
        global FAKE_LLM_LAST_TOOLS
        _ = messages
        tools = kwargs.get("tools")
        FAKE_LLM_CALL_COUNT += 1
        FAKE_LLM_LAST_TOOLS = [tool.name for tool in tools] if isinstance(tools, list) else []
        return SimpleNamespace(
            content='{"interaction_type":"dynamic_form","title":"Need more detail","description":"Pick an inventory.","fields":[{"name":"inventory","label":"Inventory","type":"text","required":true}]}'
        )


class FakeToolExecutor(ToolExecutor):
    def _agents(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "onboarding",
                "description": "Workflow specialist agent for guided inventory onboarding and setup.",
                "skills": [
                    {
                        "name": "Inventory Environment Onboarding",
                        "description": "Guide stock-location, category, inventory, and initial product setup.",
                        "tags": ["onboarding", "setup", "inventory", "stock-locations", "categories"],
                        "examples": ["Help me set up my inventory workspace from scratch."],
                    }
                ],
            },
            {
                "name": "product",
                "description": "Product service specialist agent for catalog search.",
                "skills": [
                    {
                        "name": "Product Search",
                        "description": "Search products by name, SKU, or barcode.",
                        "tags": ["product", "catalog", "search"],
                        "examples": ["Search for products matching t-shirt."],
                    }
                ],
            },
            {
                "name": "inventory",
                "description": "Inventory service specialist agent for stock and warehouse tasks.",
                "skills": [
                    {
                        "name": "Inventory Lookup",
                        "description": "Inspect stock, locations, and adjustments.",
                        "tags": ["inventory", "stock", "warehouse"],
                        "examples": ["Check stock for a SKU."],
                    }
                ],
            },
            {
                "name": "pos",
                "description": "POS service specialist agent for sessions, live orders, and cashier workflows.",
                "skills": [
                    {
                        "name": "POS Operations",
                        "description": "Inspect current sessions, POS orders, and held carts.",
                        "tags": ["pos", "session", "orders", "cashier"],
                        "examples": ["Show held POS carts."],
                    }
                ],
            },
            {
                "name": "users",
                "description": "User and workspace specialist agent for staff, invitations, roles, and permissions.",
                "skills": [
                    {
                        "name": "Workspace Membership Visibility",
                        "description": "Inspect staff, invitations, and workspace membership state.",
                        "tags": ["users", "staff", "workspace", "roles", "permissions"],
                        "examples": ["How many staff do I have?"],
                    }
                ],
            },
        ]

    async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
        _ = ctx
        return [
            ToolSpec(
                name="list_available_agents",
                description="List downstream specialist agents.",
                input_schema={"type": "object", "properties": {}, "required": []},
            ),
            ToolSpec(
                name="delegate_to_agent",
                description="Delegate to a specialist agent.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "request": {"type": "string"},
                        "agent_name": {"type": "string"},
                    },
                    "required": ["request"],
                },
            ),
            ToolSpec(
                name="create_multiple_choice",
                description="Render a pick-one list.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "options": {"type": "array"},
                        "multiple": {"type": "boolean"},
                        "allow_input": {"type": "boolean"},
                    },
                    "required": ["title", "description", "options", "multiple", "allow_input"],
                },
            ),
            ToolSpec(
                name="create_wizard_flow",
                description="Render a multi-step onboarding wizard.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "steps": {"type": "array"},
                        "allow_back": {"type": "boolean"},
                        "show_progress": {"type": "boolean"},
                    },
                    "required": ["title", "description", "steps", "allow_back", "show_progress"],
                },
            ),
        ]

    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        _ = ctx
        FAKE_TOOL_CALLS.append((name, dict(arguments)))
        if name == "list_available_agents":
            return {"agents": self._agents()}
        if name == "delegate_to_agent":
            request = str(arguments.get("request") or "")
            agent_name = str(arguments.get("agent_name") or "product")
            if agent_name == "onboarding":
                return {
                    "selected_agent": "onboarding",
                    "delegated_task_id": "delegated-onboarding-summary",
                    "response_text": (
                        "I can guide you through stock locations, inventory categories, inventory setup, "
                        "and initial product onboarding."
                    ),
                    "result_parts": [
                        {
                            "kind": "text",
                            "text": (
                                "I can guide you through stock locations, inventory categories, inventory setup, "
                                "and initial product onboarding."
                            ),
                        }
                    ],
                    "artifacts": {},
                    "status_updates": [
                        {
                            "state": "submitted",
                            "message": "delegated task submitted",
                            "final": False,
                        },
                        {
                            "state": "completed",
                            "message": (
                                "I can guide you through stock locations, inventory categories, inventory setup, "
                                "and initial product onboarding."
                            ),
                            "final": True,
                        },
                    ],
                }
            if agent_name == "inventory" and "Collected onboarding data JSON" in request:
                return {
                    "selected_agent": "inventory",
                    "delegated_task_id": "delegated-inventory-onboarding",
                    "response_text": "Created 1 stock location, 3 inventory categories, and 1 inventory ledger for onboarding.",
                    "result_parts": [
                        {
                            "kind": "text",
                            "text": "Created 1 stock location, 3 inventory categories, and 1 inventory ledger for onboarding.",
                        }
                    ],
                    "artifacts": {},
                    "status_updates": [
                        {
                            "state": "submitted",
                            "message": "delegated task submitted",
                            "final": False,
                        },
                        {
                            "state": "working",
                            "message": "creating onboarding foundation records",
                            "final": False,
                        },
                        {
                            "state": "completed",
                            "message": "Created 1 stock location, 3 inventory categories, and 1 inventory ledger for onboarding.",
                            "final": True,
                        },
                    ],
                }
            if agent_name == "product" and "Collected onboarding data JSON" in request:
                return {
                    "selected_agent": "product",
                    "delegated_task_id": "delegated-product-onboarding",
                    "response_text": "Created 3 initial products for onboarding.",
                    "result_parts": [
                        {
                            "kind": "text",
                            "text": "Created 3 initial products for onboarding.",
                        }
                    ],
                    "artifacts": {},
                    "status_updates": [
                        {
                            "state": "submitted",
                            "message": "delegated task submitted",
                            "final": False,
                        },
                        {
                            "state": "working",
                            "message": "creating initial products",
                            "final": False,
                        },
                        {
                            "state": "completed",
                            "message": "Created 3 initial products for onboarding.",
                            "final": True,
                        },
                    ],
                }
            if agent_name == "users":
                if "how many staff" in request.lower():
                    return {
                        "selected_agent": "users",
                        "delegated_task_id": "delegated-users-count",
                        "response_text": "You have 12 staff members in the current workspace.",
                        "result_parts": [{"kind": "text", "text": "You have 12 staff members in the current workspace."}],
                        "artifacts": {},
                        "status_updates": [
                            {
                                "state": "submitted",
                                "message": "delegated task submitted",
                                "final": False,
                            },
                            {
                                "state": "completed",
                                "message": "You have 12 staff members in the current workspace.",
                                "final": True,
                            },
                        ],
                    }
                return {
                    "selected_agent": "users",
                    "delegated_task_id": "delegated-users-summary",
                    "response_text": "I can help with staff lookup, invitations, roles, groups, permissions, and workspace access.",
                    "result_parts": [
                        {
                            "kind": "text",
                            "text": "I can help with staff lookup, invitations, roles, groups, permissions, and workspace access.",
                        }
                    ],
                    "artifacts": {},
                    "status_updates": [
                        {
                            "state": "submitted",
                            "message": "delegated task submitted",
                            "final": False,
                        },
                        {
                            "state": "completed",
                            "message": "I can help with staff lookup, invitations, roles, groups, permissions, and workspace access.",
                            "final": True,
                        },
                    ],
                }
            if "ambiguous" in request.lower():
                return {
                    "selected_agent": agent_name,
                    "delegated_task_id": "delegated-2",
                    "response_text": '{"interaction_type":"multiple_choice","title":"Which inventory?","description":"Choose the inventory to continue.","options":[{"id":"main","label":"Main store"},{"id":"warehouse","label":"Warehouse"}]}',
                    "result_parts": [
                        {
                            "kind": "text",
                            "text": '{"interaction_type":"multiple_choice","title":"Which inventory?","description":"Choose the inventory to continue.","options":[{"id":"main","label":"Main store"},{"id":"warehouse","label":"Warehouse"}]}',
                        }
                    ],
                    "artifacts": {},
                    "status_updates": [
                        {
                            "state": "submitted",
                            "message": "delegated task submitted",
                            "final": False,
                        },
                        {
                            "state": "input-required",
                            "message": "Which inventory should I use?",
                            "final": True,
                        },
                    ],
                }
            return {
                "selected_agent": agent_name,
                "delegated_task_id": "delegated-1",
                "response_text": "Found 3 products matching t-shirt.",
                "result_parts": [{"kind": "text", "text": "Found 3 products matching t-shirt."}],
                "artifacts": {
                    "matches": [
                        {
                            "kind": "data",
                            "data": {
                                "count": 3,
                                "items": ["Classic T-Shirt", "V-Neck T-Shirt", "Sport T-Shirt"],
                            },
                        }
                    ]
                },
                "status_updates": [
                    {
                        "state": "submitted",
                        "message": "delegated task submitted",
                        "final": False,
                    },
                    {
                        "state": "working",
                        "message": "searching catalog",
                        "final": False,
                    },
                    {
                        "state": "completed",
                        "message": "Found 3 products matching t-shirt.",
                        "final": True,
                    },
                ],
            }
        if name == "create_multiple_choice":
            return {
                "interaction_type": "multiple_choice",
                "title": arguments.get("title") or "Choose",
                "description": arguments.get("description") or "Choose one option.",
                "options": list(arguments.get("options") or []),
                "multiple": bool(arguments.get("multiple")),
                "allow_input": bool(arguments.get("allow_input")),
            }
        if name == "create_wizard_flow":
            return {
                "interaction_type": "wizard_flow",
                "title": arguments.get("title") or "Wizard",
                "description": arguments.get("description") or "Complete the steps.",
                "steps": list(arguments.get("steps") or []),
                "allow_back": bool(arguments.get("allow_back")),
                "show_progress": bool(arguments.get("show_progress")),
            }
        raise AssertionError(f"Unexpected tool call: {name}")


class FakeToolExecutorWithoutUsers(FakeToolExecutor):
    def _agents(self) -> list[dict[str, Any]]:
        return [agent for agent in super()._agents() if agent["name"] != "users"]


def fake_llm_factory(*args: Any, **kwargs: Any) -> Any:
    _ = args, kwargs
    return FakeLlm()


def fake_interaction_llm_factory(*args: Any, **kwargs: Any) -> Any:
    _ = args, kwargs
    return FakeInteractionLlm()


def build_fake_tool_executor() -> ToolExecutor:
    return FakeToolExecutor()


def build_fake_tool_executor_without_users() -> ToolExecutor:
    return FakeToolExecutorWithoutUsers()


def reset_fake_components() -> None:
    global FAKE_LLM_CALL_COUNT
    global FAKE_LLM_LAST_TOOLS
    FAKE_LLM_CALL_COUNT = 0
    FAKE_LLM_LAST_TOOLS = []
    FAKE_TOOL_CALLS.clear()
