from __future__ import annotations

import json
import importlib
import os
from collections.abc import AsyncIterator, Callable
from typing import Any, TypedDict

from kafka_a2a.context_memory import ContextMemory, ContextMemoryStore, InMemoryContextMemoryStore, RedisContextMemoryStore
from kafka_a2a.memory import KA2A_CONVERSATION_HISTORY_METADATA_KEY
from kafka_a2a.models import (
    Artifact,
    DataPart,
    FilePart,
    FileWithBytes,
    FileWithUri,
    Message,
    Role,
    Task,
    TaskConfiguration,
    TaskState,
    TaskStatus,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from kafka_a2a.processors import TaskEvent, TaskProcessor
from kafka_a2a.prompts import resolve_system_prompt_from_env
from kafka_a2a.settings import Ka2aSettings
from kafka_a2a.tenancy import extract_principal
from kafka_a2a.tools import ToolContext, ToolExecutor, ToolSpec


def _require_lang() -> Any:
    try:
        import langgraph  # noqa: F401
        import langchain_core  # noqa: F401
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "LangGraph processor requires the `lang` extra (e.g. `uv sync --extra lang`)."
        ) from exc
    return True


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


def _render_tool_prompt_block(tools: list[ToolSpec]) -> str:
    if not tools:
        return ""
    tools_obj = [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.input_schema,
        }
        for t in tools
    ]
    return (
        "\n\nAvailable tools (JSON):\n"
        + json.dumps(tools_obj, ensure_ascii=False)
        + "\n\nTool calling rules:\n"
        + "- Use tools only when they are necessary to complete the user's request.\n"
        + "- For greetings, small talk, capability questions, or simple summaries, answer normally in plain text.\n"
        + "- Use interaction/formatting tools only when the frontend needs structured UI such as a form, selection, confirmation, wizard, or table.\n"
        + "- If you need a tool, respond with STRICT JSON only (no markdown).\n"
        + '- Output MUST be either a single object or a list of objects shaped like: {"kind":"tool-call","name":"...","arguments":{...}}.\n'
        + '- Never output bare tool names or pseudo-tool JSON such as {"kind":"list_available_agents"} or {"kind":"create_dynamic_form"}.\n'
        + "- You may call multiple tools in one response.\n"
        + "- After tool results are provided, respond normally with your final answer unless the tool itself is a deliberate frontend interaction payload.\n"
    )


def _normalize_tool_call_payload(value: Any, *, tool_names: set[str]) -> Any:
    if not tool_names:
        return value
    if isinstance(value, list):
        return [_normalize_tool_call_payload(item, tool_names=tool_names) for item in value]
    if not isinstance(value, dict):
        return value

    kind = str(value.get("kind") or "").strip()
    name = str(value.get("name") or "").strip()

    candidate_name: str | None = None
    if kind == "tool-call" and name in tool_names:
        candidate_name = name
    elif name in tool_names:
        candidate_name = name
    elif kind in tool_names:
        candidate_name = kind

    if not candidate_name:
        return value

    arguments = value.get("arguments", value.get("args", value.get("parameters", {})))
    if arguments is None:
        arguments = {}
    elif not isinstance(arguments, dict):
        arguments = {"value": arguments}

    normalized: dict[str, Any] = {
        "kind": "tool-call",
        "name": candidate_name,
        "arguments": arguments,
    }
    tool_call_id = value.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id.strip():
        normalized["tool_call_id"] = tool_call_id.strip()
    metadata = value.get("metadata")
    if isinstance(metadata, dict) and metadata:
        normalized["metadata"] = metadata
    return normalized


