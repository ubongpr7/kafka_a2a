from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import os
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kafka_a2a.control_plane import ControlPlaneClient
from kafka_a2a.control_plane import ControlPlaneError
from kafka_a2a.langgraph_processor import make_langgraph_chat_processor_from_env
from kafka_a2a.local_tools import HybridDelegationBackend, KafkaDelegationBackend, LocalInteractionToolExecutor
from kafka_a2a.mcp_tools import McpServerAuthConfig, McpServerConfig, MultiMcpToolExecutor, MultiMcpToolExecutorConfig
from kafka_a2a.models import AgentCard, DataPart, Message, Role, Task, TaskArtifactUpdateEvent, TaskStatusUpdateEvent, TextPart
from kafka_a2a.registry.kafka_registry import KafkaAgentRegistry
from kafka_a2a.runtime.agent import Ka2aAgent, Ka2aAgentConfig, TaskProcessor
from kafka_a2a.transport.kafka import KafkaConfig, KafkaTransport, TopicNamer, ensure_kafka_topics

logger = logging.getLogger("kafka_a2a.shared_runtime")


def _part_to_payload(part: Any) -> dict[str, Any]:
    if isinstance(part, TextPart):
        return {"kind": "text", "text": part.text}
    if isinstance(part, DataPart):
        return {"kind": "data", "data": part.data}
    if hasattr(part, "model_dump"):
        payload = part.model_dump(by_alias=True, exclude_none=True)
        if isinstance(payload, dict):
            return payload
    return {"kind": getattr(part, "kind", "part"), "value": str(part)}


def _text_from_parts(parts: list[Any] | None) -> str:
    return "\n".join(part.text for part in (parts or []) if isinstance(part, TextPart)).strip()


def _workspace_instruction(agent_payload: dict[str, Any]) -> str:
    """Apply every workspace instruction layer to text and non-text runtimes."""
    return "\n\n".join(
        str(agent_payload.get(key) or "").strip()
        for key in ("system_instruction", "special_instruction", "assistant_instruction")
        if str(agent_payload.get(key) or "").strip()
    )


def _timestamp_to_iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _task_state_value(value: Any) -> str | None:
    raw = getattr(value, "value", value)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _card_summary(card: AgentCard) -> dict[str, Any]:
    return {
        "name": card.name,
        "description": card.description,
        "skills": [
            {
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "tags": list(skill.tags or []),
                "examples": list(skill.examples or []),
                "inputModes": list(skill.input_modes or []),
                "outputModes": list(skill.output_modes or []),
            }
            for skill in (card.skills or [])
        ],
    }


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _import_path(path: str) -> Any:
    if ":" not in path:
        raise ValueError("Import path must look like 'pkg.module:attr'")
    module_name, attr = path.split(":", 1)
    mod = importlib.import_module(module_name)
    obj = getattr(mod, attr, None)
    if obj is None:
        raise ValueError(f"Import not found: {path}")
    return obj


