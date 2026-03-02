from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator

from kafka_a2a.models import AgentCard
from kafka_a2a.transport.kafka import KafkaAgentRegistryConfig, KafkaEnvelope, KafkaTransport


@dataclass(slots=True)
class RegistryEntry:
    agent_name: str
    card: AgentCard


class KafkaAgentRegistry:
    """
    Agent-card discovery via a Kafka topic.

    Recommended broker configuration: compacted topic (cleanup.policy=compact).
    """

    def __init__(
        self,
        *,
        transport: KafkaTransport,
        registry: KafkaAgentRegistryConfig | None = None,
        sender: str | None = None,
    ):
        self._transport = transport
        self._cfg = registry or KafkaAgentRegistryConfig.from_env()
        self._sender = sender

    @property
    def topic(self) -> str:
        return self._cfg.topic

    async def publish(self, *, agent_name: str, card: AgentCard) -> None:
        envelope = KafkaEnvelope(
            type="registry",
            sender=self._sender,
            recipient="registry",
            correlation_id=agent_name,
            payload=card.model_dump(by_alias=True, exclude_none=True),
        )
        await self._transport.send(topic=self._cfg.topic, envelope=envelope, key=agent_name.encode("utf-8"))

    async def watch(
        self,
        *,
        group_id: str,
        auto_offset_reset: str = "latest",
        stop_event: asyncio.Event | None = None,
    ) -> AsyncIterator[RegistryEntry]:
        async for env in self._transport.consume_envelopes(
            topics=[self._cfg.topic],
            group_id=group_id,
            auto_offset_reset=auto_offset_reset,
            stop_event=stop_event,
        ):
            try:
                card = AgentCard.model_validate(env.payload)
            except Exception:
                continue
            if not card.name:
                continue
            yield RegistryEntry(agent_name=card.name, card=card)