def _normalize_user_text(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def _is_host_introspection_query(value: str) -> bool:
    text = _normalize_user_text(value)
    if not text:
        return False

    if text in {"help", "thanks", "thank you"}:
        return True

    greeting_prefixes = (
        "hi",
        "hello",
        "hey",
        "good morning",
        "good afternoon",
        "good evening",
    )
    if any(text == greeting or text.startswith(f"{greeting} ") for greeting in greeting_prefixes):
        return True

    phrases = (
        "what agents",
        "which agents",
        "available agents",
        "how many agents",
        "list agents",
        "show agents",
        "how can you help",
        "what can you do",
        "who are you",
        "what do you do",
        "your capabilities",
    )
    return any(phrase in text for phrase in phrases)


def _coerce_agent_summaries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    agents = value.get("agents")
    if not isinstance(agents, list):
        return []
    out: list[dict[str, Any]] = []
    for item in agents:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("name"):
            out.append(item)
    return out


def _score_agent_summary(summary: dict[str, Any], query: str) -> int:
    q = _normalize_user_text(query)
    if not q:
        return 0

    tokens = [token for token in q.split()[:12] if token]
    score = 0

    name = str(summary.get("name") or "").strip().lower()
    if name and name in q:
        score += 10

    description = str(summary.get("description") or "").strip().lower()
    if description:
        score += sum(1 for token in tokens if token in description)

    for skill in summary.get("skills") or []:
        if not isinstance(skill, dict):
            continue
        skill_name = str(skill.get("name") or "").strip().lower()
        if skill_name and skill_name in q:
            score += 5
        skill_description = str(skill.get("description") or "").strip().lower()
        if skill_description:
            score += sum(1 for token in tokens if token in skill_description)
        for tag in skill.get("tags") or []:
            if isinstance(tag, str) and tag.lower() in q:
                score += 2
        for example in skill.get("examples") or []:
            if not isinstance(example, str):
                continue
            example_text = example.lower()
            score += sum(1 for token in tokens if token in example_text)

    return score


def _select_host_delegation_agent(query: str, agents: list[dict[str, Any]]) -> str | None:
    if not agents:
        return None
    if len(agents) == 1:
        return str(agents[0].get("name") or "").strip() or None

    scored = sorted(
        ((summary, _score_agent_summary(summary, query)) for summary in agents),
        key=lambda item: item[1],
        reverse=True,
    )
    if not scored or scored[0][1] <= 0:
        return None
    selected = str(scored[0][0].get("name") or "").strip()
    return selected or None


def _coerce_task_state(value: Any, *, default: TaskState = TaskState.working) -> TaskState:
    raw = str(value or "").strip()
    if not raw:
        return default
    try:
        return TaskState(raw)
    except ValueError:
        return default


def _text_from_parts(parts: list[Any]) -> str:
    return "\n".join(part.text for part in parts if isinstance(part, TextPart)).strip()


def _format_delegation_status_text(*, agent_name: str, state: TaskState, message: str | None) -> str:
    detail = (message or "").strip()
    if detail and detail.lower() not in {"working", state.value.lower()}:
        return f"{agent_name} agent: {detail}"
    if state == TaskState.submitted:
        return f"{agent_name} agent accepted the delegated task."
    if state == TaskState.working:
        return f"{agent_name} agent is processing the delegated task."
    if state == TaskState.failed:
        return f"{agent_name} agent reported an error."
    if state == TaskState.completed:
        return f"{agent_name} agent completed the delegated task."
    return f"{agent_name} agent status: {state.value}"


def _to_model_user_content(message: Message, *, max_text_bytes: int = 8192) -> str | list[dict[str, Any]]:
    """
    Convert an incoming K-A2A `Message` into a LangChain `HumanMessage.content`.

    - Pure text => string (keeps compatibility with text-only providers)
    - Text + FileParts => list of K-A2A-like part dicts (multimodal)
    """

    text_chunks: list[str] = []
    parts: list[dict[str, Any]] = []
    has_non_text = False

    for part in message.parts:
        if isinstance(part, TextPart):
            if part.text:
                text_chunks.append(part.text)
                parts.append({"kind": "text", "text": part.text})
            continue
        if isinstance(part, FilePart):
            has_non_text = True
            file_obj = part.file
            mime = getattr(file_obj, "mime_type", None) or "application/octet-stream"
            if hasattr(file_obj, "uri"):
                uri = getattr(file_obj, "uri", "") or ""
                parts.append({"kind": "file", "file": {"uri": uri, "mimeType": mime}})
                continue
            if hasattr(file_obj, "bytes"):
                b64 = getattr(file_obj, "bytes", "") or ""
                parts.append({"kind": "file", "file": {"bytes": b64, "mimeType": mime}})
                continue
            parts.append({"kind": "file", "file": {"mimeType": mime}})
            continue
        if isinstance(part, DataPart):
            has_non_text = True
            parts.append({"kind": "data", "data": part.data})
            continue
        if isinstance(part, (ToolCallPart, ToolResultPart)):
            has_non_text = True
            parts.append(part.model_dump(by_alias=True, exclude_none=True))
            continue
        has_non_text = True
        parts.append({"kind": str(getattr(part, "kind", "part"))})

    user_text = "\n".join([t for t in text_chunks if t]).strip()
    if not has_non_text:
        return user_text
    # Keep a small textual hint for non-text requests (helps providers that don't support multimodal).
    if not user_text and parts:
        hint = f"[{len(parts)} part(s)]"
        if len(hint) <= max_text_bytes:
            parts.insert(0, {"kind": "text", "text": hint})
    return parts


def _ka2a_parts_from_model_content(content: Any) -> list[Any]:
    """
    Convert a model response content into K-A2A Parts.

    Supports:
      - string -> TextPart
      - list[dict] -> text/file/data/tool parts (K-A2A-like part dicts)
    """

    if content is None:
        return [TextPart(text="")]
    if isinstance(content, str):
        return [TextPart(text=content)]
    if isinstance(content, list):
        out: list[Any] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or item.get("type") or "").strip().lower()
            if kind == "text":
                out.append(TextPart(text=str(item.get("text") or "")))
                continue
            if kind == "file":
                file_obj = item.get("file") if isinstance(item.get("file"), dict) else {}
                mime = file_obj.get("mimeType") or file_obj.get("mime_type")
                if isinstance(file_obj.get("uri"), str) and file_obj.get("uri"):
                    out.append(FilePart(file=FileWithUri(uri=file_obj["uri"], mime_type=mime)))
                    continue
                if isinstance(file_obj.get("bytes"), str) and file_obj.get("bytes"):
                    out.append(FilePart(file=FileWithBytes(bytes=file_obj["bytes"], mime_type=mime)))
                    continue
                out.append(TextPart(text=f"[file mime={mime or 'application/octet-stream'}]"))
                continue
            if kind == "image_url":
                image_url = item.get("image_url") if isinstance(item.get("image_url"), dict) else {}
                url = image_url.get("url") or item.get("url")
                if isinstance(url, str) and url:
                    out.append(FilePart(file=FileWithUri(uri=url, mime_type="image/*")))
                continue
            if kind == "data":
                data = item.get("data")
                out.append(DataPart(data=data if isinstance(data, dict) else {"value": data}))
                continue
            if kind == "tool-call":
                try:
                    out.append(ToolCallPart.model_validate(item))
                except Exception:
                    out.append(TextPart(text=json.dumps(item, ensure_ascii=False)))
                continue
            if kind == "tool-result":
                try:
                    out.append(ToolResultPart.model_validate(item))
                except Exception:
                    out.append(TextPart(text=json.dumps(item, ensure_ascii=False)))
                continue
            out.append(TextPart(text=json.dumps(item, ensure_ascii=False)))

        if out:
            return out
        return [TextPart(text=str(content))]

    return [TextPart(text=str(content))]


