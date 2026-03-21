import asyncio

import pytest

from kafka_a2a.local_tools import KafkaDelegationBackend, LocalInteractionToolExecutor, _score_card
from kafka_a2a.models import AgentCard, AgentSkill
from kafka_a2a.tenancy import Principal
from kafka_a2a.tools import ToolContext


class _FakeDelegationBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None, ToolContext]] = []

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

    async def delegate(
        self,
        *,
        request: str,
        agent_name: str | None,
        delegated_task_id: str | None = None,
        ctx: ToolContext,
    ) -> dict[str, object]:
        self.calls.append((request, agent_name, delegated_task_id, ctx))
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
    assert backend.calls[0][2] is None
    assert backend.calls[0][3].principal is principal


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
    assert backend.calls[0][2] is None


@pytest.mark.asyncio
async def test_local_interaction_tool_executor_passes_delegated_task_id_for_follow_up() -> None:
    backend = _FakeDelegationBackend()
    executor = LocalInteractionToolExecutor(delegation_backend=backend)

    await executor.call_tool(
        name="delegate_to_agent",
        arguments={
            "request": '{"type":"multiple_choice_response","selected":"stock_locations"}',
            "agent_name": "onboarding",
            "delegated_task_id": "delegated-onboarding-scope",
        },
        ctx=ToolContext(),
    )

    assert backend.calls[0][0] == '{"type":"multiple_choice_response","selected":"stock_locations"}'
    assert backend.calls[0][1] == "onboarding"
    assert backend.calls[0][2] == "delegated-onboarding-scope"


def test_score_card_ignores_noisy_short_tokens() -> None:
    card = AgentCard(
        name="product",
        description="Product specialist for catalog search and pricing.",
        url="kafka://product",
        version="0.1.0",
        skills=[
            AgentSkill(
                id="product_catalog_lookup",
                name="Product Catalog Lookup",
                description="Search products by name, SKU, or barcode.",
                tags=["product", "catalog", "search"],
            )
        ],
    )

    assert _score_card(card, "u cant tell me aboiut my staff") == 0


def test_kafka_delegation_backend_rejects_zero_confidence_selection() -> None:
    backend = KafkaDelegationBackend()
    cards = [
        AgentCard(name="product", description="Product specialist", url="kafka://product", version="0.1.0"),
        AgentCard(name="pos", description="POS specialist", url="kafka://pos", version="0.1.0"),
    ]

    with pytest.raises(RuntimeError, match="appropriate specialist agent"):
        backend._select_agent(cards=cards, request="u cant tell me aboiut my staff", agent_name=None)


def test_kafka_delegation_backend_accepts_human_friendly_agent_aliases() -> None:
    backend = KafkaDelegationBackend()
    cards = [
        AgentCard(
            name="product_discovery",
            description="Focused product specialist",
            url="kafka://product_discovery",
            version="0.1.0",
            skills=[
                AgentSkill(
                    id="product_catalog_lookup",
                    name="Product Discovery",
                    description="Search the catalog and inspect dashboard stats.",
                )
            ],
        )
    ]

    selected = backend._select_agent(cards=cards, request="show product counts", agent_name="Product Discovery")

    assert selected.name == "product_discovery"


def test_kafka_delegation_backend_uses_agent_specific_client_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KA2A_HOST_DELEGATOR_CLIENT_ID", "host-delegator")

    host_backend = KafkaDelegationBackend(agent_name="host")
    inventory_backend = KafkaDelegationBackend(agent_name="inventory")

    assert host_backend._delegator_client_id == "host-delegator"
    assert inventory_backend._delegator_client_id == "inventory-delegator"
    assert inventory_backend._reply_group_id.startswith("ka2a.client.inventory-delegator.")


def test_kafka_delegation_backend_ignores_zero_gateway_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KA2A_GATEWAY_REQUEST_TIMEOUT_S", "0")
    monkeypatch.delenv("KA2A_DELEGATION_REQUEST_TIMEOUT_S", raising=False)
    monkeypatch.delenv("KA2A_HOST_DELEGATION_REQUEST_TIMEOUT_S", raising=False)

    backend = KafkaDelegationBackend(agent_name="inventory")

    assert backend._request_timeout_s == 30.0


@pytest.mark.asyncio
async def test_kafka_delegation_backend_times_out_stuck_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = KafkaDelegationBackend(agent_name="inventory")
    backend._request_timeout_s = 0.01
    backend._state.started = True

    async def _list_registered_cards() -> list[AgentCard]:
        return [
            AgentCard(
                name="inventory_setup",
                description="Inventory setup specialist",
                url="kafka://inventory_setup",
                version="0.1.0",
            )
        ]

    class _SlowClient:
        async def stream_message(self, *, agent_name: str, message, metadata=None):
            _ = agent_name, message, metadata
            await asyncio.sleep(1)

            async def _iter():
                if False:
                    yield None

            return _iter()

    backend._list_registered_cards = _list_registered_cards  # type: ignore[method-assign]
    backend._state.client = _SlowClient()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="Timed out waiting for delegated response from 'inventory_setup'"):
        await backend.delegate(
            request="create an inventory",
            agent_name="inventory_setup",
            ctx=ToolContext(),
        )
