from __future__ import annotations

import pytest

from kafka_a2a.errors import A2AError, A2AErrorCode
from kafka_a2a.models import Message, TextPart, Task
from kafka_a2a.protocol import METHOD_MESSAGE_SEND, METHOD_TASKS_GET, RpcRequest
from kafka_a2a.runtime.agent import Ka2aAgent, Ka2aAgentConfig
from kafka_a2a.tenancy import KA2A_PRINCIPAL_METADATA_KEY, Principal, with_principal
from kafka_a2a.transport.kafka import KafkaConfig, KafkaEnvelope, KafkaTransport


def _agent(*, tenant_isolation: bool) -> Ka2aAgent:
    transport = KafkaTransport(KafkaConfig(bootstrap_servers="localhost:9092"))
    return Ka2aAgent(
        config=Ka2aAgentConfig(agent_name="t", tenant_isolation=tenant_isolation),
        transport=transport,
    )


@pytest.mark.asyncio
async def test_tenant_isolation_requires_principal_on_message_send() -> None:
    agent = _agent(tenant_isolation=True)
    req = RpcRequest(
        id="1",
        method=METHOD_MESSAGE_SEND,
        params={"message": Message(role="user", parts=[TextPart(text="hi")]).model_dump(by_alias=True)},
    )
    env = KafkaEnvelope(type="request", sender="c", recipient="t", reply_to="reply", payload=req.model_dump())

    with pytest.raises(A2AError) as err:
        await agent._route(req, env)  # type: ignore[attr-defined]
    assert err.value.code == A2AErrorCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_tenant_isolation_stores_and_enforces_task_principal() -> None:
    agent = _agent(tenant_isolation=True)
    principal = Principal(user_id="u1", tenant_id="acme")
    metadata = with_principal({}, principal)

    msg = Message(role="user", parts=[TextPart(text="hi")])
    req = RpcRequest(
        id="1",
        method=METHOD_MESSAGE_SEND,
        params={"message": msg.model_dump(by_alias=True), "metadata": metadata},
    )
    env = KafkaEnvelope(type="request", sender="c", recipient="t", reply_to="reply", payload=req.model_dump())
    result = await agent._route(req, env)  # type: ignore[attr-defined]

    task = Task.model_validate(result)
    assert task.metadata is not None
    assert task.metadata[KA2A_PRINCIPAL_METADATA_KEY]["userId"] == "u1"

    # Same principal can fetch
    get_req = RpcRequest(id="2", method=METHOD_TASKS_GET, params={"id": task.id, "metadata": metadata})
    got = await agent._route(get_req, env)  # type: ignore[attr-defined]
    assert Task.model_validate(got).id == task.id

    # Different principal cannot fetch
    other = with_principal({}, Principal(user_id="u2", tenant_id="acme"))
    bad_req = RpcRequest(id="3", method=METHOD_TASKS_GET, params={"id": task.id, "metadata": other})
    with pytest.raises(A2AError) as err:
        await agent._route(bad_req, env)  # type: ignore[attr-defined]
    assert err.value.code == A2AErrorCode.PERMISSION_DENIED

