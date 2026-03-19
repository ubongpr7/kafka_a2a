import pytest

from kafka_a2a.local_tools import LocalInteractionToolExecutor
from kafka_a2a.tenancy import Principal
from kafka_a2a.tools import ToolContext


class _FakeDelegationBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, ToolContext]] = []

    async def list_agents(self) -> dict[str, object]:
        return {
            "agents": [
                {
                    "name": "product",
                    "description": "Product specialist",
                    "skills": [],
                }
            ]
        }

    async def delegate(self, *, request: str, agent_name: str | None, ctx: ToolContext) -> dict[str, object]:
        self.calls.append((request, agent_name, ctx))
        return {
            "selected_agent": agent_name or "product",
            "response_text": "delegated",
            "result_parts": [{"kind": "text", "text": "delegated"}],
            "artifacts": {},
        }


@pytest.mark.asyncio
async def test_local_interaction_tool_executor_lists_expected_tools() -> None:
    executor = LocalInteractionToolExecutor(delegation_backend=_FakeDelegationBackend())

    tools = await executor.list_tools(ctx=ToolContext())
    names = {tool.name for tool in tools}

    assert "create_confirmation_request" in names
    assert "create_searchable_selection" in names
    assert "create_wizard_flow" in names
    assert "delegate_to_agent" in names
    assert "list_available_agents" in names


@pytest.mark.asyncio
async def test_local_interaction_tool_executor_returns_interaction_payload() -> None:
    executor = LocalInteractionToolExecutor(delegation_backend=_FakeDelegationBackend())

    result = await executor.call_tool(
        name="create_confirmation_request",
        arguments={
            "title": "Delete products",
            "description": "Confirm deletion",
            "action_type": "delete",
            "details": "2 products selected",
            "approve_text": "Delete",
            "deny_text": "Cancel",
            "allow_input": True,
        },
        ctx=ToolContext(),
    )

    assert result["interaction_type"] == "confirmation_request"
    assert result["approve_text"] == "Delete"


@pytest.mark.asyncio
async def test_local_interaction_tool_executor_delegates_to_specialist_agent() -> None:
    backend = _FakeDelegationBackend()
    principal = Principal(user_id="user-1", tenant_id="tenant-1", bearer_token="jwt-123")
    ctx = ToolContext(principal=principal, metadata={"urn:ka2a:principal": {"userId": "user-1"}})
    executor = LocalInteractionToolExecutor(delegation_backend=backend)

    result = await executor.call_tool(
        name="delegate_to_agent",
        arguments={"request": "Find products matching printer ink", "agent_name": "product"},
        ctx=ctx,
    )

    assert result["selected_agent"] == "product"
    assert backend.calls[0][0] == "Find products matching printer ink"
    assert backend.calls[0][1] == "product"
    assert backend.calls[0][2].principal is principal


@pytest.mark.asyncio
async def test_local_interaction_tool_executor_accepts_legacy_delegate_aliases() -> None:
    backend = _FakeDelegationBackend()
    executor = LocalInteractionToolExecutor(delegation_backend=backend)

    result = await executor.call_tool(
        name="delegate_to_agent",
        arguments={
            "user_query": "How many staff members do we have in total?",
            "agent": "users",
        },
        ctx=ToolContext(),
    )

    assert result["selected_agent"] == "users"
    assert backend.calls[0][0] == "How many staff members do we have in total?"
    assert backend.calls[0][1] == "users"