def make_langgraph_chat_processor_from_env(*, agent_name: str | None = None) -> TaskProcessor:
    _require_lang()

    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    # langgraph API is reasonably stable here, but keep imports local.
    from langgraph.graph import END, StateGraph  # type: ignore

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

    system_prompt = resolve_system_prompt_from_env()

    tools_enabled = _parse_bool(os.getenv("KA2A_TOOLS_ENABLED"), default=False)
    tools_source = (os.getenv("KA2A_TOOLS_SOURCE") or "").strip().lower() or "off"
    tools_max_steps = int(os.getenv("KA2A_TOOLS_MAX_STEPS") or "5")

    memory_store_kind = (os.getenv("KA2A_CONTEXT_MEMORY_STORE") or "off").strip().lower()
    memory_enable_summary = _parse_bool(os.getenv("KA2A_CONTEXT_MEMORY_SUMMARY"), default=False)
    memory_enable_profile = _parse_bool(os.getenv("KA2A_CONTEXT_MEMORY_PROFILE"), default=False)
    memory_update_every = int(os.getenv("KA2A_CONTEXT_MEMORY_UPDATE_EVERY") or "1")
    memory_history_items = int(os.getenv("KA2A_CONTEXT_MEMORY_HISTORY_ITEMS") or "12")
    memory_max_summary_chars = int(os.getenv("KA2A_CONTEXT_MEMORY_MAX_SUMMARY_CHARS") or "1200")

    memory_store: ContextMemoryStore | None = None
    if memory_store_kind in ("redis",):
        memory_store = RedisContextMemoryStore.from_env()
    elif memory_store_kind in ("memory", "mem", "inmemory", "in-memory"):
        memory_store = InMemoryContextMemoryStore()

    class _State(TypedDict):
        messages: list[Any]

    def _build_tool_executor() -> ToolExecutor | None:
        if not tools_enabled or tools_source in ("", "off", "false", "0", "none"):
            return None

        if tools_source in ("mcp", "mcp-http", "mcp_http", "mcp_http_tools"):
            from kafka_a2a.mcp_tools import MultiMcpToolExecutor

            return MultiMcpToolExecutor.from_env(agent_name=agent_name)

        override = (os.getenv("KA2A_TOOL_EXECUTOR") or "").strip()
        if override:
            obj = _import_path(override)
            if callable(obj) and not hasattr(obj, "call_tool"):
                obj = obj()
            if not hasattr(obj, "list_tools") or not hasattr(obj, "call_tool"):
                raise ValueError("KA2A_TOOL_EXECUTOR must be a ToolExecutor or a callable returning one.")
            return obj  # type: ignore[return-value]

        return None

    tool_executor = _build_tool_executor()

    def _parts_from_model_content(content: Any, *, tool_names: set[str] | None = None) -> list[Any]:
        if isinstance(content, str):
            text = content.strip()
            if text.startswith("```"):
                text = text.strip("`").strip()
            if text.startswith("{") or text.startswith("["):
                try:
                    obj = json.loads(text)
                except Exception:
                    obj = None
                if obj is not None and tool_names:
                    obj = _normalize_tool_call_payload(obj, tool_names=tool_names)
                if isinstance(obj, dict):
                    return _ka2a_parts_from_model_content([obj])
                if isinstance(obj, list):
                    return _ka2a_parts_from_model_content(obj)
        if tool_names:
            content = _normalize_tool_call_payload(content, tool_names=tool_names)
        return _ka2a_parts_from_model_content(content)

    async def _load_memory(*, context_id: str, metadata: dict[str, Any] | None) -> ContextMemory | None:
        if memory_store is None:
            return None
        principal_key = os.getenv("KA2A_PRINCIPAL_METADATA_KEY") or "urn:ka2a:principal"
        principal = extract_principal(metadata or {}, key=principal_key)
        try:
            return await memory_store.get(context_id=context_id, principal=principal)
        except Exception:
            return None

    async def _save_memory(*, context_id: str, metadata: dict[str, Any] | None, memory: ContextMemory) -> None:
        if memory_store is None:
            return
        principal_key = os.getenv("KA2A_PRINCIPAL_METADATA_KEY") or "urn:ka2a:principal"
        principal = extract_principal(metadata or {}, key=principal_key)
        try:
            await memory_store.set(context_id=context_id, principal=principal, memory=memory)
        except Exception:
            return None

    def _system_prompt_with_memory(*, base: str, memory: ContextMemory | None) -> str:
        if memory is None or (not memory.summary and not memory.profile):
            return base
        blocks: list[str] = []
        if base:
            blocks.append(base)
        if memory.summary:
            blocks.append(f"Session summary:\n{memory.summary}".strip())
        if memory.profile:
            blocks.append("Session profile (JSON):\n" + json.dumps(memory.profile, ensure_ascii=False))
        return "\n\n".join([b for b in blocks if b]).strip()

    async def _maybe_update_memory(
        *,
        llm: Any,
        context_id: str,
        metadata: dict[str, Any] | None,
        existing: ContextMemory | None,
        history: list[dict[str, Any]] | None,
        user_text: str,
        assistant_text: str,
    ) -> None:
        if memory_store is None:
            return
        if not (memory_enable_summary or memory_enable_profile):
            return
        if memory_update_every > 1:
            turns = len(history or [])
            if (turns + 1) % memory_update_every != 0:
                return

        existing_summary = (existing.summary if existing else None) or ""
        existing_profile = (existing.profile if existing and isinstance(existing.profile, dict) else {}) or {}

        convo_lines: list[str] = []
        if history:
            for item in history[-max(0, memory_history_items) :]:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "").strip().lower()
                content = item.get("content")
                if not isinstance(content, str):
                    continue
                content = content.strip()
                if not content:
                    continue
                if role in ("user", "human"):
                    convo_lines.append(f"User: {content}")
                elif role in ("assistant", "agent", "ai"):
                    convo_lines.append(f"Assistant: {content}")
        if user_text.strip():
            convo_lines.append(f"User: {user_text.strip()}")
        if assistant_text.strip():
            convo_lines.append(f"Assistant: {assistant_text.strip()}")

        sys = (
            "You are a session memory updater for a chat assistant.\n"
            "Return STRICT JSON only (no markdown) with keys:\n"
            '  - "summary": string (short, updated session summary)\n'
            '  - "profile": object (stable user facts like name, preferences)\n'
            "Do not invent facts. If unknown, omit keys.\n"
            f"Keep summary under {memory_max_summary_chars} characters."
        )
        human = (
            "Existing summary:\n"
            f"{existing_summary}\n\n"
            "Existing profile (JSON):\n"
            f"{json.dumps(existing_profile, ensure_ascii=False)}\n\n"
            "Recent conversation:\n"
            f"{chr(10).join(convo_lines)}\n\n"
            "Updated memory JSON:"
        )

        try:
            mem_msg = await llm.ainvoke([SystemMessage(content=sys), HumanMessage(content=human)])
        except Exception:
            return
        raw = getattr(mem_msg, "content", None)
        if raw is None:
            return
        if not isinstance(raw, str):
            raw = str(raw)
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
        try:
            obj = json.loads(text)
        except Exception:
            return
        if not isinstance(obj, dict):
            return

        summary = obj.get("summary")
        profile = obj.get("profile")
        new_memory = ContextMemory(
            summary=str(summary).strip() if isinstance(summary, str) and summary.strip() else None,
            profile=profile if isinstance(profile, dict) and profile else None,
        )
        await _save_memory(context_id=context_id, metadata=metadata, memory=new_memory)

    async def _proc(
        task: Task,
        message: Message,
        configuration: TaskConfiguration | None,
        metadata: dict[str, Any] | None,
    ) -> AsyncIterator[TaskEvent]:
        _ = configuration

        creds = settings.resolve_llm_credentials(metadata=metadata, decrypt=decryptor)  # type: ignore[arg-type]
        if creds is None:
            raise ValueError(
                "LLM credentials not configured. Use KA2A_LLM_PROVIDER/KA2A_LLM_API_KEY (env mode) "
                "or include an encrypted `ka2a.llm` claim and set KA2A_LLM_CREDENTIALS_SOURCE=jwt."
            )

        llm_factory = llm_factory_override or _default_factory_for_provider(creds.provider)
        try:
            llm = llm_factory(creds, metadata=metadata)
        except TypeError:
            llm = llm_factory(creds)

        user_content = _to_model_user_content(message)
        user_text_for_memory = "\n".join([part.text for part in message.parts if isinstance(part, TextPart)]).strip()
        lc_messages: list[Any] = []
        mem = await _load_memory(context_id=task.context_id, metadata=metadata)
        sys = _system_prompt_with_memory(base=system_prompt, memory=mem)
        tool_ctx = ToolContext.from_metadata(
            metadata=metadata,
            decrypt=decryptor,
            principal_metadata_key=os.getenv("KA2A_PRINCIPAL_METADATA_KEY") or "urn:ka2a:principal",
        )
        tool_specs: list[ToolSpec] = []
        if tool_executor is not None:
            try:
                tool_specs = await tool_executor.list_tools(ctx=tool_ctx)
            except Exception:
                tool_specs = []
        tool_names = {spec.name for spec in tool_specs}
        if tool_specs:
            sys = (sys or "") + _render_tool_prompt_block(tool_specs)
        if sys:
            lc_messages.append(SystemMessage(content=sys))

        history = (metadata or {}).get(KA2A_CONVERSATION_HISTORY_METADATA_KEY)
        if isinstance(history, list):
            for item in history:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "").strip().lower()
                content = item.get("content")
                if not isinstance(content, str):
                    continue
                content = content.strip()
                if not content:
                    continue
                if role in ("user", "human"):
                    lc_messages.append(HumanMessage(content=content))
                    continue
                if role in ("assistant", "agent", "ai"):
                    lc_messages.append(AIMessage(content=content))
                    continue
                if role == "system":
                    lc_messages.append(SystemMessage(content=content))

        lc_messages.append(HumanMessage(content=user_content))

        response_parts: list[Any] = []
        response_text = ""

        if (
            agent_name == "host"
            and tool_executor is not None
            and "delegate_to_agent" in tool_names
            and user_text_for_memory
            and not _is_host_introspection_query(user_text_for_memory)
        ):
            agent_summaries: list[dict[str, Any]] = []
            if "list_available_agents" in tool_names:
                try:
                    listed_agents = await tool_executor.call_tool(
                        name="list_available_agents",
                        arguments={},
                        ctx=tool_ctx,
                    )
                    agent_summaries = _coerce_agent_summaries(listed_agents)
                except Exception:
                    agent_summaries = []

            selected_agent = _select_host_delegation_agent(user_text_for_memory, agent_summaries)
            if selected_agent or len(agent_summaries) == 1 or not agent_summaries:
                if selected_agent is None and len(agent_summaries) == 1:
                    selected_agent = str(agent_summaries[0].get("name") or "").strip() or None
                delegating_text = (
                    f"Delegating this request to the {selected_agent} specialist agent."
                    if selected_agent
                    else "Delegating this request to the appropriate specialist agent."
                )
                yield TaskStatus(
                    state=TaskState.working,
                    message=Message(
                        role=Role.agent,
                        parts=[TextPart(text=delegating_text)],
                        context_id=task.context_id,
                    ),
                )

                try:
                    delegated = await tool_executor.call_tool(
                        name="delegate_to_agent",
                        arguments={
                            "request": user_text_for_memory,
                            **({"agent_name": selected_agent} if selected_agent else {}),
                        },
                        ctx=tool_ctx,
                    )
                except Exception as exc:
                    response_text = str(exc).strip() or "Delegation failed."
                    response_parts = [TextPart(text=response_text)]
                    yield Artifact(name="result", parts=response_parts)
                    yield TaskStatus(
                        state=TaskState.failed,
                        message=Message(
                            role=Role.agent,
                            parts=response_parts,
                            context_id=task.context_id,
                        ),
                    )
                    await _maybe_update_memory(
                        llm=llm,
                        context_id=task.context_id,
                        metadata=metadata,
                        existing=mem,
                        history=history if isinstance(history, list) else None,
                        user_text=user_text_for_memory,
                        assistant_text=response_text,
                    )
                    return

                delegated_obj = delegated if isinstance(delegated, dict) else {}
                delegated_agent = str(delegated_obj.get("selected_agent") or selected_agent or "").strip() or "specialist"
                delegated_task_id = str(delegated_obj.get("delegated_task_id") or "").strip() or None
                status_updates = delegated_obj.get("status_updates") if isinstance(delegated_obj.get("status_updates"), list) else []
                delegated_final_state = TaskState.completed
                for update in reversed(status_updates):
                    if not isinstance(update, dict) or not bool(update.get("final")):
                        continue
                    delegated_final_state = _coerce_task_state(update.get("state"), default=TaskState.completed)
                    break

                yield Artifact(
                    name="delegation",
                    parts=[
                        DataPart(
                            data={
                                "selectedAgent": delegated_agent,
                                "delegatedTaskId": delegated_task_id,
                                "finalState": delegated_final_state.value,
                                "statusUpdates": status_updates,
                            }
                        )
                    ],
                )

                for update in status_updates:
                    if not isinstance(update, dict) or bool(update.get("final")):
                        continue
                    state_value = _coerce_task_state(update.get("state"), default=TaskState.working)
                    message_text = _format_delegation_status_text(
                        agent_name=delegated_agent,
                        state=state_value,
                        message=str(update.get("message") or "").strip() or None,
                    )
                    yield TaskStatus(
                        state=state_value,
                        message=Message(
                            role=Role.agent,
                            parts=[TextPart(text=message_text)],
                            context_id=task.context_id,
                        ),
                    )

                child_artifacts = delegated_obj.get("artifacts")
                if isinstance(child_artifacts, dict):
                    for artifact_name, payload in child_artifacts.items():
                        if not isinstance(artifact_name, str) or not artifact_name.strip():
                            continue
                        parts = _ka2a_parts_from_model_content(payload)
                        if parts:
                            yield Artifact(name=f"{delegated_agent}.{artifact_name}", parts=parts)

                result_payload = delegated_obj.get("result_parts")
                if isinstance(result_payload, list):
                    response_parts = _ka2a_parts_from_model_content(result_payload)
                response_text = str(delegated_obj.get("response_text") or "").strip()
                if not response_parts and response_text:
                    response_parts = [TextPart(text=response_text)]
                if not response_text and response_parts:
                    response_text = _text_from_parts(response_parts)
                if not response_parts:
                    response_parts = [TextPart(text="(no result)")]
                    response_text = "(no result)"

                yield Artifact(name="result", parts=response_parts)
                yield TaskStatus(
                    state=delegated_final_state,
                    message=Message(
                        role=Role.agent,
                        parts=response_parts,
                        context_id=task.context_id,
                    ),
                )

                await _maybe_update_memory(
                    llm=llm,
                    context_id=task.context_id,
                    metadata=metadata,
                    existing=mem,
                    history=history if isinstance(history, list) else None,
                    user_text=user_text_for_memory,
                    assistant_text=response_text,
                )
                return

        if tool_executor is None:

            async def _call_model(state: _State) -> _State:
                resp = await llm.ainvoke(state["messages"])
                return {"messages": [*state["messages"], AIMessage(content=resp.content)]}

            graph = StateGraph(_State)
            graph.add_node("model", _call_model)
            graph.set_entry_point("model")
            graph.add_edge("model", END)
            app = graph.compile()

            result = await app.ainvoke({"messages": lc_messages})
            out_messages = result.get("messages") or []
            if out_messages:
                last = out_messages[-1]
                content = getattr(last, "content", "") if hasattr(last, "content") else ""
                response_parts = _parts_from_model_content(content)
                response_text = "\n".join([p.text for p in response_parts if isinstance(p, TextPart)]).strip()
                if not response_text:
                    response_text = str(content) if not isinstance(content, str) else content

        else:
            steps = max(0, tools_max_steps)
            messages2: list[Any] = list(lc_messages)

            for _ in range(steps + 1):
                resp = await llm.ainvoke(messages2)
                messages2.append(AIMessage(content=resp.content))

                parts = _parts_from_model_content(resp.content, tool_names=tool_names)
                tool_calls = [p for p in parts if isinstance(p, ToolCallPart)]
                if not tool_calls:
                    response_parts = parts
                    response_text = "\n".join([p.text for p in response_parts if isinstance(p, TextPart)]).strip()
                    if not response_text:
                        response_text = str(resp.content) if not isinstance(resp.content, str) else resp.content
                    break

                yield Artifact(name="tool_calls", parts=tool_calls)

                tool_results: list[ToolResultPart] = []
                for call in tool_calls:
                    try:
                        output = await tool_executor.call_tool(
                            name=call.name, arguments=call.arguments, ctx=tool_ctx
                        )
                        tool_results.append(
                            ToolResultPart(tool_call_id=call.tool_call_id, output=output, is_error=False)
                        )
                    except Exception as exc:
                        tool_results.append(
                            ToolResultPart(
                                tool_call_id=call.tool_call_id,
                                output={"error": str(exc)},
                                is_error=True,
                            )
                        )

                yield Artifact(name="tool_results", parts=tool_results)
                messages2.append(
                    HumanMessage(
                        content=[p.model_dump(by_alias=True, exclude_none=True) for p in tool_results]
                    )
                )

            if not response_parts:
                response_parts = [TextPart(text="Tool execution limit reached.")]
                response_text = "Tool execution limit reached."

        artifact = Artifact(name="result", parts=response_parts or [TextPart(text=response_text)])
        yield artifact

        agent_msg = Message(role=Role.agent, parts=response_parts or [TextPart(text=response_text)])
        yield TaskStatus(state=TaskState.completed, message=agent_msg)

        await _maybe_update_memory(
            llm=llm,
            context_id=task.context_id,
            metadata=metadata,
            existing=mem,
            history=history if isinstance(history, list) else None,
            user_text=user_text_for_memory,
            assistant_text=response_text,
        )

    return _proc
