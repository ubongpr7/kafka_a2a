from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from kafka_a2a.client import Ka2aClient, Ka2aClientConfig
from kafka_a2a.models import (
    AgentCard,
    Artifact,
    DataPart,
    Message,
    Role,
    Task,
    TaskConfiguration,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TaskArtifactUpdateEvent,
    TextPart,
)
from kafka_a2a.processors import TaskEvent, TaskProcessor
from kafka_a2a.registry.directory import KafkaAgentDirectory, KafkaAgentDirectoryConfig
from kafka_a2a.registry.kafka_registry import KafkaAgentRegistry
from kafka_a2a.settings import Ka2aSettings
from kafka_a2a.transport.kafka import KafkaConfig, KafkaTransport


def _import_path(path: str) -> Any:
    if ":" not in path:
        raise ValueError("Import path must look like 'pkg.module:attr'")
    module_name, attr = path.split(":", 1)
    mod = importlib.import_module(module_name)
    obj = getattr(mod, attr, None)
    if obj is None:
        raise ValueError(f"Import not found: {path}")
    return obj


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    value = value.strip().lower()
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off"):
        return False
    return default


def _card_summary(card: AgentCard) -> dict[str, object]:
    return {
        "name": card.name,
        "description": card.description,
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "tags": list(s.tags or []),
                "examples": list(s.examples or []),
                "inputModes": list(s.input_modes or []),
                "outputModes": list(s.output_modes or []),
            }
            for s in (card.skills or [])
        ],
    }


def _score_card(card: AgentCard, query: str) -> int:
    q = (query or "").strip().lower()
    if not q:
        return 0
    score = 0
    if card.name.lower() in q:
        score += 10
    if card.description and any(tok in card.description.lower() for tok in q.split()[:6]):
        score += 1
    for skill in card.skills or []:
        if skill.name and skill.name.lower() in q:
            score += 5
        for tag in skill.tags or []:
            if tag.lower() in q:
                score += 2
    return score


_ROUTER_INTROSPECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(list|show|display)\b.*\bagents?\b", re.IGNORECASE),
    re.compile(r"\bwhat\b.*\bagents?\b", re.IGNORECASE),
    re.compile(r"\bavailable\b.*\bagents?\b", re.IGNORECASE),
    re.compile(r"\bregistered\b.*\bagents?\b", re.IGNORECASE),
    re.compile(r"\bagents?\b.*\b(available|registered|here|in this|in your|on this)\b", re.IGNORECASE),
    re.compile(
        r"\bagents?\b.*\b(work with|working with|do you have|can you use|can you talk to)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bagent\s*cards?\b", re.IGNORECASE),
    re.compile(r"\b(skills|capabilities)\b", re.IGNORECASE),
    re.compile(r"\bwho are you\b", re.IGNORECASE),
    re.compile(r"\bwhat can you do\b", re.IGNORECASE),
)


def _is_router_introspection_query(query: str) -> bool:
    q = (query or "").strip()
    if not q:
        return False
    if q.strip().lower() in {"help", "agents", "list agents"}:
        return True
    return any(p.search(q) for p in _ROUTER_INTROSPECTION_PATTERNS)


@dataclass(slots=True)
class _RouterState:
    started: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    client: Ka2aClient | None = None
    directory: KafkaAgentDirectory | None = None


@dataclass(slots=True)
class _LlmMsg:
    type: str
    content: Any


