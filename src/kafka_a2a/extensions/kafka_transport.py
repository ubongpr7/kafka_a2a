from __future__ import annotations

from dataclasses import dataclass

from kafka_a2a.models import AgentExtension


KAFKA_TRANSPORT_EXTENSION_URI = "urn:ka2a:transport:kafka"


@dataclass(slots=True)
class KafkaTransportExtensionParams:
    bootstrap_servers: list[str]
    request_topic: str
    registry_topic: str
    replies_prefix: str
    events_prefix: str
    envelope_version: str = "ka2a.v1"


def build_kafka_transport_extension(*, params: KafkaTransportExtensionParams) -> AgentExtension:
    return AgentExtension(
        uri=KAFKA_TRANSPORT_EXTENSION_URI,
        description="Kafka transport configuration for K-A2A",
        required=True,
        params={
            "bootstrapServers": params.bootstrap_servers,
            "requestTopic": params.request_topic,
            "registryTopic": params.registry_topic,
            "repliesPrefix": params.replies_prefix,
            "eventsPrefix": params.events_prefix,
            "envelopeVersion": params.envelope_version,
        },
    )

