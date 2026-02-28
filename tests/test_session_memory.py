from __future__ import annotations

import pytest

from kafka_a2a.memory import KA2A_CONVERSATION_HISTORY_METADATA_KEY
from kafka_a2a.models import Artifact, Message, TaskConfiguration, TextPart
from kafka_a2a.runtime.agent import Ka2aAgent, Ka2aAgentConfig
from kafka_a2a.runtime.task_store import InMemoryTaskStore
from kafka_a2a.transport.kafka import KafkaConfig, KafkaTransport


@pytest.mark.asyncio
async def test_agent_builds_conversation_history_from_context() -> None:
    store = InMemoryTaskStore()
    transport = KafkaTransport(KafkaConfig(bootstrap_servers="localhost:9092"))
    agent = Ka2aAgent(config=Ka2aAgentConfig(agent_name="t", context_history_turns=2), transport=transport, task_store=store)

    ctx = "ctx1"

    m1 = Message(role="user", parts=[TextPart(text="my name is ubong")], context_id=ctx)
    t1 = await store.create_task(initial_message=m1, context_id=ctx)
    await store.append_artifact(task_id=t1.id, artifact=Artifact(name="result", parts=[TextPart(text="nice to meet you")]))

    m2 = Message(role="user", parts=[TextPart(text="what is my name")], context_id=ctx)
    t2 = await store.create_task(initial_message=m2, context_id=ctx)
    await store.append_artifact(task_id=t2.id, artifact=Artifact(name="result", parts=[TextPart(text="your name is ubong")]))

    current_msg = Message(role="user", parts=[TextPart(text="tell me again")], context_id=ctx)
    current_task = await store.create_task(initial_message=current_msg, context_id=ctx)

    md = await agent._processor_metadata_for_request(  # type: ignore[attr-defined]
        task=current_task,
        configuration=None,
        request_metadata=None,
    )

    assert md is not None
    history = md.get(KA2A_CONVERSATION_HISTORY_METADATA_KEY)
    assert history == [
        {"role": "user", "content": "my name is ubong"},
        {"role": "assistant", "content": "nice to meet you"},
        {"role": "user", "content": "what is my name"},
        {"role": "assistant", "content": "your name is ubong"},
    ]

    md2 = await agent._processor_metadata_for_request(  # type: ignore[attr-defined]
        task=current_task,
        configuration=TaskConfiguration(history_length=1),
        request_metadata=None,
    )
    assert md2 is not None
    history2 = md2.get(KA2A_CONVERSATION_HISTORY_METADATA_KEY)
    assert history2 == [
        {"role": "user", "content": "what is my name"},
        {"role": "assistant", "content": "your name is ubong"},
    ]

