from __future__ import annotations

import asyncio
import importlib
import inspect
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, Union, get_args, get_origin
from uuid import uuid4

from kafka_a2a.agent_filter import filter_agent_cards
from kafka_a2a.client import Ka2aClient, Ka2aClientConfig
from kafka_a2a.models import (
    AgentCard,
    DataPart,
    Message,
    Role,
    Task,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
    TextPart,
)
from kafka_a2a.registry.directory import KafkaAgentDirectory, KafkaAgentDirectoryConfig
from kafka_a2a.registry.kafka_registry import KafkaAgentRegistry
from kafka_a2a.tools import ToolContext, ToolExecutor, ToolSpec
from kafka_a2a.transport.kafka import KafkaConfig, KafkaTransport


UTILITY_MODULES: tuple[str, ...] = (
    "kafka_a2a.utilities.mcp.agent_confirmation",
    "kafka_a2a.utilities.mcp.advanced_interaction_tools",
    "kafka_a2a.utilities.mcp.extra_collab_tools",
    "kafka_a2a.utilities.mcp.helper_tools",
)

QUERY_TOKEN_ALLOWLIST: set[str] = {"pos", "sku", "api", "ui"}

QUERY_TOKEN_STOPWORDS: set[str] = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "be",
    "can",
    "cant",
    "could",
    "do",
    "for",
    "from",
    "get",
    "have",
    "hello",
    "help",
    "hey",
    "hi",
    "how",
    "i",
    "if",
    "in",
    "is",
    "it",
    "let",
    "like",
    "me",
    "my",
    "need",
    "of",
    "on",
    "or",
    "please",
    "show",
    "tell",
    "the",
    "to",
    "u",
    "us",
    "we",
    "what",
    "with",
    "you",
    "your",
}


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    value = value.strip().lower()
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off"):
        return False
    return default


def _parse_optional_timeout_s(*values: str | None) -> float | None:
    for value in values:
        if value is None:
            continue
        raw = value.strip()
        if not raw:
            continue
        timeout_s = float(raw)
        if timeout_s <= 0:
            return None
        return timeout_s
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


def _query_tokens(value: str, *, max_tokens: int = 12) -> list[str]:
    tokens: list[str] = []
    for token in (value or "").strip().lower().split():
        token = "".join(ch for ch in token if ch.isalnum() or ch in {"_", "-"})
        if not token or token in QUERY_TOKEN_STOPWORDS:
            continue
        if len(token) < 3 and token not in QUERY_TOKEN_ALLOWLIST:
            continue
        tokens.append(token)
        if len(tokens) >= max_tokens:
            break
    return tokens


def _score_card(card: AgentCard, query: str) -> int:
    q = (query or "").strip().lower()
    if not q:
        return 0

    tokens = _query_tokens(q)
    score = 0

    if card.name.lower() in q:
        score += 10
    if card.description:
        desc = card.description.lower()
        score += sum(1 for token in tokens if token in desc)

    for skill in card.skills or []:
        if skill.name and skill.name.lower() in q:
            score += 5
        if skill.description:
            desc = skill.description.lower()
            score += sum(1 for token in tokens if token in desc)
        for tag in skill.tags or []:
            if tag.lower() in q:
                score += 2
        for example in skill.examples or []:
            ex = example.lower()
            score += sum(1 for token in tokens if token in ex)

    return score


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


def _load_utility_functions() -> dict[str, Callable[..., Dict[str, Any]]]:
    """
    Load the real host formatting/interaction tools from `utilities/mcp`.

    A few modules contain duplicate helper names. We keep first registration wins so
    the tool surface stays stable for the frontend and prompts.
    """

    functions: dict[str, Callable[..., Dict[str, Any]]] = {}
    for module_name in UTILITY_MODULES:
        module = importlib.import_module(module_name)
        for name, obj in vars(module).items():
            if name.startswith("_") or not inspect.isfunction(obj):
                continue
            if getattr(obj, "__module__", "") != module.__name__:
                continue
            functions.setdefault(name, obj)
    return functions


LOCAL_UTILITY_FUNCTIONS = _load_utility_functions()