def make_router_processor_from_env() -> TaskProcessor:
    """
    Multi-agent router processor.

    The host agent:
      - watches the agent registry for cards/skills
      - selects a target agent
      - delegates via Kafka JSON-RPC and aggregates the final result
    """

    bootstrap = os.getenv("KA2A_BOOTSTRAP_SERVERS", "localhost:9092")
    agent_name = (os.getenv("KA2A_AGENT_NAME") or "host").strip()

    # Selection settings
    enable_llm_selection = _parse_bool(os.getenv("KA2A_ROUTER_LLM_SELECTION"), default=True)
    fallback_agent = (os.getenv("KA2A_ROUTER_FALLBACK_AGENT") or "").strip() or None

    directory_ttl_s = float(os.getenv("KA2A_DIRECTORY_ENTRY_TTL_S") or "300")
    directory_offset_reset = (os.getenv("KA2A_DIRECTORY_AUTO_OFFSET_RESET") or "earliest").strip().lower()
    directory_warmup_timeout_s = float(os.getenv("KA2A_DIRECTORY_WARMUP_TIMEOUT_S") or "3.0")
    directory_warmup_settle_s = float(os.getenv("KA2A_DIRECTORY_WARMUP_SETTLE_S") or "0.5")

    # LLM selection (optional): reuse the same LLM creds resolution as langgraph processor.
    settings = Ka2aSettings.from_env()
    decryptor: Callable[[Any], str] | None = None
    decryptor_path = (os.getenv("KA2A_SECRET_DECRYPTOR") or "").strip()
    if decryptor_path:
        decryptor = _import_path(decryptor_path)
        if not callable(decryptor):
            raise ValueError("KA2A_SECRET_DECRYPTOR must be a callable import path")

    factory_override_path = (os.getenv("KA2A_LLM_FACTORY") or "").strip() or None
    llm_factory_override: Callable[..., Any] | None = None
    if factory_override_path:
        llm_factory_override = _import_path(factory_override_path)
        if not callable(llm_factory_override):
            raise ValueError("KA2A_LLM_FACTORY must be a callable import path")

    def _default_factory_for_provider(provider: str) -> Callable[..., Any]:
        provider_lower = (provider or "").strip().lower()
        if provider_lower in ("gemini", "google", "google_genai", "google-genai"):
            return _import_path("kafka_a2a.llms.gemini:create_chat_model")
        return _import_path("kafka_a2a.llms.openai_compat:create_chat_model")

    state = _RouterState()

    async def _list_downstream_cards() -> list[AgentCard]:
        """
        Best-effort snapshot of downstream agent cards.

        The directory watcher runs in the background and may take a short moment to
        hydrate when the host receives its first request after boot.
        """

        assert state.directory is not None

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, directory_warmup_timeout_s)

        last_names: set[str] | None = None
        settle_deadline: float | None = None
        best: list[AgentCard] = []

        while True:
            cards_now = [c for c in state.directory.list() if c.name and c.name != agent_name]
            cards_now.sort(key=lambda c: c.name)
            best = cards_now or best

            names_now = {c.name for c in cards_now}
            if names_now != (last_names or set()):
                last_names = set(names_now)
                settle_deadline = loop.time() + max(0.0, directory_warmup_settle_s)

            if names_now and settle_deadline is not None and loop.time() >= settle_deadline:
                return cards_now

            if loop.time() >= deadline:
                return best

            await asyncio.sleep(0.05)

    async def _ensure_started() -> None:
        async with state.lock:
            if state.started:
                return
            transport = KafkaTransport(
                KafkaConfig.from_env(bootstrap_servers=bootstrap, client_id=f"ka2a-router-{uuid4()}")
            )
            client = Ka2aClient(transport=transport, config=Ka2aClientConfig(client_id=os.getenv("KA2A_ROUTER_CLIENT_ID")))
            await client.start()

            registry = KafkaAgentRegistry(transport=transport, sender=client.client_id)
            directory = KafkaAgentDirectory(
                registry=registry,
                config=KafkaAgentDirectoryConfig(
                    group_id=f"ka2a.router.directory.{uuid4()}",
                    auto_offset_reset=directory_offset_reset,
                    entry_ttl_s=directory_ttl_s if directory_ttl_s > 0 else None,
                ),
            )
            await directory.start()

            state.client = client
            state.directory = directory
            state.started = True

    async def _select_agent_with_llm(*, metadata: dict[str, Any] | None, cards: list[AgentCard], query: str) -> str | None:
        creds = settings.resolve_llm_credentials(metadata=metadata, decrypt=decryptor)  # type: ignore[arg-type]
        if creds is None:
            return None
        llm_factory = llm_factory_override or _default_factory_for_provider(creds.provider)
        try:
            llm = llm_factory(creds, metadata=metadata)
        except TypeError:
            llm = llm_factory(creds)

        system = (
            "You are an agent router.\n"
            "Given a user request and a list of agent cards, select the single best agent.\n"
            'Return STRICT JSON only: {"agent": "<name>", "reason": "<short>"}.\n'
            "If none match, pick the closest general agent."
        )
        body = {
            "request": query,
            "agents": [_card_summary(c) for c in cards],
        }
        prompt = "Input:\n" + json.dumps(body, ensure_ascii=False) + "\n\nOutput JSON:"

        resp = await llm.ainvoke([_LlmMsg(type="system", content=system), _LlmMsg(type="user", content=prompt)])
        raw = getattr(resp, "content", None)
        if raw is None:
            return None
        if not isinstance(raw, str):
            raw = str(raw)
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
        try:
            obj = json.loads(text)
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        agent = obj.get("agent")
        if not isinstance(agent, str) or not agent.strip():
            return None
        return agent.strip()

    async def _proc(
        task: Task,
        message: Message,
        configuration: TaskConfiguration | None,
        metadata: dict[str, Any] | None,
    ) -> AsyncIterator[TaskEvent]:
        await _ensure_started()
        assert state.client is not None
        assert state.directory is not None

        user_text = "\n".join([p.text for p in message.parts if isinstance(p, TextPart)]).strip()
        cards = await _list_downstream_cards()

        if not cards:
            yield Artifact(name="result", parts=[TextPart(text="No downstream agents are registered yet.")])
            yield TaskStatus(
                state=TaskState.completed,
                message=Message(role=Role.agent, parts=[TextPart(text="No downstream agents are registered yet.")]),
            )
            return

        if _is_router_introspection_query(user_text):
            yield Artifact(
                name="router",
                parts=[
                    DataPart(
                        data={
                            "selectedAgent": agent_name,
                            "availableAgents": [c.name for c in cards],
                            "mode": "introspection",
                        }
                    )
                ],
            )
            yield Artifact(
                name="agents",
                parts=[DataPart(data={"agents": [_card_summary(c) for c in cards]})],
            )
            lines = ["I am the host router agent.", "", "Available agents:"]
            for c in cards:
                desc = (c.description or "").strip()
                if desc:
                    lines.append(f"- {c.name}: {desc}")
                else:
                    lines.append(f"- {c.name}")
            lines.append("")
            lines.append('Ask me a question normally and I will delegate to the best agent (or pass `"agent_name"` to force one).')
            text = "\n".join(lines).strip()
            yield Artifact(name="result", parts=[TextPart(text=text)])
            yield TaskStatus(
                state=TaskState.completed,
                message=Message(role=Role.agent, parts=[TextPart(text=text)], context_id=task.context_id),
            )
            return

        scored = sorted(((c, _score_card(c, user_text)) for c in cards), key=lambda x: x[1], reverse=True)
        selected = scored[0][0].name if scored and scored[0][1] > 0 else None

        if selected is None and enable_llm_selection:
            selected = await _select_agent_with_llm(metadata=metadata, cards=cards, query=user_text)

        if selected is None and fallback_agent is not None:
            selected = fallback_agent

        if selected is None:
            selected = cards[0].name

        yield Artifact(name="router", parts=[DataPart(data={"selectedAgent": selected, "availableAgents": [c.name for c in cards]})])

        # Delegate to the selected agent and aggregate its final result.
        delegated_task_id: str | None = None
        last_result_parts: list[Any] | None = None
        child_artifacts: dict[str, list[Any]] = {}
        last_final_message: str | None = None

        stream = await state.client.stream_message(
            agent_name=selected,
            message=Message(role=Role.user, parts=message.parts, context_id=task.context_id),
            configuration=configuration,
            metadata=metadata,
        )
        async for ev in stream:
            if isinstance(ev, Task):
                delegated_task_id = ev.id
                continue
            if isinstance(ev, TaskArtifactUpdateEvent):
                name = (ev.artifact.name or "").strip()
                if name == "result":
                    last_result_parts = list(ev.artifact.parts or [])
                    continue
                if name:
                    child_artifacts[name] = list(ev.artifact.parts or [])
                continue
            if isinstance(ev, TaskStatusUpdateEvent) and ev.final:
                msg = ev.status.message
                if msg is not None:
                    last_final_message = "\n".join([p.text for p in msg.parts if isinstance(p, TextPart)]).strip() or None
                break

        parts: list[Any] = []
        if last_result_parts:
            parts = last_result_parts
        elif last_final_message:
            parts = [TextPart(text=last_final_message)]
        else:
            parts = [TextPart(text="(no result)")]

        if delegated_task_id:
            yield Artifact(name="delegation", parts=[DataPart(data={"agent": selected, "taskId": delegated_task_id})])
        for name, parts2 in child_artifacts.items():
            yield Artifact(name=f"{selected}.{name}", parts=parts2)
        yield Artifact(name="result", parts=parts)
        yield TaskStatus(state=TaskState.completed, message=Message(role=Role.agent, parts=parts))

    return _proc
