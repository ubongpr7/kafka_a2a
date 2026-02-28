from __future__ import annotations

import importlib
import os
from collections.abc import AsyncIterator, Callable
from typing import Any, TypedDict

from kafka_a2a.memory import KA2A_CONVERSATION_HISTORY_METADATA_KEY
from kafka_a2a.models import Artifact, FilePart, Message, Role, Task, TaskConfiguration, TaskState, TaskStatus, TextPart
from kafka_a2a.processors import TaskEvent, TaskProcessor
from kafka_a2a.settings import Ka2aSettings


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


def _extract_user_text(message: Message, *, max_text_bytes: int = 8192) -> str:
    chunks: list[str] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            if part.text:
                chunks.append(part.text)
            continue
        if isinstance(part, FilePart):
            file_obj = part.file
            mime = getattr(file_obj, "mime_type", None) or "application/octet-stream"
            if hasattr(file_obj, "uri"):
                chunks.append(f"[file uri={getattr(file_obj, 'uri', '')} mime={mime}]")
                continue
            if hasattr(file_obj, "bytes"):
                # Don't inline large/binary payloads. Keep a small hint for the model.
                b64 = getattr(file_obj, "bytes", "") or ""
                chunks.append(f"[file bytes={min(len(b64), max_text_bytes)}b mime={mime}]")
            continue
        chunks.append(f"[{getattr(part, 'kind', 'part')}]")
    return "\n".join(chunks).strip()


def make_langgraph_chat_processor_from_env() -> TaskProcessor:
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

    system_prompt = (os.getenv("KA2A_SYSTEM_PROMPT") or os.getenv("KA2A_AGENT_SYSTEM_PROMPT") or "").strip()

    class _State(TypedDict):
        messages: list[Any]

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

        user_text = _extract_user_text(message)
        lc_messages: list[Any] = []
        if system_prompt:
            lc_messages.append(SystemMessage(content=system_prompt))

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

        lc_messages.append(HumanMessage(content=user_text))

        async def _call_model(state: _State) -> _State:
            resp = await llm.ainvoke(state["messages"])
            return {"messages": [*state["messages"], resp]}

        graph = StateGraph(_State)
        graph.add_node("model", _call_model)
        graph.set_entry_point("model")
        graph.add_edge("model", END)
        app = graph.compile()

        result = await app.ainvoke({"messages": lc_messages})
        out_messages = result.get("messages") or []
        response_text = ""
        if out_messages:
            last = out_messages[-1]
            response_text = getattr(last, "content", "") or ""
            if not isinstance(response_text, str):
                response_text = str(response_text)

        artifact = Artifact(name="result", parts=[TextPart(text=response_text)])
        yield artifact

        agent_msg = Message(role=Role.agent, parts=[TextPart(text=response_text)])
        yield TaskStatus(state=TaskState.completed, message=agent_msg)

    return _proc