def _is_optional(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is Union:
        return any(arg is type(None) for arg in get_args(annotation))
    return False


def _schema_for_annotation(annotation: Any) -> dict[str, Any]:
    if annotation is inspect._empty or annotation is Any:
        return {"type": "object"}
    origin = get_origin(annotation)
    if origin is Union:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return _schema_for_annotation(args[0])
        return {"anyOf": [_schema_for_annotation(arg) for arg in args] or [{"type": "object"}]}
    if origin in (list, List):
        args = get_args(annotation)
        return {"type": "array", "items": _schema_for_annotation(args[0]) if args else {"type": "object"}}
    if origin in (dict, Dict):
        return {"type": "object"}
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    return {"type": "object"}


def _function_tool_spec(name: str, fn: Callable[..., Dict[str, Any]]) -> ToolSpec:
    signature = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in signature.parameters.values():
        properties[param.name] = _schema_for_annotation(param.annotation)
        if param.default is inspect._empty:
            required.append(param.name)
    doc = inspect.getdoc(fn) or ""
    first_line = doc.strip().splitlines()[0] if doc.strip() else name.replace("_", " ")
    return ToolSpec(
        name=name,
        description=first_line,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    )


class DelegationBackend(Protocol):
    async def list_agents(self) -> dict[str, Any]: ...

    async def delegate(
        self,
        *,
        request: str,
        agent_name: str | None,
        delegated_task_id: str | None = None,
        ctx: ToolContext,
    ) -> dict[str, Any]: ...


@dataclass(slots=True)
class _DelegationState:
    started: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    client: Ka2aClient | None = None
    directory: KafkaAgentDirectory | None = None


class KafkaDelegationBackend:
    def __init__(self) -> None:
        self._bootstrap = os.getenv("KA2A_BOOTSTRAP_SERVERS", "localhost:9092")
        self._host_agent_name = (os.getenv("KA2A_AGENT_NAME") or "host").strip()
        self._delegator_client_id = (
            os.getenv("KA2A_HOST_DELEGATOR_CLIENT_ID") or f"{self._host_agent_name}-delegator"
        ).strip()
        self._request_timeout_s = _parse_optional_timeout_s(
            os.getenv("KA2A_REQUEST_TIMEOUT_S"),
            os.getenv("KA2A_GATEWAY_REQUEST_TIMEOUT_S"),
            os.getenv("KA2A_PROXY_REQUEST_TIMEOUT_S"),
        )
        self._directory_ttl_s = float(os.getenv("KA2A_DIRECTORY_ENTRY_TTL_S") or "300")
        self._directory_offset_reset = (os.getenv("KA2A_DIRECTORY_AUTO_OFFSET_RESET") or "earliest").strip().lower()
        self._directory_warmup_timeout_s = float(os.getenv("KA2A_DIRECTORY_WARMUP_TIMEOUT_S") or "3.0")
        self._directory_warmup_settle_s = float(os.getenv("KA2A_DIRECTORY_WARMUP_SETTLE_S") or "0.5")
        self._state = _DelegationState()

    async def _ensure_started(self) -> None:
        async with self._state.lock:
            if self._state.started:
                return

            transport = KafkaTransport(
                KafkaConfig.from_env(
                    bootstrap_servers=self._bootstrap,
                    client_id=f"ka2a-delegation-{self._delegator_client_id}",
                )
            )
            client = Ka2aClient(
                transport=transport,
                config=Ka2aClientConfig(
                    client_id=self._delegator_client_id,
                    request_timeout_s=self._request_timeout_s,
                ),
            )
            await client.start()

            registry = KafkaAgentRegistry(transport=transport, sender=client.client_id)
            directory = KafkaAgentDirectory(
                registry=registry,
                config=KafkaAgentDirectoryConfig(
                    group_id=f"ka2a.delegation.directory.{uuid4()}",
                    auto_offset_reset=self._directory_offset_reset,
                    entry_ttl_s=self._directory_ttl_s if self._directory_ttl_s > 0 else None,
                ),
            )
            await directory.start()

            self._state.client = client
            self._state.directory = directory
            self._state.started = True

    async def _list_registered_cards(self) -> list[AgentCard]:
        await self._ensure_started()
        assert self._state.directory is not None

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, self._directory_warmup_timeout_s)
        last_names: set[str] | None = None
        settle_deadline: float | None = None
        best: list[AgentCard] = []

        while True:
            cards_now = [
                card
                for card in self._state.directory.list()
                if (card.name or "").strip() and card.name != self._host_agent_name
            ]
            cards_now.sort(key=lambda card: card.name)
            best = cards_now or best

            names_now = {card.name for card in cards_now}
            if names_now != (last_names or set()):
                last_names = set(names_now)
                settle_deadline = loop.time() + max(0.0, self._directory_warmup_settle_s)

            if names_now and settle_deadline is not None and loop.time() >= settle_deadline:
                return cards_now
            if loop.time() >= deadline:
                return best

            await asyncio.sleep(0.05)

    def _visible_downstream_cards(self, cards: list[AgentCard]) -> list[AgentCard]:
        visible_cards = filter_agent_cards(cards)
        visible_cards.sort(key=lambda card: card.name)
        return visible_cards

    async def _list_downstream_cards(self) -> list[AgentCard]:
        registered_cards = await self._list_registered_cards()
        return self._visible_downstream_cards(registered_cards)

    async def list_agents(self) -> dict[str, Any]:
        registered_cards = await self._list_registered_cards()
        visible_cards = self._visible_downstream_cards(registered_cards)
        visible_names = {card.name for card in visible_cards}
        hidden_cards = [card for card in registered_cards if card.name not in visible_names]
        return {
            "agents": [_card_summary(card) for card in visible_cards],
            "registered_agents": [_card_summary(card) for card in registered_cards],
            "hidden_agents": [_card_summary(card) for card in hidden_cards],
        }

    def _select_agent(self, *, cards: list[AgentCard], request: str, agent_name: str | None) -> AgentCard:
        if agent_name:
            for card in cards:
                if card.name == agent_name:
                    return card
            raise RuntimeError(f"Requested agent '{agent_name}' is not registered.")

        if not cards:
            raise RuntimeError("No downstream specialist agents are registered.")
        if len(cards) == 1:
            return cards[0]

        scored = sorted(((card, _score_card(card, request)) for card in cards), key=lambda item: item[1], reverse=True)
        selected, score = scored[0]
        if score <= 0:
            raise RuntimeError("Could not determine an appropriate specialist agent for this request.")
        return selected

    async def delegate(
        self,
        *,
        request: str,
        agent_name: str | None,
        delegated_task_id: str | None = None,
        ctx: ToolContext,
    ) -> dict[str, Any]:
        registered_cards = await self._list_registered_cards()
        visible_cards = self._visible_downstream_cards(registered_cards)
        selected = self._select_agent(cards=visible_cards, request=request, agent_name=agent_name)

        assert self._state.client is not None
        if delegated_task_id:
            stream = await self._state.client.continue_task_stream(
                agent_name=selected.name,
                task_id=delegated_task_id,
                message=Message(role=Role.user, parts=[TextPart(text=request)]),
                metadata=ctx.metadata,
            )
        else:
            stream = await self._state.client.stream_message(
                agent_name=selected.name,
                message=Message(role=Role.user, parts=[TextPart(text=request)]),
                metadata=ctx.metadata,
            )

        delegated_task_id: str | None = None
        result_parts_payload: list[dict[str, Any]] | None = None
        response_text: str | None = None
        artifacts: dict[str, list[dict[str, Any]]] = {}
        status_updates: list[dict[str, Any]] = []

        async for event in stream:
            if isinstance(event, Task):
                delegated_task_id = event.id
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
            "selected_agent": selected.name,
            "delegated_task_id": delegated_task_id,
            "response_text": response_text or "",
            "result_parts": result_parts_payload or [],
            "artifacts": artifacts,
            "status_updates": status_updates,
            "agent_card": _card_summary(selected),
        }


