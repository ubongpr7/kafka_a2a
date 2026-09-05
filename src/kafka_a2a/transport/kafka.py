from __future__ import annotations

import asyncio
import base64
import os
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator, Iterable
from uuid import uuid4

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from pydantic import BaseModel, ConfigDict, Field

from kafka_a2a.ops import inc_counter
from kafka_a2a.serde import dumps, loads


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(word[:1].upper() + word[1:] for word in parts[1:])


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_text_list(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple, set)):
        return None
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = _normalize_text(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out or None


def _extract_envelope_scope_fields(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    candidates: list[dict[str, Any]] = [payload]
    params = payload.get("params")
    if isinstance(params, dict):
        candidates.append(params)
        metadata = params.get("metadata")
        if isinstance(metadata, dict):
            candidates.append(metadata)

    def first_text(*keys: str) -> str | None:
        for source in candidates:
            for key in keys:
                normalized = _normalize_text(source.get(key))
                if normalized:
                    return normalized
        return None

    def first_text_list(*keys: str) -> list[str] | None:
        for source in candidates:
            for key in keys:
                normalized = _normalize_text_list(source.get(key))
                if normalized:
                    return normalized
        return None

    scope_fields: dict[str, Any] = {}
    scope_mode = first_text("scope_mode", "scopeMode", "scope")
    structural_location_id = first_text("structural_location_id", "structuralLocationId", "primary_structural_location_id")
    structural_location_ids = first_text_list("structural_location_ids", "structuralLocationIds")
    stock_location_id = first_text("stock_location_id", "stockLocationId")
    sync_key = first_text("sync_key", "syncKey", "idempotency_key", "idempotencyKey")
    entity_ids = first_text_list("entity_ids", "entityIds")

    if scope_mode:
        scope_fields["scope_mode"] = scope_mode
    if structural_location_id:
        scope_fields["structural_location_id"] = structural_location_id
    if structural_location_ids:
        scope_fields["structural_location_ids"] = structural_location_ids
    if stock_location_id:
        scope_fields["stock_location_id"] = stock_location_id
    if sync_key:
        scope_fields["sync_key"] = sync_key
    if entity_ids:
        scope_fields["entity_ids"] = entity_ids
    return scope_fields


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
    scope_mode: str | None = None
    structural_location_id: str | None = None
    structural_location_ids: list[str] | None = None
    stock_location_id: str | None = None
    sync_key: str | None = None
    entity_ids: list[str] | None = None
    payload: Any

    def scope_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if self.scope_mode:
            fields["scope_mode"] = self.scope_mode
        if self.structural_location_id:
            fields["structural_location_id"] = self.structural_location_id
        if self.structural_location_ids:
            fields["structural_location_ids"] = list(self.structural_location_ids)
        if self.stock_location_id:
            fields["stock_location_id"] = self.stock_location_id
        if self.sync_key:
            fields["sync_key"] = self.sync_key
        if self.entity_ids:
            fields["entity_ids"] = list(self.entity_ids)
        return fields

    def to_bytes(self) -> bytes:
        return dumps(self)

    @classmethod
    def from_bytes(cls, data: bytes) -> "KafkaEnvelope":
        return cls.model_validate(loads(data))


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    value = value.strip().lower()
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off"):
        return False
    return default


def _parse_int(value: str | None, *, default: int, minimum: int | None = None) -> int:
    if value is None:
        return default
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        return max(minimum, parsed)
    return parsed


@dataclass(slots=True)
class KafkaConfig:
    bootstrap_servers: str | list[str]
    client_id: str | None = None
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: str | None = None
    sasl_username: str | None = None
    sasl_password: str | None = None
    ssl_cafile: str | None = None
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    ssl_key_password: str | None = None
    ssl_check_hostname: bool = True
    ssl_insecure_skip_verify: bool = False

    @classmethod
    def from_env(
        cls,
        *,
        bootstrap_servers: str | list[str] | None = None,
        client_id: str | None = None,
        env: dict[str, str] | None = None,
    ) -> "KafkaConfig":
        env_map = env or os.environ
        bootstrap = bootstrap_servers or env_map.get("KA2A_BOOTSTRAP_SERVERS") or "localhost:9092"

        sec_proto = (env_map.get("KA2A_KAFKA_SECURITY_PROTOCOL") or "PLAINTEXT").strip()
        sasl_mech = (env_map.get("KA2A_KAFKA_SASL_MECHANISM") or "").strip() or None
        sasl_user = (env_map.get("KA2A_KAFKA_SASL_USERNAME") or "").strip() or None

        sasl_pass = (env_map.get("KA2A_KAFKA_SASL_PASSWORD") or "").strip() or None
        if not sasl_pass:
            sasl_pass_env = (env_map.get("KA2A_KAFKA_SASL_PASSWORD_ENV") or "").strip() or None
            if sasl_pass_env:
                sasl_pass = (env_map.get(sasl_pass_env) or "").strip() or None

        cafile = (env_map.get("KA2A_KAFKA_SSL_CA_FILE") or env_map.get("KA2A_KAFKA_SSL_CAFILE") or "").strip() or None
        certfile = (env_map.get("KA2A_KAFKA_SSL_CERT_FILE") or env_map.get("KA2A_KAFKA_SSL_CERTFILE") or "").strip() or None
        keyfile = (env_map.get("KA2A_KAFKA_SSL_KEY_FILE") or env_map.get("KA2A_KAFKA_SSL_KEYFILE") or "").strip() or None

        key_password = (env_map.get("KA2A_KAFKA_SSL_KEY_PASSWORD") or "").strip() or None
        if not key_password:
            key_password_env = (env_map.get("KA2A_KAFKA_SSL_KEY_PASSWORD_ENV") or "").strip() or None
            if key_password_env:
                key_password = (env_map.get(key_password_env) or "").strip() or None

        check_hostname = _parse_bool(env_map.get("KA2A_KAFKA_SSL_CHECK_HOSTNAME"), default=True)
        insecure_skip_verify = _parse_bool(env_map.get("KA2A_KAFKA_SSL_INSECURE_SKIP_VERIFY"), default=False)

        return cls(
            bootstrap_servers=bootstrap,
            client_id=client_id,
            security_protocol=sec_proto,
            sasl_mechanism=sasl_mech,
            sasl_username=sasl_user,
            sasl_password=sasl_pass,
            ssl_cafile=cafile,
            ssl_certfile=certfile,
            ssl_keyfile=keyfile,
            ssl_key_password=key_password,
            ssl_check_hostname=check_hostname,
            ssl_insecure_skip_verify=insecure_skip_verify,
        )

    def _ssl_context(self) -> ssl.SSLContext | None:
        proto = (self.security_protocol or "").upper()
        if "SSL" not in proto:
            return None

        ctx = ssl.create_default_context(cafile=self.ssl_cafile or None)
        if self.ssl_insecure_skip_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx.check_hostname = bool(self.ssl_check_hostname)
            ctx.verify_mode = ssl.CERT_REQUIRED

        if self.ssl_certfile and self.ssl_keyfile:
            ctx.load_cert_chain(self.ssl_certfile, self.ssl_keyfile, password=self.ssl_key_password)
        return ctx

    def aiokafka_kwargs(self) -> dict[str, Any]:
        """
        Build kwargs shared by AIOKafkaProducer/AIOKafkaConsumer.
        """

        kwargs: dict[str, Any] = {
            "bootstrap_servers": self.bootstrap_servers,
            "client_id": self.client_id,
        }

        proto = (self.security_protocol or "").strip()
        if proto:
            kwargs["security_protocol"] = proto

        if proto.upper().startswith("SASL"):
            if not self.sasl_mechanism:
                raise ValueError("SASL security_protocol requires KA2A_KAFKA_SASL_MECHANISM.")
            kwargs["sasl_mechanism"] = self.sasl_mechanism
            if self.sasl_username is not None:
                kwargs["sasl_plain_username"] = self.sasl_username
            if self.sasl_password is not None:
                kwargs["sasl_plain_password"] = self.sasl_password

        ssl_ctx = self._ssl_context()
        if ssl_ctx is not None:
            kwargs["ssl_context"] = ssl_ctx

        # Remove None values to keep aiokafka happy.
        return {k: v for k, v in kwargs.items() if v is not None}


async def ensure_kafka_topics(
    *,
    config: KafkaConfig,
    topic_names: Iterable[str],
    partitions: int = 1,
    replication_factor: int = 1,
) -> list[str]:
    """Create missing topics safely when Kafka auto-creation is disabled."""

    names = sorted({str(name).strip() for name in topic_names if str(name).strip()})
    if not names:
        return []

    try:
        from aiokafka.admin import AIOKafkaAdminClient, NewTopic
        from aiokafka.errors import TopicAlreadyExistsError
    except Exception as exc:  # pragma: no cover - aiokafka is a runtime dependency
        raise RuntimeError("aiokafka admin support is required to provision Kafka topics.") from exc

    admin = AIOKafkaAdminClient(**config.aiokafka_kwargs())
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        missing = [name for name in names if name not in existing]
        if not missing:
            return []
        try:
            await admin.create_topics(
                new_topics=[
                    NewTopic(
                        name=name,
                        num_partitions=max(1, int(partitions)),
                        replication_factor=max(1, int(replication_factor)),
                    )
                    for name in missing
                ],
                validate_only=False,
            )
        except TopicAlreadyExistsError:
            # Another runtime may have won the creation race.
            pass
        return missing
    finally:
        await admin.close()


@dataclass(slots=True)
class KafkaAgentRegistryConfig:
    topic: str = "ka2a.agent_cards"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "KafkaAgentRegistryConfig":
        env_map = env or os.environ
        namespace = (env_map.get("KA2A_TOPIC_NAMESPACE") or "").strip()
        topic = (env_map.get("KA2A_REGISTRY_TOPIC") or "").strip() or "ka2a.agent_cards"
        if namespace and "KA2A_REGISTRY_TOPIC" not in env_map:
            topic = f"{namespace}.{topic}"
        return cls(topic=topic)


@dataclass(slots=True)
class TopicNamer:
    requests_prefix: str = "ka2a.req"
    replies_prefix: str = "ka2a.reply"
    events_prefix: str = "ka2a.evt"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "TopicNamer":
        env_map = env or os.environ
        namespace = (env_map.get("KA2A_TOPIC_NAMESPACE") or "").strip()

        default = cls()
        req = (env_map.get("KA2A_REQUESTS_PREFIX") or "").strip() or default.requests_prefix
        rep = (env_map.get("KA2A_REPLIES_PREFIX") or "").strip() or default.replies_prefix
        evt = (env_map.get("KA2A_EVENTS_PREFIX") or "").strip() or default.events_prefix

        if namespace:
            if "KA2A_REQUESTS_PREFIX" not in env_map:
                req = f"{namespace}.{req}"
            if "KA2A_REPLIES_PREFIX" not in env_map:
                rep = f"{namespace}.{rep}"
            if "KA2A_EVENTS_PREFIX" not in env_map:
                evt = f"{namespace}.{evt}"

        return cls(requests_prefix=req, replies_prefix=rep, events_prefix=evt)

    def agent_requests(self, agent_name: str) -> str:
        return f"{self.requests_prefix}.{agent_name}"

    def client_replies(self, client_id: str) -> str:
        return f"{self.replies_prefix}.{client_id}"

    def task_events(self, task_id: str) -> str:
        return f"{self.events_prefix}.{task_id}"


@dataclass(slots=True)
class KafkaDlqConfig:
    enabled: bool = False
    topic: str = "ka2a.dlq"
    max_value_bytes: int = 16384

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "KafkaDlqConfig":
        env_map = env or os.environ
        enabled = _parse_bool(env_map.get("KA2A_DLQ_ENABLED"), default=False)

        namespace = (env_map.get("KA2A_TOPIC_NAMESPACE") or "").strip()
        topic = (env_map.get("KA2A_DLQ_TOPIC") or "").strip() or "ka2a.dlq"
        if namespace and "KA2A_DLQ_TOPIC" not in env_map:
            topic = f"{namespace}.{topic}"

        raw_max = (env_map.get("KA2A_DLQ_MAX_VALUE_BYTES") or env_map.get("KA2A_DLQ_MAX_BYTES") or "16384").strip()
        try:
            max_value_bytes = int(raw_max)
        except Exception:
            max_value_bytes = 16384
        max_value_bytes = max(0, max_value_bytes)

        return cls(enabled=enabled, topic=topic, max_value_bytes=max_value_bytes)


def _b64_trunc(data: bytes, *, max_bytes: int) -> tuple[str, bool, int]:
    original = len(data)
    if max_bytes >= 0 and original > max_bytes:
        data = data[:max_bytes]
        return base64.b64encode(data).decode("utf-8"), True, original
    return base64.b64encode(data).decode("utf-8"), False, original


class KafkaTransport:
    """
    Thin wrapper around aiokafka producer/consumer creation.

    This class is intentionally small; higher-level request/response semantics live
    in the client/runtime layers.
    """

    def __init__(self, config: KafkaConfig, *, dlq: KafkaDlqConfig | None = None):
        self._config = config
        self._dlq = dlq or KafkaDlqConfig.from_env()
        self._producer: AIOKafkaProducer | None = None

    @property
    def config(self) -> KafkaConfig:
        return self._config

    async def start(self) -> None:
        if self._producer is not None:
            return
        self._producer = AIOKafkaProducer(**self._config.aiokafka_kwargs())
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
        inc_counter("kafka_send_total")

    async def publish_dlq(
        self,
        *,
        reason: str,
        error: str,
        topic: str,
        partition: int | None = None,
        offset: int | None = None,
        timestamp_ms: int | None = None,
        key: bytes | None = None,
        headers: list[tuple[str, bytes]] | None = None,
        value: bytes | None = None,
    ) -> None:
        if not self._dlq.enabled:
            return
        if topic == self._dlq.topic:
            return
        inc_counter("dlq_publish_attempt_total")

        payload: dict[str, Any] = {
            "kind": "dlq",
            "reason": str(reason),
            "error": str(error),
            "source": {
                "topic": topic,
                "partition": partition,
                "offset": offset,
                "timestampMs": timestamp_ms,
            },
        }

        if key is not None:
            key_b64, key_trunc, key_len = _b64_trunc(key, max_bytes=min(self._dlq.max_value_bytes, len(key)))
            payload["source"]["keyB64"] = key_b64
            payload["source"]["keyTruncated"] = bool(key_trunc)
            payload["source"]["keyBytes"] = int(key_len)

        if headers:
            hdrs: dict[str, str] = {}
            for k, v in headers:
                if not isinstance(k, str) or not isinstance(v, (bytes, bytearray)):
                    continue
                hdrs[k] = base64.b64encode(bytes(v)).decode("utf-8")
            if hdrs:
                payload["source"]["headersB64"] = hdrs

        if value is not None:
            b64, truncated, original_len = _b64_trunc(value, max_bytes=self._dlq.max_value_bytes)
            payload["valueB64"] = b64
            payload["valueTruncated"] = bool(truncated)
            payload["valueBytes"] = int(original_len)

        try:
            await self.start()
            await self.send(
                topic=self._dlq.topic,
                envelope=KafkaEnvelope(
                    type=EnvelopeType.event,
                    sender=self._config.client_id,
                    recipient="dlq",
                    payload=payload,
                ),
            )
            inc_counter("dlq_published_total")
        except Exception:
            # DLQ must never crash the main processing path.
            return

    def create_consumer(
        self,
        *,
        topics: list[str],
        group_id: str,
        auto_offset_reset: str = "latest",
        enable_auto_commit: bool = True,
    ) -> AIOKafkaConsumer:
        session_timeout_ms = _parse_int(os.getenv("KA2A_KAFKA_SESSION_TIMEOUT_MS"), default=45000, minimum=6000)
        heartbeat_interval_ms = _parse_int(os.getenv("KA2A_KAFKA_HEARTBEAT_INTERVAL_MS"), default=15000, minimum=1000)
        if heartbeat_interval_ms >= session_timeout_ms:
            heartbeat_interval_ms = max(1000, session_timeout_ms // 3)
        return AIOKafkaConsumer(
            *topics,
            group_id=group_id,
            auto_offset_reset=auto_offset_reset,
            enable_auto_commit=enable_auto_commit,
            auto_commit_interval_ms=_parse_int(
                os.getenv("KA2A_KAFKA_AUTO_COMMIT_INTERVAL_MS"),
                default=30000,
                minimum=1000,
            ),
            heartbeat_interval_ms=heartbeat_interval_ms,
            session_timeout_ms=session_timeout_ms,
            max_poll_interval_ms=_parse_int(
                os.getenv("KA2A_KAFKA_MAX_POLL_INTERVAL_MS"),
                default=600000,
                minimum=session_timeout_ms,
            ),
            request_timeout_ms=_parse_int(
                os.getenv("KA2A_KAFKA_REQUEST_TIMEOUT_MS"),
                default=60000,
                minimum=session_timeout_ms,
            ),
            retry_backoff_ms=_parse_int(os.getenv("KA2A_KAFKA_RETRY_BACKOFF_MS"), default=500, minimum=100),
            **self._config.aiokafka_kwargs(),
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
                try:
                    env = KafkaEnvelope.from_bytes(msg.value)
                except Exception as exc:
                    await self.publish_dlq(
                        reason="envelope_decode_error",
                        error=str(exc),
                        topic=str(getattr(msg, "topic", "")),
                        partition=getattr(msg, "partition", None),
                        offset=getattr(msg, "offset", None),
                        timestamp_ms=getattr(msg, "timestamp", None),
                        key=getattr(msg, "key", None),
                        headers=list(getattr(msg, "headers", None) or []),
                        value=getattr(msg, "value", None),
                    )
                    continue

                yield env
                if stop_event is not None and stop_event.is_set():
                    break
        finally:
            await consumer.stop()
