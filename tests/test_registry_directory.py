from __future__ import annotations

from datetime import timedelta
from typing import Any

from kafka_a2a.models import AgentCard
from kafka_a2a.registry.directory import KafkaAgentDirectory, KafkaAgentDirectoryConfig, _utc_now
from kafka_a2a.registry.kafka_registry import KafkaAgentRegistry, RegistryEntry
from kafka_a2a.transport.kafka import KafkaEnvelope


def _card(name: str) -> AgentCard:
    return AgentCard(name=name, description=f"{name} agent", url=f"kafka://{name}", version="0.1.0")


class _TransportCapture:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send(self, *, topic: str, envelope: KafkaEnvelope, key: bytes | None = None) -> None:
        self.calls.append({"topic": topic, "envelope": envelope, "key": key})


def test_registry_publish_and_unpublish_emit_records() -> None:
    transport = _TransportCapture()
    registry = KafkaAgentRegistry(transport=transport)  # type: ignore[arg-type]

    async def _run() -> None:
        await registry.publish(agent_name="product", card=_card("product"))
        await registry.unpublish(agent_name="product")

    import asyncio

    asyncio.run(_run())

    publish_payload = transport.calls[0]["envelope"].payload
    delete_payload = transport.calls[1]["envelope"].payload

    assert publish_payload["agent_name"] == "product"
    assert publish_payload["deleted"] is False
    assert publish_payload["card"]["name"] == "product"

    assert delete_payload["agent_name"] == "product"
    assert delete_payload["deleted"] is True
    assert "card" not in delete_payload


def test_directory_ignores_stale_registry_entries() -> None:
    directory = KafkaAgentDirectory(
        registry=object(),  # type: ignore[arg-type]
        config=KafkaAgentDirectoryConfig(entry_ttl_s=60.0),
    )

    old_entry = RegistryEntry(
        agent_name="echo",
        card=_card("echo"),
        published_at=_utc_now() - timedelta(hours=1),
        deleted=False,
    )
    directory._apply_registry_entry(old_entry)

    assert directory.list() == []


def test_directory_removes_agent_on_unregister() -> None:
    directory = KafkaAgentDirectory(
        registry=object(),  # type: ignore[arg-type]
        config=KafkaAgentDirectoryConfig(entry_ttl_s=300.0),
    )

    now = _utc_now()
    directory._apply_registry_entry(
        RegistryEntry(agent_name="product", card=_card("product"), published_at=now, deleted=False)
    )
    assert [card.name for card in directory.list()] == ["product"]

    directory._apply_registry_entry(
        RegistryEntry(agent_name="product", card=None, published_at=now + timedelta(seconds=1), deleted=True)
    )

    assert directory.list() == []
