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
    def __init__(
        self,
        *,
        agent_name: str | None = None,
        hidden_agents: set[str] | None = None,
        failing_tools: set[str] | None = None,
    ) -> None:
        self._agent_name = (agent_name or "").strip() or None
        self._hidden_agents = set(hidden_agents or set())
        self._failing_tools = set(failing_tools or set())

    def _agents(self) -> list[dict[str, Any]]:
        agents = [
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
        if not self._hidden_agents:
            return agents
        return [agent for agent in agents if agent["name"] not in self._hidden_agents]

    def _tool_specs(self) -> list[ToolSpec]:
        specs = [
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
        if self._agent_name != "onboarding":
            return specs
        return specs + [
            ToolSpec(
                name="users.get_active_company_profile",
                description="Fetch the active company profile.",
                input_schema={"type": "object", "properties": {}, "required": []},
            ),
            ToolSpec(
                name="inventory.create_stock_location",
                description="Create a stock location.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string"},
                        "is_primary": {"type": "boolean"},
                    },
                    "required": ["name"],
                },
            ),
            ToolSpec(
                name="inventory.create_inventory_category",
                description="Create an inventory category.",
                input_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            ),
            ToolSpec(
                name="inventory.create_inventory",
                description="Create an inventory ledger.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "location_name": {"type": "string"},
                        "category_name": {"type": "string"},
                    },
                    "required": ["name"],
                },
            ),
            ToolSpec(
                name="product.create_product",
                description="Create a product.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "category_name": {"type": "string"},
                        "pos_ready": {"type": "boolean"},
                    },
                    "required": ["name"],
                },
            ),
        ]

    async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
        _ = ctx
        return self._tool_specs()

    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        _ = ctx
        FAKE_TOOL_CALLS.append((name, dict(arguments)))
        if name in self._failing_tools:
            raise RuntimeError(f"Simulated failure for {name}")
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
        if name == "users.get_active_company_profile":
            return {
                "id": "company-1",
                "name": "Intera Demo Company",
            }
        if name == "inventory.create_stock_location":
            return {
                "id": f"stock-location-{arguments.get('name','').lower().replace(' ', '-')}",
                "name": arguments.get("name"),
                "type": arguments.get("type"),
            }
        if name == "inventory.create_inventory_category":
            return {
                "id": f"inventory-category-{arguments.get('name','').lower().replace(' ', '-')}",
                "name": arguments.get("name"),
            }
        if name == "inventory.create_inventory":
            return {
                "id": f"inventory-{arguments.get('name','').lower().replace(' ', '-')}",
                "name": arguments.get("name"),
            }
        if name == "product.create_product":
            return {
                "id": f"product-{arguments.get('name','').lower().replace(' ', '-')}",
                "name": arguments.get("name"),
            }
        raise AssertionError(f"Unexpected tool call: {name}")


class FakeToolExecutorWithoutUsers(FakeToolExecutor):
    def __init__(self, *, agent_name: str | None = None) -> None:
        super().__init__(agent_name=agent_name, hidden_agents={"users"})


class FakeToolExecutorWithCategoryFailures(FakeToolExecutor):
    def __init__(self, *, agent_name: str | None = None) -> None:
        super().__init__(agent_name=agent_name, failing_tools={"inventory.create_inventory_category"})


def fake_llm_factory(*args: Any, **kwargs: Any) -> Any:
    _ = args, kwargs
    return FakeLlm()


def fake_interaction_llm_factory(*args: Any, **kwargs: Any) -> Any:
    _ = args, kwargs
    return FakeInteractionLlm()


def build_fake_tool_executor(*, agent_name: str | None = None) -> ToolExecutor:
    return FakeToolExecutor(agent_name=agent_name)


def build_fake_tool_executor_without_users(*, agent_name: str | None = None) -> ToolExecutor:
    return FakeToolExecutorWithoutUsers(agent_name=agent_name)


def build_fake_tool_executor_with_category_failures(*, agent_name: str | None = None) -> ToolExecutor:
    return FakeToolExecutorWithCategoryFailures(agent_name=agent_name)


def reset_fake_components() -> None:
    global FAKE_LLM_CALL_COUNT
    global FAKE_LLM_LAST_TOOLS
    FAKE_LLM_CALL_COUNT = 0
    FAKE_LLM_LAST_TOOLS = []
    FAKE_TOOL_CALLS.clear()
