from __future__ import annotations

import asyncio
import os

from kafka_a2a.runtime.agent import Ka2aAgent, Ka2aAgentConfig
from kafka_a2a.transport.kafka import KafkaConfig, KafkaTransport
from kafka_a2a.registry.kafka_registry import KafkaAgentRegistry


async def main() -> None:
    bootstrap = os.getenv("KA2A_BOOTSTRAP_SERVERS", "localhost:9092")
    agent_name = os.getenv("KA2A_AGENT_NAME", "echo")

    transport = KafkaTransport(KafkaConfig(bootstrap_servers=bootstrap, client_id=f"ka2a-agent-{agent_name}"))
    registry = KafkaAgentRegistry(transport=transport, sender=agent_name)

    agent = Ka2aAgent(
        config=Ka2aAgentConfig(agent_name=agent_name, description="Echo agent (example)"),
        transport=transport,
        registry=registry,
    )

    await agent.start()
    try:
        await asyncio.Event().wait()
    finally:
        await agent.stop()


if __name__ == "__main__":
    asyncio.run(main())

