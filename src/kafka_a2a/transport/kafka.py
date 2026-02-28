from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator
from uuid import uuid4

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from pydantic import BaseModel, ConfigDict, Field

from kafka_a2a.serde import dumps, loads


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(word[:1].upper() + word[1:] for word in parts[1:])


class Ka2aWireModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="allow",
    )


class EnvelopeType(str, Enum):
    request = "request"
    response = "response"
    event = "event"
    registry = "registry"


class KafkaEnvelope(Ka2aWireModel):
    """
    Transport envelope for Kafka.

    The `payload` is expected to contain protocol-level request/response/event objects.
    """

    envelope_version: str = "ka2a.v1"
    type: EnvelopeType
    message_id: str = Field(default_factory=lambda: str(uuid4()))
    correlation_id: str | int | None = None
    sender: str | None = None
    recipient: str | None = None
    reply_to: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    content_type: str = "application/json"
    payload: Any

    def to_bytes(self) -> bytes:
        return dumps(self)

    @classmethod
    def from_bytes(cls, data: bytes) -> "KafkaEnvelope":
        return cls.model_validate(loads(data))


@dataclass(slots=True)
class KafkaConfig:
    bootstrap_servers: str | list[str]
    client_id: str | None = None


@dataclass(slots=True)
class KafkaAgentRegistryConfig:
    topic: str = "ka2a.agent_cards"


@dataclass(slots=True)
class TopicNamer:
    requests_prefix: str = "ka2a.req"
    replies_prefix: str = "ka2a.reply"
    events_prefix: str = "ka2a.evt"

    def agent_requests(self, agent_name: str) -> str:
        return f"{self.requests_prefix}.{agent_name}"

    def client_replies(self, client_id: str) -> str:
        return f"{self.replies_prefix}.{client_id}"

    def task_events(self, task_id: str) -> str:
        return f"{self.events_prefix}.{task_id}"


class KafkaTransport:
    """
    Thin wrapper around aiokafka producer/consumer creation.

    This class is intentionally small; higher-level request/response semantics live
    in the client/runtime layers.
    """

    def __init__(self, config: KafkaConfig):
        self._config = config
        self._producer: AIOKafkaProducer | None = None

    @property
    def config(self) -> KafkaConfig:
        return self._config

    async def start(self) -> None:
        if self._producer is not None:
            return
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._config.bootstrap_servers,
            client_id=self._config.client_id,
        )
        await self._producer.start()

    async def stop(self) -> None:
        if self._producer is None:
            return
        await self._producer.stop()
        self._producer = None

    async def send(
        self,
        *,
        topic: str,
        envelope: KafkaEnvelope,
        key: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if self._producer is None:
            raise RuntimeError("KafkaTransport not started")
        hdrs = None
        if headers:
            hdrs = [(k, v.encode("utf-8")) for k, v in headers.items()]
        await self._producer.send_and_wait(topic, envelope.to_bytes(), key=key, headers=hdrs)

    def create_consumer(
        self,
        *,
        topics: list[str],
        group_id: str,
        auto_offset_reset: str = "latest",
        enable_auto_commit: bool = True,
    ) -> AIOKafkaConsumer:
        return AIOKafkaConsumer(
            *topics,
            bootstrap_servers=self._config.bootstrap_servers,
            client_id=self._config.client_id,
            group_id=group_id,
            auto_offset_reset=auto_offset_reset,
            enable_auto_commit=enable_auto_commit,
        )

    async def consume_envelopes(
        self,
        *,
        topics: list[str],
        group_id: str,
        auto_offset_reset: str = "latest",
        enable_auto_commit: bool = True,
        stop_event: asyncio.Event | None = None,
    ) -> AsyncIterator[KafkaEnvelope]:
        consumer = self.create_consumer(
            topics=topics,
            group_id=group_id,
            auto_offset_reset=auto_offset_reset,
            enable_auto_commit=enable_auto_commit,
        )
        await consumer.start()
        try:
            async for msg in consumer:
                yield KafkaEnvelope.from_bytes(msg.value)
                if stop_event is not None and stop_event.is_set():
                    break
        finally:
            await consumer.stop()
