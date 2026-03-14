from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from kafka_a2a.tools import ToolContext, ToolExecutor, ToolSpec


FAKE_LLM_CALL_COUNT = 0
FAKE_TOOL_CALLS: list[tuple[str, dict[str, Any]]] = []


class FakeLlm:
    async def ainvoke(self, messages: list[Any]) -> Any:
        global FAKE_LLM_CALL_COUNT
        _ = messages
        FAKE_LLM_CALL_COUNT += 1
        return SimpleNamespace(content="This should not be used for delegated host requests.")


class FakeToolExecutor(ToolExecutor):
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
        ]

    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        _ = ctx
        FAKE_TOOL_CALLS.append((name, dict(arguments)))
        if name == "list_available_agents":
            return {
                "agents": [
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
                ]
            }
        if name == "delegate_to_agent":
            return {
                "selected_agent": arguments.get("agent_name") or "product",
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
        raise AssertionError(f"Unexpected tool call: {name}")


def fake_llm_factory(*args: Any, **kwargs: Any) -> Any:
    _ = args, kwargs
    return FakeLlm()


def build_fake_tool_executor() -> ToolExecutor:
    return FakeToolExecutor()


def reset_fake_components() -> None:
    global FAKE_LLM_CALL_COUNT
    FAKE_LLM_CALL_COUNT = 0
    FAKE_TOOL_CALLS.clear()