DELEGATION_TOOL_SPECS: dict[str, ToolSpec] = {
    "list_available_agents": ToolSpec(
        name="list_available_agents",
        description=(
            "List downstream specialist agents registered in Kafka. Use only when routing is ambiguous or "
            "the user explicitly asks which specialists are available, then summarize the result in plain text."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    ),
    "delegate_to_agent": ToolSpec(
        name="delegate_to_agent",
        description=(
            "Delegate a domain-specific task to the best downstream agent or to a named agent. "
            "Do not use for greetings, small talk, or simple capability questions."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "request": {"type": "string"},
                "agent_name": {"type": "string"},
                "delegated_task_id": {"type": "string"},
            },
            "required": ["request"],
            "additionalProperties": False,
        },
    ),
}


class LocalInteractionToolExecutor(ToolExecutor):
    def __init__(
        self,
        *,
        functions: dict[str, Callable[..., Dict[str, Any]]] | None = None,
        delegation_backend: DelegationBackend | None = None,
    ) -> None:
        self._functions = functions or LOCAL_UTILITY_FUNCTIONS
        self._delegation = delegation_backend or KafkaDelegationBackend()
        self._specs = {name: _function_tool_spec(name, fn) for name, fn in self._functions.items()}
        self._specs.update(DELEGATION_TOOL_SPECS)

    async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
        _ = ctx
        return list(self._specs.values())

    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        payload = arguments or {}

        if name == "list_available_agents":
            return await self._delegation.list_agents()

        if name == "delegate_to_agent":
            request = str(
                payload.get("request")
                or payload.get("user_query")
                or payload.get("query")
                or payload.get("prompt")
                or ""
            ).strip()
            agent_name = str(payload.get("agent_name") or payload.get("agent") or "").strip() or None
            delegated_task_id = str(payload.get("delegated_task_id") or payload.get("task_id") or "").strip() or None
            if not request:
                raise ValueError("Missing required argument 'request' for local tool 'delegate_to_agent'.")
            return await self._delegation.delegate(
                request=request,
                agent_name=agent_name,
                delegated_task_id=delegated_task_id,
                ctx=ctx,
            )

        fn = self._functions.get(name)
        if fn is None:
            raise ValueError(f"Unknown local interaction tool: {name}")

        signature = inspect.signature(fn)
        resolved: dict[str, Any] = {}

        for param in signature.parameters.values():
            if param.name in payload:
                resolved[param.name] = payload[param.name]
                continue
            if param.default is not inspect._empty:
                resolved[param.name] = param.default
                continue
            if _is_optional(param.annotation):
                resolved[param.name] = None
                continue
            raise ValueError(f"Missing required argument '{param.name}' for local tool '{name}'.")

        return fn(**resolved)


def build_interaction_tool_executor() -> ToolExecutor:
    return LocalInteractionToolExecutor()


def build_host_tool_executor() -> ToolExecutor:
    return LocalInteractionToolExecutor()
