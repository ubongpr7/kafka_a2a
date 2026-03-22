from __future__ import annotations
import json
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


class FakeRelationInteractionLlm:
    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> Any:
        global FAKE_LLM_CALL_COUNT
        global FAKE_LLM_LAST_TOOLS
        _ = messages
        tools = kwargs.get("tools")
        FAKE_LLM_CALL_COUNT += 1
        FAKE_LLM_LAST_TOOLS = [tool.name for tool in tools] if isinstance(tools, list) else []
        return SimpleNamespace(
            content=(
                '{"interaction_type":"dynamic_form","title":"Inventory Setup",'
                '"description":"Please provide the Inventory Category and Stock Location IDs for setup.",'
                '"fields":['
                '{"name":"inventory_category","label":"Inventory Category","type":"text","required":true},'
                '{"name":"stock_location","label":"Stock Location","type":"text","required":true}'
                ']}'
            )
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
        agents = self._registered_agents()
        if not self._hidden_agents:
            return agents
        return [agent for agent in agents if agent["name"] not in self._hidden_agents]

    def _registered_agents(self) -> list[dict[str, Any]]:
        if self._agent_name == "product":
            return [
                {
                    "name": "product_discovery",
                    "description": "Focused product specialist for catalog search, analytics, dashboard stats, and stock alerts.",
                    "skills": [
                        {
                            "id": "product_catalog_lookup",
                            "name": "Product Discovery",
                            "description": "Search the catalog and inspect product, variant, analytics, dashboard, and stock-alert information.",
                            "tags": ["product", "catalog", "search", "analytics", "dashboard"],
                            "examples": ["How many products do I have?", "Search for products matching t-shirt."],
                        }
                    ],
                },
                {
                    "name": "product_catalog_admin",
                    "description": "Focused product specialist for catalog creation, updates, deletion, and exports.",
                    "skills": [
                        {
                            "id": "product_catalog_admin",
                            "name": "Product Catalog Admin",
                            "description": "Create, update, delete, export, and bulk-seed products and variants.",
                            "tags": ["product", "catalog", "create", "update"],
                            "examples": ["Create the first products for this business."],
                        }
                    ],
                },
            ]
        if self._agent_name == "inventory":
            return [
                {
                    "name": "inventory_visibility",
                    "description": "Focused inventory specialist for stock posture, alerts, reservations, movements, and warehouse visibility.",
                    "skills": [
                        {
                            "id": "inventory_lookup",
                            "name": "Inventory Visibility",
                            "description": "Search inventories, inspect stock posture, and review low-stock or expiry alerts.",
                            "tags": ["inventory", "stock", "warehouse", "alerts"],
                            "examples": ["Show low-stock inventories."],
                        }
                    ],
                },
                {
                    "name": "inventory_setup",
                    "description": "Focused inventory specialist for stock-location, inventory-category, and inventory-item setup and maintenance workflows.",
                    "skills": [
                        {
                            "id": "inventory_setup_admin",
                            "name": "Inventory Setup Admin",
                            "description": "Create and update stock locations, inventory categories, and inventory items.",
                            "tags": ["inventory", "setup", "locations", "categories", "create"],
                            "examples": ["Create the main inventory item for onboarding."],
                        }
                    ],
                },
                {
                    "name": "inventory_procurement",
                    "description": "Focused inventory specialist for purchase orders and receiving.",
                    "skills": [
                        {
                            "id": "inventory_procurement",
                            "name": "Inventory Procurement",
                            "description": "Inspect and process purchase orders, receiving, and purchase returns.",
                            "tags": ["purchase-orders", "receiving", "procurement"],
                            "examples": ["Show open purchase orders."],
                        }
                    ],
                },
                {
                    "name": "inventory_fulfillment",
                    "description": "Focused inventory specialist for reservations, adjustments, transfers, and shipments.",
                    "skills": [
                        {
                            "id": "inventory_fulfillment",
                            "name": "Inventory Fulfillment",
                            "description": "Handle sales-order processing, reservations, adjustments, transfers, and shipment details.",
                            "tags": ["reservation", "adjustment", "transfer", "shipment"],
                            "examples": ["Transfer stock between locations."],
                        }
                    ],
                },
            ]
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
        return agents

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
                        "delegated_task_id": {"type": "string"},
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
        if self._agent_name == "inventory_setup":
            return specs + [
                ToolSpec(
                    name="inventory.search_stock_locations",
                    description="Search stock locations.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer"},
                        },
                        "required": [],
                    },
                ),
                ToolSpec(
                    name="inventory.list_inventory_categories",
                    description="List inventory categories.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer"},
                        },
                        "required": [],
                    },
                ),
                ToolSpec(
                    name="inventory.create_inventory_item",
                    description="Create an inventory item.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "payload": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "description": {"type": "string"},
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
                ToolSpec(
                    name="inventory.create_inventory_category",
                    description="Create an inventory category.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "payload": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "default_location_id": {
                                        "type": "string",
                                        "description": "UUID of default StockLocation",
                                    },
                                },
                                "required": ["name"],
                            }
                        },
                        "required": ["payload"],
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
                name="inventory.search_stock_locations",
                description="Search stock locations.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": [],
                },
            ),
            ToolSpec(
                name="inventory.list_inventory_categories",
                description="List inventory categories.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": [],
                },
            ),
            ToolSpec(
                name="product.get_product_categories",
                description="List product categories.",
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
                name="inventory.create_inventory_item",
                description="Create an inventory item.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "stock_location_id": {"type": "string"},
                        "location_name": {"type": "string"},
                        "category_id": {"type": "string"},
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
                        "category_id": {"type": "string"},
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
            visible_agents = self._agents()
            registered_agents = self._registered_agents()
            visible_names = {agent["name"] for agent in visible_agents}
            return {
                "agents": visible_agents,
                "registered_agents": registered_agents,
                "hidden_agents": [
                    agent for agent in registered_agents if agent["name"] not in visible_names
                ],
            }
        if name == "delegate_to_agent":
            request = str(arguments.get("request") or "")
            agent_name = str(arguments.get("agent_name") or "product")
            delegated_task_id = str(arguments.get("delegated_task_id") or "")
            if agent_name == "onboarding":
                if delegated_task_id == "delegated-onboarding-scope":
                    return {
                        "selected_agent": "onboarding",
                        "delegated_task_id": "delegated-onboarding-wizard",
                        "response_text": "",
                        "result_parts": [
                            {
                                "kind": "data",
                                "data": {
                                    "interaction_type": "wizard_flow",
                                    "title": "Full Inventory Setup Wizard",
                                    "description": "Fill in the setup details and I will prepare the onboarding action plan.",
                                    "steps": [
                                        {"id": "step_0", "title": "Stock Locations"},
                                        {"id": "step_1", "title": "Inventory Categories"},
                                    ],
                                    "allow_back": True,
                                    "show_progress": True,
                                    "workflow": "inventory_onboarding",
                                    "workflow_stage": "wizard",
                                    "onboarding_scope": "full_setup",
                                },
                            }
                        ],
                        "artifacts": {},
                        "status_updates": [
                            {
                                "state": "submitted",
                                "message": "delegated task continued",
                                "final": False,
                            },
                            {
                                "state": "input-required",
                                "message": "Fill in the onboarding details to continue.",
                                "final": True,
                            },
                        ],
                    }
                return {
                    "selected_agent": "onboarding",
                    "delegated_task_id": "delegated-onboarding-scope",
                    "response_text": "",
                    "result_parts": [
                        {
                            "kind": "data",
                            "data": {
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
                            },
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
                            "message": "Choose the setup area you want to complete first.",
                            "final": True,
                        },
                    ],
                }
            if agent_name == "inventory" and "Collected onboarding data JSON" in request:
                return {
                    "selected_agent": "inventory",
                    "delegated_task_id": "delegated-inventory-onboarding",
                    "response_text": "Created 1 stock location, 3 inventory categories, and 1 inventory item for onboarding.",
                    "result_parts": [
                        {
                            "kind": "text",
                            "text": "Created 1 stock location, 3 inventory categories, and 1 inventory item for onboarding.",
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
                            "message": "Created 1 stock location, 3 inventory categories, and 1 inventory item for onboarding.",
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
            if agent_name == "inventory_setup":
                return {
                    "selected_agent": "inventory_setup",
                    "delegated_task_id": "delegated-inventory-setup",
                    "response_text": "Inventory setup specialist engaged.",
                    "result_parts": [{"kind": "text", "text": "Inventory setup specialist engaged."}],
                    "artifacts": {},
                    "status_updates": [
                        {
                            "state": "submitted",
                            "message": "delegated task submitted",
                            "final": False,
                        },
                        {
                            "state": "completed",
                            "message": "Inventory setup specialist engaged.",
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
        if name == "inventory.search_stock_locations":
            return {
                "profile_id": 1,
                "count": 2,
                "results": [
                    {
                        "id": "loc-1",
                        "name": "Main Warehouse",
                        "location_type": "Warehouse",
                        "physical_address": "Lagos",
                    },
                    {
                        "id": "loc-2",
                        "name": "Front Store",
                        "location_type": "Store",
                        "physical_address": "Abuja",
                    },
                ],
            }
        if name == "inventory.list_inventory_categories":
            return {
                "profile_id": 1,
                "category": {
                    "count": 2,
                    "results": [
                        {"id": "cat-1", "name": "Men's Clothes", "description": "Menswear"},
                        {"id": "cat-2", "name": "Shoes", "description": "Footwear"},
                    ],
                },
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
        if name == "inventory.create_inventory_item":
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


class FakeToolExecutorWithoutUsersOrOnboarding(FakeToolExecutor):
    def __init__(self, *, agent_name: str | None = None) -> None:
        super().__init__(agent_name=agent_name, hidden_agents={"users", "onboarding"})


class FakeToolExecutorWithCategoryFailures(FakeToolExecutor):
    def __init__(self, *, agent_name: str | None = None) -> None:
        super().__init__(agent_name=agent_name, failing_tools={"inventory.create_inventory_category"})


class FakeWrappedLookupToolExecutor(FakeToolExecutor):
    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        result = await super().call_tool(name=name, arguments=arguments, ctx=ctx)
        if name not in {
            "inventory.search_stock_locations",
            "inventory.list_inventory_categories",
            "product.get_product_categories",
            "inventory.list_inventory_items",
            "inventory.search_inventory_items",
        }:
            return result
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result),
                }
            ],
            "structuredContent": result,
            "isError": False,
        }


class FakeCategoryTextWrappedToolExecutor(FakeToolExecutor):
    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        result = await super().call_tool(name=name, arguments=arguments, ctx=ctx)
        if name != "inventory.list_inventory_categories":
            return result
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Inventory categories lookup result:\n```json\n" + json.dumps(result) + "\n```",
                }
            ],
            "isError": False,
        }


def fake_llm_factory(*args: Any, **kwargs: Any) -> Any:
    _ = args, kwargs
    return FakeLlm()


def fake_interaction_llm_factory(*args: Any, **kwargs: Any) -> Any:
    _ = args, kwargs
    return FakeInteractionLlm()


def fake_relation_interaction_llm_factory(*args: Any, **kwargs: Any) -> Any:
    _ = args, kwargs
    return FakeRelationInteractionLlm()


def build_fake_tool_executor(*, agent_name: str | None = None) -> ToolExecutor:
    return FakeToolExecutor(agent_name=agent_name)


def build_fake_tool_executor_without_users(*, agent_name: str | None = None) -> ToolExecutor:
    return FakeToolExecutorWithoutUsers(agent_name=agent_name)


def build_fake_tool_executor_without_users_or_onboarding(*, agent_name: str | None = None) -> ToolExecutor:
    return FakeToolExecutorWithoutUsersOrOnboarding(agent_name=agent_name)


def build_fake_tool_executor_with_category_failures(*, agent_name: str | None = None) -> ToolExecutor:
    return FakeToolExecutorWithCategoryFailures(agent_name=agent_name)


def build_fake_wrapped_lookup_tool_executor(*, agent_name: str | None = None) -> ToolExecutor:
    return FakeWrappedLookupToolExecutor(agent_name=agent_name)


def build_fake_category_text_wrapped_tool_executor(*, agent_name: str | None = None) -> ToolExecutor:
    return FakeCategoryTextWrappedToolExecutor(agent_name=agent_name)


def reset_fake_components() -> None:
    global FAKE_LLM_CALL_COUNT
    global FAKE_LLM_LAST_TOOLS
    FAKE_LLM_CALL_COUNT = 0
    FAKE_LLM_LAST_TOOLS = []
    FAKE_TOOL_CALLS.clear()