@contextmanager
def _env_overrides(values: dict[str, str | None]):
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _runtime_value(config: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in config:
            return config[key]
    return default


def _tool_auth_from_server(server_payload: dict[str, Any]) -> McpServerAuthConfig:
    auth_config = server_payload.get("auth_config")
    if not isinstance(auth_config, dict):
        auth_config = {}
    return McpServerAuthConfig(
        mode=str(auth_config.get("mode") or server_payload.get("auth_mode") or "none"),
        token=auth_config.get("token"),
        token_env=auth_config.get("token_env") or auth_config.get("tokenEnv"),
        header_name=auth_config.get("header_name") or auth_config.get("headerName") or "authorization",
        scheme=auth_config.get("scheme") or "Bearer",
    )


def _server_timeout_from_payload(server_payload: dict[str, Any]) -> float | None:
    raw_value = (
        server_payload.get("timeout_s")
        or server_payload.get("timeoutS")
        or server_payload.get("timeout")
    )
    if raw_value in (None, ""):
        return None
    try:
        timeout_s = float(raw_value)
    except (TypeError, ValueError):
        return None
    if timeout_s <= 0:
        return None
    return timeout_s


def _build_mcp_servers(agent_payload: dict[str, Any]) -> list[McpServerConfig]:
    grouped: dict[str, dict[str, Any]] = {}
    for binding in agent_payload.get("tool_bindings") or []:
        tool = binding.get("tool") if isinstance(binding, dict) else None
        if not isinstance(tool, dict):
            continue
        server_payload = tool.get("tool_server")
        if not isinstance(server_payload, dict):
            continue
        server_url = str(server_payload.get("server_url") or "").strip()
        remote_tool_name = str(tool.get("remote_tool_name") or "").strip()
        server_id = str(server_payload.get("server_id") or server_payload.get("name") or "").strip()
        if not server_url or not remote_tool_name or not server_id:
            continue

        entry = grouped.setdefault(
            server_id,
            {
                "id": server_id,
                "server_url": server_url,
                "tool_name_prefix": str(server_payload.get("tool_name_prefix") or "").strip() or None,
                "headers": None,
                "auth": _tool_auth_from_server(server_payload),
                "runtime_connections": deepcopy(server_payload.get("runtime_connections") or []),
                "timeout_s": _server_timeout_from_payload(server_payload),
                "tools": [],
            },
        )
        if entry.get("timeout_s") is None:
            entry["timeout_s"] = _server_timeout_from_payload(server_payload)
        entry["tools"].append(remote_tool_name)

    servers: list[McpServerConfig] = []
    for payload in grouped.values():
        servers.append(
            McpServerConfig(
                id=payload["id"],
                server_url=payload["server_url"],
                tools=payload["tools"],
                tool_name_prefix=payload["tool_name_prefix"],
                headers=payload["headers"],
                auth=payload["auth"],
                runtime_connections=payload["runtime_connections"],
                enabled=True,
                timeout_s=payload.get("timeout_s"),
            )
        )
    return servers


def _build_extra_executor(
    agent_payload: dict[str, Any],
    runtime_config: dict[str, Any],
    *,
    delegation_backend_factory=None,
):
    tool_executor_path = str(_runtime_value(runtime_config, "tool_executor", "toolExecutor", default="") or "").strip()
    if not tool_executor_path:
        return None

    public_slug = str(agent_payload.get("slug") or "").strip()
    runtime_name = str(agent_payload.get("runtime_name") or "").strip()
    profile_id = agent_payload.get("profile")
    allowed_downstream_slugs = _runtime_value(
        runtime_config,
        "allowed_downstream_slugs",
        "allowedDownstreamSlugs",
        default=[],
    )
    allowed_public_slugs = [str(item).strip() for item in (allowed_downstream_slugs or []) if str(item).strip()]

    if tool_executor_path in {
        "kafka_a2a.local_tools:build_interaction_tool_executor",
        "kafka_a2a.local_tools:build_host_tool_executor",
    }:
        delegation_backend = (
            delegation_backend_factory(
                agent_name=public_slug,
                runtime_agent_name=runtime_name,
                profile_id=profile_id,
                allowed_public_slugs=allowed_public_slugs,
            )
            if delegation_backend_factory is not None
            else KafkaDelegationBackend(
                agent_name=public_slug,
                runtime_agent_name=runtime_name,
                workspace_profile_id=profile_id,
                allowed_public_slugs=allowed_public_slugs,
            )
        )
        return LocalInteractionToolExecutor(
            agent_name=public_slug,
            delegation_backend=delegation_backend,
        )

    obj = _import_path(tool_executor_path)
    if callable(obj) and not hasattr(obj, "call_tool"):
        try:
            obj = obj(agent_name=public_slug)
        except TypeError:
            obj = obj()
    if not hasattr(obj, "list_tools") or not hasattr(obj, "call_tool"):
        raise ValueError("Configured runtime tool executor must expose list_tools() and call_tool().")
    return obj


def _build_tool_executor(agent_payload: dict[str, Any], runtime_config: dict[str, Any], *, delegation_backend_factory=None):
    mcp_servers = _build_mcp_servers(agent_payload)
    extra_executor = _build_extra_executor(
        agent_payload,
        runtime_config,
        delegation_backend_factory=delegation_backend_factory,
    )
    if not mcp_servers and extra_executor is None:
        return None

    timeout_s = float(os.getenv("KA2A_MCP_TIMEOUT_S") or "30")
    tools_cache_s = float(os.getenv("KA2A_MCP_TOOLS_CACHE_S") or "60")
    return MultiMcpToolExecutor(
        config=MultiMcpToolExecutorConfig(
            servers=mcp_servers,
            timeout_s=timeout_s,
            tools_cache_s=tools_cache_s,
            agent_name=str(agent_payload.get("slug") or "").strip() or None,
            config_path=None,
        ),
        extra_executor=extra_executor,
    )


def _build_processor(agent_payload: dict[str, Any], *, delegation_backend_factory=None) -> TaskProcessor:
    runtime_config = agent_payload.get("runtime_config")
    if not isinstance(runtime_config, dict):
        runtime_config = {}

    public_slug = str(agent_payload.get("slug") or "").strip() or None
    processor_name = str(_runtime_value(runtime_config, "processor", default="langgraph-chat") or "langgraph-chat").strip()
    tool_executor = _build_tool_executor(
        agent_payload,
        runtime_config,
        delegation_backend_factory=delegation_backend_factory,
    )
    workspace_instruction = _workspace_instruction(agent_payload)

    if processor_name in {"langgraph-chat", "langgraph_chat", "langgraph"}:
        return make_langgraph_chat_processor_from_env(
            agent_name=public_slug,
            system_prompt_override=workspace_instruction,
            tool_executor_override=tool_executor,
        )

    env_values = {
        "KA2A_AGENT_NAME": public_slug,
        "KA2A_RUNTIME_AGENT_NAME": str(agent_payload.get("runtime_name") or "").strip() or None,
        "KA2A_WORKSPACE_PROFILE_ID": str(agent_payload.get("profile") or "").strip() or None,
        "KA2A_ALLOWED_DOWNSTREAM_AGENTS": ",".join(
            str(item).strip()
            for item in (_runtime_value(runtime_config, "allowed_downstream_slugs", "allowedDownstreamSlugs", default=[]) or [])
            if str(item).strip()
        )
        or None,
        "KA2A_SYSTEM_PROMPT": workspace_instruction,
        "KA2A_SYSTEM_PROMPT_PATH": None,
        "KA2A_AGENT_SYSTEM_PROMPT": workspace_instruction,
        "KA2A_AGENT_SYSTEM_PROMPT_PATH": None,
    }
    with _env_overrides(env_values):
        if processor_name in {"router", "host-router", "router-agent", "router_agent"}:
            from kafka_a2a.router_processor import make_router_processor_from_env

            return make_router_processor_from_env()

        obj = _import_path(processor_name)
        if not callable(obj):
            raise ValueError("Configured runtime processor must be callable.")
        try:
            return obj(agent_name=public_slug)
        except TypeError:
            return obj()


@dataclass(slots=True)
class _ManagedAgent:
    fingerprint: str
    agent: Ka2aAgent


class SharedRuntimeService:
    def __init__(self, *, bootstrap_servers: str, poll_interval_s: float = 15.0) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._poll_interval_s = max(5.0, poll_interval_s)
        self._control_plane = ControlPlaneClient()
        self._managed: dict[str, _ManagedAgent] = {}
        self._last_registry_agents: list[dict[str, Any]] = []
        self._sync_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def _build_delegation_backend(
        self,
        *,
        agent_name: str,
        runtime_agent_name: str,
        profile_id: str | None,
        allowed_public_slugs: list[str],
    ) -> HybridDelegationBackend:
        return HybridDelegationBackend(
            agent_name=agent_name,
            runtime_agent_name=runtime_agent_name,
            workspace_profile_id=profile_id,
            allowed_public_slugs=allowed_public_slugs,
            is_local_agent=self._is_local_agent,
            delegate_local=self._delegate_local,
        )

    def _find_local_agent(self, agent_name: str) -> Ka2aAgent | None:
        for managed in self._managed.values():
            if managed.agent.card.name == agent_name:
                return managed.agent
        return None

    def _is_local_agent(self, agent_name: str) -> bool:
        return self._find_local_agent(agent_name) is not None

    async def _delegate_local(
        self,
        selected_card: AgentCard,
        request: str,
        delegated_task_id: str | None,
        ctx,
    ) -> dict[str, Any]:
        agent = self._find_local_agent(selected_card.name)
        if agent is None:
            raise RuntimeError(f"Selected local agent '{selected_card.name}' is not loaded in the shared runtime.")

        if delegated_task_id:
            stream = await agent.continue_task_stream_local(
                task_id=delegated_task_id,
                message=Message(role=Role.user, parts=[TextPart(text=request)]),
                metadata=ctx.metadata,
            )
        else:
            stream = await agent.stream_message_local(
                message=Message(role=Role.user, parts=[TextPart(text=request)]),
                metadata=ctx.metadata,
            )

        child_task_id: str | None = None
        result_parts_payload: list[dict[str, Any]] | None = None
        response_text: str | None = None
        artifacts: dict[str, list[dict[str, Any]]] = {}
        status_updates: list[dict[str, Any]] = []

        async for event in stream:
            if isinstance(event, Task):
                child_task_id = event.id
                status_updates.append(
                    {
                        "state": _task_state_value(event.status.state) or "submitted",
                        "timestamp": _timestamp_to_iso(getattr(event.status, "timestamp", None)),
                        "final": False,
                        "message": None,
                    }
                )
                continue
            if isinstance(event, TaskArtifactUpdateEvent):
                artifact_name = (event.artifact.name or "").strip() or "artifact"
                payload = [_part_to_payload(part) for part in (event.artifact.parts or [])]
                if artifact_name == "result":
                    result_parts_payload = payload
                else:
                    artifacts[artifact_name] = payload
                continue
            if isinstance(event, TaskStatusUpdateEvent):
                status_updates.append(
                    {
                        "state": _task_state_value(event.status.state) or "working",
                        "timestamp": _timestamp_to_iso(getattr(event.status, "timestamp", None)),
                        "final": bool(event.final),
                        "message": _text_from_parts(event.status.message.parts if event.status.message else None) or None,
                    }
                )
                if event.final:
                    response_text = _text_from_parts(event.status.message.parts if event.status.message else None) or None
                    break

        if response_text is None and result_parts_payload:
            response_text = "\n".join(
                item.get("text", "")
                for item in result_parts_payload
                if isinstance(item, dict) and item.get("kind") == "text"
            ).strip() or None

        return {
            "selected_agent": selected_card.name,
            "delegated_task_id": child_task_id,
            "response_text": response_text or "",
            "result_parts": result_parts_payload or [],
            "artifacts": artifacts,
            "status_updates": status_updates,
            "agent_card": _card_summary(selected_card),
            "transport": "local",
        }

    async def start(self) -> None:
        if not self._control_plane.enabled:
            raise RuntimeError("KA2A_CONTROL_PLANE_BASE_URL must be configured for shared runtime mode.")
        await self._wait_for_initial_reconcile()
        self._sync_task = asyncio.create_task(self._sync_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._sync_task is not None:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            self._sync_task = None
        for runtime_name in list(self._managed):
            await self._stop_agent(runtime_name)

    async def wait(self) -> None:
        await self._stop.wait()

    async def _sync_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval_s)
                    break
                except asyncio.TimeoutError:
                    try:
                        await self._reconcile()
                    except asyncio.CancelledError:
                        raise
                    except ControlPlaneError as exc:
                        logger.warning("shared runtime reconcile waiting for control plane: %s", str(exc))
                    except Exception:
                        logger.warning("shared runtime reconcile failed; will retry", exc_info=True)
        except asyncio.CancelledError:
            raise

    def _fingerprint(self, agent_payload: dict[str, Any]) -> str:
        serialized = json.dumps(agent_payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    async def _load_registry(self) -> list[dict[str, Any]]:
        try:
            payload = await asyncio.to_thread(self._control_plane.list_internal_runtime_registry)
        except ControlPlaneError as exc:
            if self._last_registry_agents:
                logger.warning(
                    "shared runtime registry refresh failed; reusing last known registry: %s",
                    str(exc),
                )
                return [dict(item) for item in self._last_registry_agents]
            raise

        agents = payload.get("agents")
        if not isinstance(agents, list):
            self._last_registry_agents = []
            return []
        filtered = [item for item in agents if isinstance(item, dict)]
        self._last_registry_agents = [dict(item) for item in filtered]
        return filtered

    async def _wait_for_initial_reconcile(self) -> None:
        delay_s = 2.0
        max_delay_s = min(self._poll_interval_s, 15.0)
        while not self._stop.is_set():
            try:
                await self._reconcile()
                return
            except asyncio.CancelledError:
                raise
            except ControlPlaneError as exc:
                logger.warning("shared runtime initial reconcile waiting for control plane: %s", str(exc))
            except Exception:
                logger.warning("shared runtime initial reconcile failed; waiting for control plane", exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay_s)
                break
            except asyncio.TimeoutError:
                delay_s = min(delay_s * 2.0, max_delay_s)
        raise RuntimeError("shared runtime stopped before initial control-plane reconcile completed.")

    async def _reconcile(self) -> None:
        agents = await self._load_registry()
        desired: dict[str, tuple[str, dict[str, Any]]] = {}
        for agent_payload in agents:
            runtime_name = str(agent_payload.get("runtime_name") or "").strip()
            if not runtime_name:
                continue
            desired[runtime_name] = (self._fingerprint(agent_payload), agent_payload)

        for runtime_name in list(self._managed):
            if runtime_name not in desired:
                await self._stop_agent(runtime_name)

        agents_to_start: list[tuple[str, str, dict[str, Any]]] = []
        for runtime_name, (fingerprint, agent_payload) in desired.items():
            current = self._managed.get(runtime_name)
            if current is not None and current.fingerprint == fingerprint:
                continue
            if current is not None:
                await self._stop_agent(runtime_name)
            agents_to_start.append((runtime_name, fingerprint, agent_payload))

        # Kafka consumer group joins take seconds. Starting workspace workers one
        # at a time leaves the active host unavailable behind unrelated agents.
        # They are independent runtime names, so registration can proceed together.
        if agents_to_start:
            await asyncio.gather(
                *(
                    self._start_agent(
                        runtime_name=runtime_name,
                        fingerprint=fingerprint,
                        agent_payload=agent_payload,
                    )
                    for runtime_name, fingerprint, agent_payload in agents_to_start
                )
            )

    async def _ensure_agent_request_topic(self, runtime_name: str) -> None:
        """Provision a workspace worker's request topic before it joins Kafka."""

        config = KafkaConfig.from_env(
            bootstrap_servers=self._bootstrap_servers,
            client_id=f"ka2a-runtime-topic-{runtime_name}",
        )
        created = await ensure_kafka_topics(
            config=config,
            topic_names=[TopicNamer.from_env().agent_requests(runtime_name)],
            partitions=int(os.getenv("KA2A_KAFKA_TOPIC_PARTITIONS") or "1"),
            replication_factor=int(os.getenv("KA2A_KAFKA_TOPIC_REPLICATION_FACTOR") or "1"),
        )
        if created:
            logger.info("provisioned workspace runtime request topic", extra={"runtime_name": runtime_name, "topics": created})

    async def _start_agent(self, *, runtime_name: str, fingerprint: str, agent_payload: dict[str, Any]) -> None:
        runtime_card_payload = agent_payload.get("runtime_card_payload")
        if not isinstance(runtime_card_payload, dict):
            raise ValueError(f"Runtime card payload is missing for agent '{runtime_name}'.")

        await self._ensure_agent_request_topic(runtime_name)

        card = AgentCard.model_validate(runtime_card_payload)
        processor = _build_processor(
            agent_payload,
            delegation_backend_factory=self._build_delegation_backend,
        )

        transport = KafkaTransport(
            KafkaConfig.from_env(
                bootstrap_servers=self._bootstrap_servers,
                client_id=f"ka2a-agent-{runtime_name}",
            )
        )
        registry = KafkaAgentRegistry(transport=transport, sender=runtime_name)

        task_store = None
        store_kind = (os.getenv("KA2A_TASK_STORE") or "memory").strip().lower()
        if store_kind == "redis":
            from kafka_a2a.runtime.redis_task_store import RedisTaskStore

            task_store = RedisTaskStore.from_env()

        cfg = Ka2aAgentConfig(
            agent_name=runtime_name,
            description=str(agent_payload.get("description") or ""),
            url=str(runtime_card_payload.get("url") or f"kafka://{runtime_name}"),
            version=str(agent_payload.get("version") or "0.1.0"),
            push_notifications=_parse_bool(
                ((agent_payload.get("capabilities") or {}) if isinstance(agent_payload.get("capabilities"), dict) else {}).get("pushNotifications"),
                default=False,
            ),
            push_delivery_timeout_s=float(os.getenv("KA2A_PUSH_DELIVERY_TIMEOUT_S") or "5.0"),
            push_queue_maxsize=int(os.getenv("KA2A_PUSH_QUEUE_MAXSIZE") or "1000"),
            tenant_isolation=_parse_bool(os.getenv("KA2A_TENANT_ISOLATION"), default=True),
            require_tenant_match=_parse_bool(os.getenv("KA2A_REQUIRE_TENANT_MATCH"), default=True),
            principal_metadata_key=os.getenv("KA2A_PRINCIPAL_METADATA_KEY") or "urn:ka2a:principal",
            store_principal_secrets=_parse_bool(os.getenv("KA2A_STORE_PRINCIPAL_SECRETS"), default=False),
            registry_heartbeat_s=float(os.getenv("KA2A_REGISTRY_HEARTBEAT_S") or "60"),
            request_group_id=f"ka2a.agent.{runtime_name}",
            max_concurrency=int(os.getenv("KA2A_MAX_CONCURRENCY") or "50"),
            context_history_turns=int(os.getenv("KA2A_CONTEXT_HISTORY_TURNS") or "20"),
        )

        agent = Ka2aAgent(
            config=cfg,
            transport=transport,
            registry=registry,
            processor=processor,
            card=card,
            task_store=task_store,
        )
        await agent.start()
        self._managed[runtime_name] = _ManagedAgent(fingerprint=fingerprint, agent=agent)
        logger.info("started shared runtime agent", extra={"runtime_name": runtime_name})

    async def _stop_agent(self, runtime_name: str) -> None:
        managed = self._managed.pop(runtime_name, None)
        if managed is None:
            return
        await managed.agent.stop()
        logger.info("stopped shared runtime agent", extra={"runtime_name": runtime_name})
