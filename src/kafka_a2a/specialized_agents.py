from __future__ import annotations

import importlib
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from kafka_a2a.credentials import (
    resolve_tavily_credentials_from_env,
    resolve_tavily_credentials_from_metadata,
)
from kafka_a2a.llms.controls import RetryConfig
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
from kafka_a2a.settings import Ka2aSettings
from kafka_a2a.tavily import TavilySearchResult, tavily_search


def _import_path(path: str) -> Any:
    if ":" not in path:
        raise ValueError("Import path must look like 'pkg.module:attr'")
    module_name, attr = path.split(":", 1)
    mod = importlib.import_module(module_name)
    obj = getattr(mod, attr, None)
    if obj is None:
        raise ValueError(f"Import not found: {path}")
    return obj


@dataclass(slots=True)
class _LlmMsg:
    type: str
    content: Any


def _to_model_user_content(message: Message, *, max_text_bytes: int = 8192) -> str | list[dict[str, Any]]:
    """
    Convert a K-A2A `Message` into a provider-friendly user content.

    - text-only => string
    - multimodal => list of K-A2A-like part dicts (text/file/data/tool)
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
    if not user_text and parts:
        hint = f"[{len(parts)} part(s)]"
        if len(hint) <= max_text_bytes:
            parts.insert(0, {"kind": "text", "text": hint})
    return parts


def _ka2a_parts_from_model_content(content: Any) -> list[Any]:
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
            if kind == "data":
                data = item.get("data")
                out.append(DataPart(data=data if isinstance(data, dict) else {"value": data}))
                continue
            if kind == "tool-call":
                try:
                    out.append(ToolCallPart.model_validate(item))
                except Exception:
                    out.append(TextPart(text=str(item)))
                continue
            if kind == "tool-result":
                try:
                    out.append(ToolResultPart.model_validate(item))
                except Exception:
                    out.append(TextPart(text=str(item)))
                continue
            out.append(TextPart(text=str(item)))
        return out or [TextPart(text=str(content))]
    return [TextPart(text=str(content))]


def _format_tavily_results(results: list[TavilySearchResult]) -> str:
    lines: list[str] = []
    for idx, r in enumerate(results, 1):
        head = f"{idx}. {r.title} - {r.url}"
        if r.published_date:
            head = f"{head} ({r.published_date})"
        lines.append(head)
        if r.content:
            snippet = r.content.strip()
            if len(snippet) > 600:
                snippet = snippet[:600].rstrip() + "…"
            lines.append(snippet)
    return "\n".join(lines).strip()


def _add_history(messages: list[_LlmMsg], history: Any) -> None:
    if not isinstance(history, list):
        return
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
            messages.append(_LlmMsg(type="user", content=content))
            continue
        if role in ("assistant", "agent", "ai"):
            messages.append(_LlmMsg(type="assistant", content=content))
            continue
        if role == "system":
            messages.append(_LlmMsg(type="system", content=content))


def _make_tavily_research_agent_processor(*, system_prompt: str) -> TaskProcessor:
    settings = Ka2aSettings.from_env()
    retry_cfg = RetryConfig.from_env()

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

    tavily_env_creds = resolve_tavily_credentials_from_env()
    tavily_api_key = tavily_env_creds.api_key if tavily_env_creds is not None else None
    tavily_max_results = int(os.getenv("KA2A_TAVILY_MAX_RESULTS") or "5")
    tavily_search_depth = (os.getenv("KA2A_TAVILY_SEARCH_DEPTH") or "basic").strip()

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

        user_text = "\n".join([p.text for p in message.parts if isinstance(p, TextPart)]).strip()
        history = (metadata or {}).get(KA2A_CONVERSATION_HISTORY_METADATA_KEY)

        llm_messages: list[_LlmMsg] = [_LlmMsg(type="system", content=system_prompt)]
        _add_history(llm_messages, history)

        tavily_creds = None
        if decryptor is not None:
            try:
                tavily_creds = resolve_tavily_credentials_from_metadata(metadata=metadata, decrypt=decryptor)
            except Exception:
                tavily_creds = None
        request_tavily_api_key = tavily_creds.api_key if tavily_creds is not None else tavily_api_key

        sources: list[dict[str, Any]] = []
        search_block = ""
        if request_tavily_api_key and user_text:
            try:
                results = await tavily_search(
                    api_key=request_tavily_api_key,
                    query=user_text,
                    max_results=tavily_max_results,
                    search_depth=tavily_search_depth,
                    timeout_s=float(os.getenv("KA2A_TAVILY_TIMEOUT_S") or "30"),
                    retry=retry_cfg,
                )
                sources = [
                    {
                        "title": r.title,
                        "url": r.url,
                        "publishedDate": r.published_date,
                        "score": r.score,
                    }
                    for r in results
                ]
                search_block = _format_tavily_results(results)
            except Exception as exc:
                sources = [{"error": str(exc)}]
                search_block = ""

        user_content = _to_model_user_content(message)
        if isinstance(user_content, str):
            combined = user_content.strip()
            if search_block:
                combined = f"{combined}\n\nWeb search results:\n{search_block}"
            llm_messages.append(_LlmMsg(type="user", content=combined))
        else:
            parts = list(user_content)
            if search_block:
                parts.append({"kind": "text", "text": f"Web search results:\n{search_block}"})
            llm_messages.append(_LlmMsg(type="user", content=parts))

        resp = await llm.ainvoke(llm_messages)
        content = getattr(resp, "content", "")
        parts_out = _ka2a_parts_from_model_content(content)
        text_out = "\n".join([p.text for p in parts_out if isinstance(p, TextPart)]).strip()

        if sources:
            yield Artifact(name="sources", parts=[DataPart(data={"provider": "tavily", "results": sources})])
        yield Artifact(name="result", parts=parts_out or [TextPart(text=text_out)])
        yield TaskStatus(
            state=TaskState.completed,
            message=Message(role=Role.agent, parts=parts_out or [TextPart(text=text_out)], context_id=task.context_id),
        )

    return _proc


def make_weather_agent_processor_from_env() -> TaskProcessor:
    system = (
        "You are a Weather Agent.\n"
        "Use provided web search results when available. Always include citations (URLs) for factual claims.\n"
        "If the user did not provide a location/date, ask a clarifying question.\n"
        "Prefer concise bullet forecasts (today/next 7 days). Include units and local timezone when possible."
    )
    return _make_tavily_research_agent_processor(system_prompt=system)


def make_sports_journalist_agent_processor_from_env() -> TaskProcessor:
    system = (
        "You are a Sports Journalist Agent.\n"
        "Use provided web search results when available. Always include citations (URLs) for factual claims.\n"
        "Write in a clear, journalistic style. If the team/league/date is unclear, ask a clarifying question.\n"
        "When summarizing games, include the event date and context (standings/importance) if available."
    )
    return _make_tavily_research_agent_processor(system_prompt=system)


def make_financial_analysis_agent_processor_from_env() -> TaskProcessor:
    system = (
        "You are a Financial Analysis Agent.\n"
        "Use provided web search results when available. Always include citations (URLs) for factual claims.\n"
        "Be explicit about uncertainty and assumptions. Do not provide personalized investment advice.\n"
        "Focus on explaining drivers, risks, and scenarios. If the ticker/company/region/timeframe is unclear, ask."
    )
    return _make_tavily_research_agent_processor(system_prompt=system)
