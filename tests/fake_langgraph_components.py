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


class FakeUuidFailureLlm:
    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> Any:
        global FAKE_LLM_CALL_COUNT
        global FAKE_LLM_LAST_TOOLS
        _ = messages
        tools = kwargs.get("tools")
        FAKE_LLM_CALL_COUNT += 1
        FAKE_LLM_LAST_TOOLS = [tool.name for tool in tools] if isinstance(tools, list) else []
        return SimpleNamespace(
            content=(
                '{"kind":"tool-call","name":"inventory.create_inventory_item","arguments":{"payload":{"name_snapshot":"Fashion Master Inventory","inventory_category_id":"not-a-uuid"}}}'
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
                    "name": "marketplace_sourcing",
                    "description": "Focused marketplace specialist for online product sourcing and offer comparison.",
                    "skills": [
                        {
                            "id": "marketplace_product_search",
                            "name": "Marketplace Sourcing",
                            "description": "Search online marketplaces and compare supplier offers.",
                            "tags": ["marketplace", "sourcing", "online", "compare", "price"],
                            "examples": ["Search online marketplaces for Adidas shoes."],
                        }
                    ],
                },
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
            ToolSpec(
                name="create_dynamic_form",
                description="Render a dynamic form.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "fields": {"type": "array"},
                    },
                    "required": ["title", "description", "fields"],
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
                    name="inventory.list_stock_locations",
                    description="List stock locations.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer"},
                        },
                        "required": [],
                    },
                ),
                ToolSpec(
                    name="inventory.list_stock_location_types",
                    description="List stock location types.",
                    input_schema={"type": "object", "properties": {}, "required": []},
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
                ToolSpec(
                    name="inventory.update_stock_location",
                    description="Update a stock location.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "location_id": {"type": "string"},
                            "payload": {
                                "type": "object",
                                "properties": {
                                    "parent_id": {
                                        "type": "string",
                                        "description": "UUID of parent StockLocation",
                                    },
                                },
                                "required": [],
                            },
                        },
                        "required": ["location_id", "payload"],
                    },
                ),
            ]
        if self._agent_name == "product_catalog_admin":
            return specs + [
                ToolSpec(
                    name="product.get_product_categories",
                    description="List product categories.",
                    input_schema={"type": "object", "properties": {}, "required": []},
                ),
                ToolSpec(
                    name="product.search_products",
                    description="Search products.",
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
                    name="product.create_product",
                    description="Create a product.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "payload": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "description": {"type": "string"},
                                    "category_ref_id": {"type": "string"},
                                    "base_price": {"type": "string"},
                                    "quick_sale": {"type": "boolean"},
                                    "pos_category": {"type": "string"},
                                },
                                "required": ["name"],
                            }
                        },
                        "required": ["payload"],
                    },
                ),
                ToolSpec(
                    name="product.update_product",
                    description="Update a product.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "string"},
                            "payload": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "description": {"type": "string"},
                                    "category_ref_id": {"type": "string"},
                                    "base_price": {"type": "string"},
                                    "quick_sale": {"type": "boolean"},
                                    "pos_category": {"type": "string"},
                                },
                                "required": [],
                            },
                        },
                        "required": ["product_id", "payload"],
                    },
                ),
            ]
        if self._agent_name == "marketplace_sourcing":
            return specs + [
                ToolSpec(
                    name="search_marketplace_products",
                    description="Search online marketplaces.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "marketplaces": {"type": "array"},
                            "max_results": {"type": "integer"},
                        },
                        "required": ["query"],
                    },
                ),
                ToolSpec(
                    name="compare_marketplace_products",
                    description="Compare selected marketplace products.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "items": {"type": "array"},
                            "title": {"type": "string"},
                        },
                        "required": ["items"],
                    },
                ),
            ]
        if self._agent_name == "inventory_fulfillment":
            return specs + [
                ToolSpec(
                    name="inventory.list_stock_locations",
                    description="List stock locations.",
                    input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []},
                ),
                ToolSpec(
                    name="inventory.list_inventory_items",
                    description="List inventory items.",
                    input_schema={"type": "object", "properties": {}, "required": []},
                ),
                ToolSpec(
                    name="inventory.create_stock_reservation",
                    description="Create a stock reservation.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "payload": {
                                "type": "object",
                                "properties": {
                                    "inventory_item_id": {"type": "string"},
                                    "stock_location_id": {"type": "string"},
                                    "reserved_quantity": {"type": "string"},
                                    "external_order_type": {"type": "string"},
                                    "external_order_id": {"type": "string"},
                                    "external_order_line_id": {"type": "string"},
                                    "notes": {"type": "string"},
                                },
                                "required": [
                                    "inventory_item_id",
                                    "stock_location_id",
                                    "reserved_quantity",
                                    "external_order_type",
                                    "external_order_id",
                                ],
                            },
                        },
                        "required": ["payload"],
                    },
                ),
                ToolSpec(
                    name="inventory.transfer_location_stock",
                    description="Transfer stock between locations.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "location_id": {"type": "string"},
                            "payload": {
                                "type": "object",
                                "properties": {
                                    "transfers": {"type": "array"},
                                    "reason": {"type": "string"},
                                    "notes": {"type": "string"},
                                },
                                "required": ["transfers"],
                            },
                        },
                        "required": ["location_id", "payload"],
                    },
                ),
                ToolSpec(
                    name="inventory.adjust_inventory_item_stock",
                    description="Adjust stock on an inventory item.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "inventory_item_id": {"type": "string"},
                            "payload": {
                                "type": "object",
                                "properties": {
                                    "adjustments": {"type": "array"},
                                    "reason": {"type": "string"},
                                    "notes": {"type": "string"},
                                },
                                "required": ["adjustments"],
                            },
                        },
                        "required": ["inventory_item_id", "payload"],
                    },
                ),
            ]
        if self._agent_name == "inventory_procurement":
            return specs + [
                ToolSpec(
                    name="inventory.search_purchase_orders",
                    description="Search purchase orders.",
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": []},
                ),
                ToolSpec(
                    name="inventory.list_inventory_items",
                    description="List inventory items.",
                    input_schema={"type": "object", "properties": {}, "required": []},
                ),
                ToolSpec(
                    name="inventory.add_purchase_order_line_item",
                    description="Add purchase-order line item.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "purchase_order_id": {"type": "string"},
                            "payload": {
                                "type": "object",
                                "properties": {
                                    "inventory_item_id": {"type": "string"},
                                    "quantity": {"type": "string"},
                                    "unit_price": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["inventory_item_id", "quantity", "unit_price"],
                            },
                        },
                        "required": ["purchase_order_id", "payload"],
                    },
                ),
            ]
        if self._agent_name == "product_merchandising":
            return specs + [
                ToolSpec(
                    name="product.search_products",
                    description="Search products.",
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": []},
                ),
                ToolSpec(
                    name="product.get_product_categories",
                    description="List product categories.",
                    input_schema={"type": "object", "properties": {}, "required": []},
                ),
                ToolSpec(
                    name="product.update_product",
                    description="Update a product.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "string"},
                            "payload": {
                                "type": "object",
                                "properties": {
                                    "category_ref_id": {"type": "string"},
                                    "quick_sale": {"type": "boolean"},
                                    "is_featured": {"type": "boolean"},
                                    "pos_category": {"type": "string"},
                                },
                                "required": [],
                            },
                        },
                        "required": ["product_id", "payload"],
                    },
                ),
            ]
        if self._agent_name == "product_pricing":
            return specs + [
                ToolSpec(
                    name="product.search_products",
                    description="Search products.",
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": []},
                ),
                ToolSpec(
                    name="product.get_product_categories",
                    description="List product categories.",
                    input_schema={"type": "object", "properties": {}, "required": []},
                ),
                ToolSpec(
                    name="product.create_pricing_strategy",
                    description="Create pricing strategy.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "payload": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "strategy": {"type": "string"},
                                    "product_id": {"type": "string"},
                                    "margin_percentage": {"type": "string"},
                                    "market_multiplier": {"type": "string"},
                                    "min_price": {"type": "string"},
                                    "max_price": {"type": "string"},
                                },
                                "required": ["name", "strategy"],
                            },
                        },
                        "required": ["payload"],
                    },
                ),
                ToolSpec(
                    name="product.create_pricing_rule",
                    description="Create pricing rule.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "payload": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "rule_type": {"type": "string"},
                                    "product_id": {"type": "string"},
                                    "category_ref_id": {"type": "string"},
                                    "discount_type": {"type": "string"},
                                    "value": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["name", "rule_type", "discount_type", "value"],
                            },
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
                name="inventory.list_stock_locations",
                description="List stock locations.",
                input_schema={
                    "type": "object",
                    "properties": {
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
                        "payload": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "location_type_name": {"type": "string"},
                                "structural": {"type": "boolean"},
                                "parent_id": {"type": "string"},
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
                                "default_location_id": {"type": "string"},
                            },
                            "required": ["name"],
                        }
                    },
                    "required": ["payload"],
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
                                "name_snapshot": {"type": "string"},
                                "description": {"type": "string"},
                                "inventory_category_id": {"type": "string"},
                            },
                            "required": ["name_snapshot"],
                        }
                    },
                    "required": ["payload"],
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
                if "Collected onboarding data JSON" in request:
                    return {
                        "selected_agent": "onboarding",
                        "delegated_task_id": "delegated-onboarding-create",
                        "response_text": "Created 3 stock locations, 2 inventory categories, and 1 inventory item for onboarding.",
                        "result_parts": [
                            {
                                "kind": "text",
                                "text": "Created 3 stock locations, 2 inventory categories, and 1 inventory item for onboarding.",
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
                                "message": "applying onboarding setup plan",
                                "final": False,
                            },
                            {
                                "state": "completed",
                                "message": "Created 3 stock locations, 2 inventory categories, and 1 inventory item for onboarding.",
                                "final": True,
                            },
                        ],
                    }
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
            if agent_name == "inventory" and "set up inventory locations and then create products" in request.lower():
                return {
                    "selected_agent": "inventory",
                    "delegated_task_id": "delegated-inventory-multistep",
                    "response_text": "Inventory locations created successfully.",
                    "result_parts": [{"kind": "text", "text": "Inventory locations created successfully."}],
                    "artifacts": {},
                    "status_updates": [
                        {
                            "state": "submitted",
                            "message": "delegated task submitted",
                            "final": False,
                        },
                        {
                            "state": "completed",
                            "message": "Inventory locations created successfully.",
                            "final": True,
                        },
                    ],
                }
            if agent_name == "inventory" and (
                "group my inventories into categories and assign items" in request.lower()
                or "group the inventories into categories and assign items" in request.lower()
            ):
                return {
                    "selected_agent": "inventory",
                    "delegated_task_id": "delegated-inventory-categorize",
                    "response_text": (
                        "I reviewed the inventory items and prepared a category plan. "
                        "Once you confirm, I'll create the categories and assign the items."
                    ),
                    "result_parts": [
                        {
                            "kind": "text",
                            "text": (
                                "I reviewed the inventory items and prepared a category plan. "
                                "Once you confirm, I'll create the categories and assign the items."
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
                            "message": "Awaiting your confirmation to create the categories and assignments.",
                            "final": True,
                        },
                    ],
                }
            if agent_name == "inventory" and delegated_task_id == "delegated-inventory-categorize":
                return {
                    "selected_agent": "inventory",
                    "delegated_task_id": "delegated-inventory-categorize",
                    "response_text": "Created 3 inventory categories and assigned 14 inventory items.",
                    "result_parts": [
                        {
                            "kind": "text",
                            "text": "Created 3 inventory categories and assigned 14 inventory items.",
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
                            "state": "completed",
                            "message": "Created 3 inventory categories and assigned 14 inventory items.",
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
            if agent_name == "product" and "continue the user's multi-domain workflow" in request.lower():
                return {
                    "selected_agent": "product",
                    "delegated_task_id": "delegated-product-multistep",
                    "response_text": "Initial products created successfully.",
                    "result_parts": [{"kind": "text", "text": "Initial products created successfully."}],
                    "artifacts": {},
                    "status_updates": [
                        {
                            "state": "submitted",
                            "message": "delegated task submitted",
                            "final": False,
                        },
                        {
                            "state": "completed",
                            "message": "Initial products created successfully.",
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
        if name == "create_dynamic_form":
            return {
                "interaction_type": "dynamic_form",
                "title": arguments.get("title") or "Form",
                "description": arguments.get("description") or "Complete the form.",
                "fields": list(arguments.get("fields") or []),
            }
        if name == "users.get_active_company_profile":
            return {
                "id": "company-1",
                "name": "Intera Demo Company",
            }
        if name in {"inventory.search_stock_locations", "inventory.list_stock_locations"}:
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
        if name == "inventory.list_stock_location_types":
            return {
                "results": [
                    {"value": "Warehouse", "label": "Warehouse"},
                    {"value": "Shelf", "label": "Shelf"},
                    {"value": "Store", "label": "Store"},
                ]
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
            payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else arguments
            return {
                "location": {
                    "id": f"stock-location-{str(payload.get('name','')).lower().replace(' ', '-')}",
                    "name": payload.get("name"),
                    "location_type": payload.get("location_type_name"),
                    "parent_id": payload.get("parent_id"),
                }
            }
        if name == "inventory.create_inventory_category":
            payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else arguments
            return {
                "category": {
                    "id": f"inventory-category-{str(payload.get('name','')).lower().replace(' ', '-')}",
                    "name": payload.get("name"),
                    "default_location_id": payload.get("default_location_id"),
                }
            }
        if name == "inventory.update_stock_location":
            payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else {}
            return {
                "location": {
                    "id": arguments.get("location_id"),
                    "parent_id": payload.get("parent_id"),
                }
            }
        if name == "inventory.create_inventory_item":
            payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else arguments
            return {
                "inventory_item": {
                    "id": f"inventory-{str(payload.get('name_snapshot') or payload.get('name') or '').lower().replace(' ', '-')}",
                    "name": payload.get("name_snapshot") or payload.get("name"),
                    "inventory_category_id": payload.get("inventory_category_id") or payload.get("category_id"),
                }
            }
        if name == "product.get_product_categories":
            return {
                "count": 2,
                "results": [
                    {"id": "prod-cat-1", "name": "Apparel"},
                    {"id": "prod-cat-2", "name": "Footwear"},
                ],
            }
        if name == "product.search_products":
            return {
                "count": 2,
                "results": [
                    {"id": "prod-1", "name": "Men's Oxford Shirt"},
                    {"id": "prod-2", "name": "Leather Tote Bag"},
                ],
            }
        if name == "product.create_product":
            payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else arguments
            return {
                "id": f"product-{str(payload.get('name') or '').lower().replace(' ', '-')}",
                "name": payload.get("name"),
            }
        if name == "product.update_product":
            payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else {}
            return {
                "id": arguments.get("product_id"),
                "name": payload.get("name"),
            }
        if name == "product.create_pricing_strategy":
            payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else {}
            return {
                "pricing_strategy": {
                    "id": "pricing-strategy-1",
                    "name": payload.get("name"),
                    "strategy": payload.get("strategy"),
                    "product_id": payload.get("product_id"),
                }
            }
        if name == "product.create_pricing_rule":
            payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else {}
            return {
                "pricing_rule": {
                    "id": "pricing-rule-1",
                    "name": payload.get("name"),
                    "rule_type": payload.get("rule_type"),
                    "product_id": payload.get("product_id"),
                    "category_ref_id": payload.get("category_ref_id"),
                }
            }
        if name == "search_marketplace_products":
            query = str(arguments.get("query") or "").strip()
            return {
                "interaction_type": "marketplace_results",
                "title": f"Marketplace results for “{query}”",
                "description": f"Found 3 marketplace matches for {query}.",
                "query": query,
                "products": [
                    {
                        "id": "adidas-1",
                        "title": "Adidas Ultraboost Light",
                        "marketplace": "Amazon",
                        "product_url": "https://amazon.com/dp/adidas-ultraboost-light",
                        "price": "USD 129.99",
                        "total_price": "USD 129.99",
                        "total_price_value": 129.99,
                        "score": 0.92,
                    },
                    {
                        "id": "adidas-2",
                        "title": "Adidas Adizero SL 2",
                        "marketplace": "eBay",
                        "product_url": "https://ebay.com/itm/adidas-adizero-sl-2",
                        "price": "USD 119.00",
                        "total_price": "USD 119.00",
                        "total_price_value": 119.0,
                        "score": 0.89,
                    },
                    {
                        "id": "adidas-3",
                        "title": "Adidas Duramo Speed",
                        "marketplace": "AliExpress",
                        "product_url": "https://aliexpress.com/item/adidas-duramo-speed",
                        "price": "USD 78.50",
                        "total_price": "USD 78.50",
                        "total_price_value": 78.5,
                        "score": 0.84,
                    },
                ],
                "summary": {
                    "query": query,
                    "result_count": 3,
                    "cheapest_offer": {
                        "title": "Adidas Duramo Speed",
                        "marketplace": "AliExpress",
                        "price": "USD 78.50",
                        "product_url": "https://aliexpress.com/item/adidas-duramo-speed",
                    },
                },
                "available_marketplaces": ["Amazon", "eBay", "AliExpress"],
                "allow_selection": True,
                "allow_compare": True,
                "max_selection": 4,
                "workflow": "marketplace_sourcing",
                "workflow_stage": "results",
            }
        if name == "compare_marketplace_products":
            items = arguments.get("items") if isinstance(arguments.get("items"), list) else []
            return {
                "interaction_type": "comparison_view",
                "title": str(arguments.get("title") or "Compare marketplace products"),
                "description": "Review the selected marketplace offers side by side.",
                "items": items,
                "comparison_fields": ["marketplace", "price", "total_price"],
                "allow_selection": True,
                "highlight_differences": True,
                "workflow": "marketplace_sourcing",
                "workflow_stage": "comparison",
            }
        if name in {"inventory.list_inventory_items", "inventory.search_inventory_items"}:
            return {
                "count": 2,
                "results": [
                    {"id": "inv-1", "name": "Men's Oxford Shirt Inventory"},
                    {"id": "inv-2", "name": "Leather Tote Bag Inventory"},
                ],
            }
        if name == "inventory.search_purchase_orders":
            return {
                "count": 2,
                "results": [
                    {"id": "po-1", "order_no": "PO-1001", "supplier_name": "Style Source Ltd"},
                    {"id": "po-2", "order_no": "PO-1002", "supplier_name": "Urban Wholesale"},
                ],
            }
        if name == "inventory.add_purchase_order_line_item":
            payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else {}
            return {
                "line_item": {
                    "purchase_order_id": arguments.get("purchase_order_id"),
                    "inventory_item_id": payload.get("inventory_item_id"),
                    "quantity": payload.get("quantity"),
                    "unit_price": payload.get("unit_price"),
                    "description": payload.get("description"),
                }
            }
        if name == "inventory.transfer_location_stock":
            payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else {}
            transfers = payload.get("transfers") or []
            first = transfers[0] if isinstance(transfers, list) and transfers else {}
            return {
                "stock_transfer": {
                    "location_id": arguments.get("location_id"),
                    "inventory_item_id": first.get("inventory_item_id"),
                    "to_location_id": first.get("to_location_id"),
                    "quantity": first.get("quantity"),
                }
            }
        if name == "inventory.create_stock_reservation":
            payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else {}
            return {
                "reservation": {
                    "inventory_item_id": payload.get("inventory_item_id"),
                    "stock_location_id": payload.get("stock_location_id"),
                    "reserved_quantity": payload.get("reserved_quantity"),
                    "external_order_type": payload.get("external_order_type"),
                    "external_order_id": payload.get("external_order_id"),
                }
            }
        if name == "inventory.adjust_inventory_item_stock":
            payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else {}
            adjustments = payload.get("adjustments") or []
            first = adjustments[0] if isinstance(adjustments, list) and adjustments else {}
            return {
                "inventory_adjustment": {
                    "inventory_item_id": arguments.get("inventory_item_id"),
                    "quantity": first.get("quantity"),
                    "adjustment_type": first.get("adjustment_type"),
                }
            }
        raise AssertionError(f"Unexpected tool call: {name}")


class FakeToolExecutorWithoutUsers(FakeToolExecutor):
    def __init__(self, *, agent_name: str | None = None) -> None:
        super().__init__(agent_name=agent_name, hidden_agents={"users"})


class FakeToolExecutorWithUuidFailure(FakeToolExecutor):
    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        if name == "inventory.create_inventory_item":
            FAKE_TOOL_CALLS.append((name, dict(arguments)))
            raise RuntimeError("invalid UUID input syntax for type uuid")
        return await super().call_tool(name=name, arguments=arguments, ctx=ctx)


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
            "inventory.list_stock_locations",
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


def build_fake_tool_executor_with_uuid_failure(*, agent_name: str | None = None) -> ToolExecutor:
    return FakeToolExecutorWithUuidFailure(agent_name=agent_name)


def reset_fake_components() -> None:
    global FAKE_LLM_CALL_COUNT
    global FAKE_LLM_LAST_TOOLS
    FAKE_LLM_CALL_COUNT = 0
    FAKE_LLM_LAST_TOOLS = []
    FAKE_TOOL_CALLS.clear()


def fake_uuid_failure_llm_factory(*_: Any, **__: Any) -> FakeUuidFailureLlm:
    return FakeUuidFailureLlm()
