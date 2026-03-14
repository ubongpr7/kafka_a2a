from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator

from pydantic import BaseModel, ConfigDict, Field

from kafka_a2a.models import AgentCard
from kafka_a2a.transport.kafka import KafkaAgentRegistryConfig, KafkaEnvelope, KafkaTransport


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class RegistryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_name: str
    card: AgentCard | None = None
    published_at: datetime = Field(default_factory=_utc_now)
    deleted: bool = False


@dataclass(slots=True)
class RegistryEntry:
    agent_name: str
    card: AgentCard | None
    published_at: datetime
    deleted: bool = False


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
        record = RegistryRecord(agent_name=agent_name, card=card, published_at=_utc_now(), deleted=False)
        envelope = KafkaEnvelope(
            type="registry",
            sender=self._sender,
            recipient="registry",
            correlation_id=agent_name,
            payload=record.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        await self._transport.send(topic=self._cfg.topic, envelope=envelope, key=agent_name.encode("utf-8"))

    async def unpublish(self, *, agent_name: str) -> None:
        record = RegistryRecord(agent_name=agent_name, card=None, published_at=_utc_now(), deleted=True)
        envelope = KafkaEnvelope(
            type="registry",
            sender=self._sender,
            recipient="registry",
            correlation_id=agent_name,
            payload=record.model_dump(mode="json", by_alias=True, exclude_none=True),
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
                record = RegistryRecord.model_validate(env.payload)
            except Exception:
                record = None

            if record is not None:
                agent_name = (record.agent_name or "").strip()
                if not agent_name:
                    continue
                yield RegistryEntry(
                    agent_name=agent_name,
                    card=record.card,
                    published_at=record.published_at,
                    deleted=bool(record.deleted),
                )
                continue

            try:
                card = AgentCard.model_validate(env.payload)
            except Exception:
                continue
            if not card.name:
                continue
            yield RegistryEntry(
                agent_name=card.name,
                card=card,
                published_at=getattr(env, "created_at", _utc_now()),
                deleted=False,
            )
