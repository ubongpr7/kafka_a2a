from __future__ import annotations

import ast
import json
import importlib
import os
import re
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
        + "- For greetings or small talk, answer normally in plain text.\n"
        + "- If the user asks what you can do, what help is available, or wants a list of options to choose from, prefer an interaction tool such as create_multiple_choice.\n"
        + "- Use interaction/formatting tools only when the frontend needs structured UI such as a form, selection, confirmation, wizard, or table.\n"
        + "- If you need a tool, respond with STRICT JSON only (no markdown).\n"
        + '- Output MUST be either a single object or a list of objects shaped like: {"kind":"tool-call","name":"...","arguments":{...}}.\n'
        + '- Never output bare tool names or pseudo-tool JSON such as {"kind":"list_available_agents"} or {"kind":"create_dynamic_form"}.\n'
        + '- Never output legacy wrappers such as {"tool_code":"..."} or print(create_multiple_choice(...)) or print(delegate_to_agent(...)).\n'
        + "- You may call multiple tools in one response.\n"
        + "- After tool results are provided, respond normally with your final answer unless the tool itself is a deliberate frontend interaction payload.\n"
    )


def _extract_json_candidate_from_text(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None

    code_block_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, flags=re.IGNORECASE)
    if code_block_match:
        candidate = code_block_match.group(1).strip()
        if candidate:
            return candidate

    if raw.startswith("{") or raw.startswith("["):
        return raw
    return None


def _legacy_tool_call_from_code(tool_code: str, *, tool_names: set[str]) -> dict[str, Any] | None:
    source = (tool_code or "").strip()
    if not source:
        return None

    try:
        module = ast.parse(source, mode="exec")
    except Exception:
        return None

    if len(module.body) != 1 or not isinstance(module.body[0], ast.Expr):
        return None

    expr = module.body[0].value
    if not isinstance(expr, ast.Call):
        return None

    call = expr
    if isinstance(expr.func, ast.Name) and expr.func.id == "print" and expr.args:
        inner = expr.args[0]
        if isinstance(inner, ast.Call):
            call = inner

    if isinstance(call.func, ast.Name):
        name = call.func.id
    elif isinstance(call.func, ast.Attribute):
        name = call.func.attr
    else:
        return None

    if name not in tool_names:
        return None

    arguments: dict[str, Any] = {}
    for keyword in call.keywords:
        if not keyword.arg:
            continue
        try:
            arguments[keyword.arg] = ast.literal_eval(keyword.value)
        except Exception:
            return None

    return {
        "kind": "tool-call",
        "name": name,
        "arguments": arguments,
    }


def _normalize_tool_call_payload(value: Any, *, tool_names: set[str]) -> Any:
    if not tool_names:
        return value
    if isinstance(value, list):
        return [_normalize_tool_call_payload(item, tool_names=tool_names) for item in value]
    if not isinstance(value, dict):
        return value

    legacy_tool_code = value.get("tool_code")
    if isinstance(legacy_tool_code, str) and legacy_tool_code.strip():
        legacy_tool = _legacy_tool_call_from_code(legacy_tool_code, tool_names=tool_names)
        if legacy_tool is not None:
            return legacy_tool

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


def _query_tokens(value: str, *, max_tokens: int = 12) -> list[str]:
    tokens: list[str] = []
    for token in re.findall(r"[a-z0-9][a-z0-9_-]*", _normalize_user_text(value)):
        if token in QUERY_TOKEN_STOPWORDS:
            continue
        if len(token) < 3 and token not in QUERY_TOKEN_ALLOWLIST:
            continue
        tokens.append(token)
        if len(tokens) >= max_tokens:
            break
    return tokens


HOST_AGENT_LABELS: dict[str, str] = {
    "onboarding": "Inventory Onboarding",
    "product": "Product Management",
    "inventory": "Inventory Management",
    "pos": "Point of Sale (POS)",
    "users": "User and Workspace Management",
}


HOST_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "onboarding": (
        "onboarding",
        "onboard",
        "inventory onboarding",
        "guided setup",
        "initial setup",
        "first-time setup",
        "first time setup",
        "get started",
        "setup my inventory",
        "set up my inventory",
        "configure my inventory",
        "setup stock locations",
        "set up stock locations",
        "product onboarding",
    ),
    "product": (
        "product",
        "products",
        "catalog",
        "variant",
        "variants",
        "sku",
        "barcode",
        "price",
        "pricing",
    ),
    "inventory": (
        "inventory",
        "inventories",
        "stock",
        "warehouse",
        "location",
        "locations",
        "reservation",
        "reservations",
        "movement",
        "movements",
        "lot",
        "serial",
        "expiry",
        "reorder",
    ),
    "pos": (
        "point of sale",
        "pos",
        "cashier",
        "session",
        "sessions",
        "held cart",
        "held carts",
        "terminal",
        "terminals",
        "table",
        "tables",
        "discount",
        "discounts",
        "daily sales",
        "checkout",
    ),
    "users": (
        "staff",
        "staff member",
        "staff members",
        "employee",
        "employees",
        "invitation",
        "invitations",
        "invite",
        "invites",
        "role",
        "roles",
        "group",
        "groups",
        "permission",
        "permissions",
        "workspace",
        "company profile",
        "accessible companies",
        "company staff",
    ),
}


def _extract_json_object_from_text(text: str) -> dict[str, Any] | None:
    raw = _extract_json_candidate_from_text(text)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _interaction_response_from_text(text: str) -> dict[str, Any] | None:
    obj = _extract_json_object_from_text(text)
    if not isinstance(obj, dict):
        return None
    response_type = str(obj.get("type") or "").strip().lower()
    if response_type.endswith("_response"):
        return obj
    return None


def _last_agent_interaction_payload(task: Task) -> dict[str, Any] | None:
    for msg in reversed(task.history or []):
        if not isinstance(msg, Message) or msg.role != Role.agent:
            continue
        for part in reversed(msg.parts or []):
            if isinstance(part, DataPart):
                payload = _interaction_payload_from_obj(part.data)
                if payload is not None:
                    return payload
            if isinstance(part, TextPart):
                payload = _interaction_payload_from_text(part.text)
                if payload is not None:
                    return payload
    return None


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


def _is_host_capability_picker_query(value: str) -> bool:
    text = _normalize_user_text(value)
    if not text:
        return False

    capability_phrases = (
        "what can you do",
        "how can you help",
        "what do you do",
        "what can you help",
        "show what you can do",
        "list what you can do",
        "list your capabilities",
        "show your capabilities",
        "what help do you have",
    )
    picker_phrases = (
        "pick",
        "choose",
        "select",
        "option",
        "list",
        "menu",
        "tool representation",
        "use tool",
    )

    if any(phrase in text for phrase in capability_phrases):
        return True
    if ("what you can do" in text or "how you can help" in text) and any(phrase in text for phrase in picker_phrases):
        return True
    return False


def _is_host_capability_picker_payload(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    interaction_type = str(payload.get("interaction_type") or "").strip().lower()
    title = str(payload.get("title") or "").strip().lower()
    if interaction_type != "multiple_choice":
        return False
    return title == "choose what you need help with"


def _friendly_agent_label(name: str) -> str:
    return HOST_AGENT_LABELS.get(name, name.replace("_", " ").title())


def _available_agent_names(agent_summaries: list[dict[str, Any]] | None) -> set[str]:
    return {
        str(summary.get("name") or "").strip()
        for summary in (agent_summaries or [])
        if isinstance(summary, dict) and isinstance(summary.get("name"), str)
    }


def _host_capability_picker_arguments(
    agent_summaries: list[dict[str, Any]] | None = None,
    *,
    title: str = "Choose What You Need Help With",
    description: str = "Select the area you want help with. I can continue from your choice.",
) -> dict[str, Any]:
    available_names = _available_agent_names(agent_summaries)
    options = [
        {"value": name, "label": _friendly_agent_label(name)}
        for name in ("onboarding", "product", "inventory", "pos", "users")
        if not available_names or name in available_names
    ]
    options.append({"value": "general", "label": "General Question"})
    return {
        "title": title,
        "description": description,
        "options": options,
        "multiple": False,
        "allow_input": True,
    }


def _host_follow_up_request_for_agent(agent_name: str) -> str:
    label = _friendly_agent_label(agent_name)
    return (
        f"The user selected {label} from the host menu. "
        "Briefly explain what kinds of tasks you can help with in this domain, "
        "using a concise user-facing summary."
    )


def _selected_interaction_value(response: dict[str, Any] | None) -> str | None:
    if not isinstance(response, dict):
        return None
    selected = response.get("selected")
    if isinstance(selected, str) and selected.strip():
        return selected.strip().lower()
    if isinstance(selected, list):
        for item in selected:
            if isinstance(item, str) and item.strip():
                return item.strip().lower()
    return None


def _infer_domain_agent_name(query: str) -> str | None:
    text = _normalize_user_text(query)
    if not text:
        return None

    scored: list[tuple[str, int]] = []
    for agent_name, keywords in HOST_DOMAIN_KEYWORDS.items():
        score = sum(1 for keyword in keywords if keyword in text)
        if score > 0:
            scored.append((agent_name, score))

    if not scored:
        return None
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[0][0]


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

    tokens = _query_tokens(q)
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
    inferred_agent = _infer_domain_agent_name(query)
    available_names = _available_agent_names(agents)
    if inferred_agent:
        if not available_names or inferred_agent in available_names:
            return inferred_agent
        return None

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


def _coerce_delegated_response(
    delegated: Any,
    *,
    fallback_agent_name: str | None = None,
) -> dict[str, Any] | None:
    delegated_obj = delegated if isinstance(delegated, dict) else {}
    delegated_agent = str(delegated_obj.get("selected_agent") or fallback_agent_name or "").strip()
    if not delegated_agent:
        return None

    delegated_task_id = str(delegated_obj.get("delegated_task_id") or "").strip() or None
    status_updates = delegated_obj.get("status_updates") if isinstance(delegated_obj.get("status_updates"), list) else []
    delegated_final_state = TaskState.completed
    for update in reversed(status_updates):
        if not isinstance(update, dict) or not bool(update.get("final")):
            continue
        delegated_final_state = _coerce_task_state(update.get("state"), default=TaskState.completed)
        break

    child_artifacts = delegated_obj.get("artifacts") if isinstance(delegated_obj.get("artifacts"), dict) else {}
    result_payload = delegated_obj.get("result_parts")
    response_parts = _ka2a_parts_from_model_content(result_payload) if isinstance(result_payload, list) else []
    response_text = str(delegated_obj.get("response_text") or "").strip()
    if not response_parts and response_text:
        response_parts = [TextPart(text=response_text)]
    if not response_text and response_parts:
        response_text = _text_from_parts(response_parts)
    if not response_parts:
        response_parts = [TextPart(text="(no result)")]
        response_text = "(no result)"
    if not status_updates and _interaction_payload_from_parts(response_parts) is not None:
        delegated_final_state = TaskState.input_required

    return {
        "delegated_agent": delegated_agent,
        "delegated_task_id": delegated_task_id,
        "status_updates": status_updates,
        "delegated_final_state": delegated_final_state,
        "child_artifacts": child_artifacts,
        "response_parts": response_parts,
        "response_text": response_text,
    }


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
    if state == TaskState.input_required:
        return f"{agent_name} agent needs more information from you."
    if state == TaskState.auth_required:
        return f"{agent_name} agent requires authentication before it can continue."
    if state == TaskState.completed:
        return f"{agent_name} agent completed the delegated task."
    return f"{agent_name} agent status: {state.value}"


def _interaction_payload_from_text(text: str) -> dict[str, Any] | None:
    raw = _extract_json_candidate_from_text(text)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    return _interaction_payload_from_obj(obj)


def _interaction_payload_from_obj(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        interaction_type = str(obj.get("interaction_type") or "").strip()
        typed = str(obj.get("type") or "").strip()
        if interaction_type or typed.startswith("AGENT_"):
            return obj
        legacy_tool_code = obj.get("tool_code")
        if isinstance(legacy_tool_code, str) and legacy_tool_code.strip():
            # Mark legacy wrapped interaction responses as interactive so the task pauses
            # instead of being completed and turned into a fresh unrelated follow-up turn.
            return {
                "interaction_type": "legacy_tool_code",
                "tool_code": legacy_tool_code.strip(),
            }
    return None


def _interaction_payload_from_parts(parts: list[Any]) -> dict[str, Any] | None:
    for part in parts:
        if isinstance(part, DataPart):
            payload = _interaction_payload_from_obj(part.data)
            if payload is not None:
                return payload
            continue
        if isinstance(part, ToolResultPart) and isinstance(part.output, dict):
            payload = _interaction_payload_from_obj(part.output)
            if payload is not None:
                return payload
            continue
        if not isinstance(part, TextPart):
            continue
        payload = _interaction_payload_from_text(part.text)
        if payload is not None:
            return payload
    return None


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
    if isinstance(content, dict):
        interaction_payload = _interaction_payload_from_obj(content)
        if interaction_payload is not None:
            return [DataPart(data=interaction_payload)]
        return [TextPart(text=json.dumps(content, ensure_ascii=False))]
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
            text = _extract_json_candidate_from_text(content)
            if text is not None:
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

        history_contains_current_message = False
        if task.history:
            for msg in task.history:
                if not isinstance(msg, Message):
                    continue
                content = _to_model_user_content(msg)
                if msg.role == Role.user:
                    lc_messages.append(HumanMessage(content=content))
                elif msg.role == Role.agent:
                    if isinstance(content, str) and content.strip().lower() in {"working", "completed"}:
                        if msg.message_id == message.message_id:
                            history_contains_current_message = True
                        continue
                    lc_messages.append(AIMessage(content=content))
                if msg.message_id == message.message_id:
                    history_contains_current_message = True

        if not history_contains_current_message:
            lc_messages.append(HumanMessage(content=user_content))

        response_parts: list[Any] = []
        response_text = ""
        response_state_override: TaskState | None = None
        host_agent_summaries: list[dict[str, Any]] | None = None

        async def _load_host_agent_summaries() -> list[dict[str, Any]]:
            nonlocal host_agent_summaries
            if host_agent_summaries is not None:
                return host_agent_summaries
            if agent_name != "host" or tool_executor is None or "list_available_agents" not in tool_names:
                host_agent_summaries = []
                return host_agent_summaries
            try:
                listed_agents = await tool_executor.call_tool(
                    name="list_available_agents",
                    arguments={},
                    ctx=tool_ctx,
                )
                host_agent_summaries = _coerce_agent_summaries(listed_agents)
            except Exception:
                host_agent_summaries = []
            return host_agent_summaries

        last_interaction_payload = _last_agent_interaction_payload(task)
        interaction_response = _interaction_response_from_text(user_text_for_memory)

        if (
            agent_name == "host"
            and tool_executor is not None
            and "delegate_to_agent" in tool_names
            and interaction_response is not None
            and _is_host_capability_picker_payload(last_interaction_payload)
        ):
            selected_value = _selected_interaction_value(interaction_response)
            if selected_value == "general" or not selected_value:
                response_text = "Tell me what you need help with, and I will answer directly or route it to the right specialist."
                response_parts = [TextPart(text=response_text)]
                yield Artifact(name="result", parts=response_parts)
                yield TaskStatus(
                    state=TaskState.completed,
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

            agent_summaries = await _load_host_agent_summaries()
            available_names = _available_agent_names(agent_summaries)
            if available_names and selected_value not in available_names:
                if "create_multiple_choice" in tool_names:
                    try:
                        interaction_output = await tool_executor.call_tool(
                            name="create_multiple_choice",
                            arguments=_host_capability_picker_arguments(
                                agent_summaries,
                                description=(
                                    f"{_friendly_agent_label(selected_value)} is not currently available. "
                                    "Choose one of the areas that is available right now."
                                ),
                            ),
                            ctx=tool_ctx,
                        )
                    except Exception:
                        interaction_output = None
                    if isinstance(interaction_output, dict):
                        response_text = json.dumps(interaction_output, ensure_ascii=False)
                        response_parts = [DataPart(data=interaction_output)]
                        yield Artifact(name="result", parts=response_parts)
                        yield TaskStatus(
                            state=TaskState.input_required,
                            message=Message(
                                role=Role.agent,
                                parts=response_parts,
                                context_id=task.context_id,
                            ),
                        )
                        return

                response_text = (
                    f"{_friendly_agent_label(selected_value)} is not currently available. "
                    "Ask another question or choose a different available area."
                )
                response_parts = [TextPart(text=response_text)]
                yield Artifact(name="result", parts=response_parts)
                yield TaskStatus(
                    state=TaskState.completed,
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

            yield TaskStatus(
                state=TaskState.working,
                message=Message(
                    role=Role.agent,
                    parts=[TextPart(text=f"Delegating this request to the {selected_value} specialist agent.")],
                    context_id=task.context_id,
                ),
            )

            try:
                delegated = await tool_executor.call_tool(
                    name="delegate_to_agent",
                    arguments={
                        "request": _host_follow_up_request_for_agent(selected_value),
                        "agent_name": selected_value,
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

            delegated_response = _coerce_delegated_response(delegated, fallback_agent_name=selected_value)
            if delegated_response is None:
                response_text = "Delegation did not return a usable result."
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

            yield Artifact(
                name="delegation",
                parts=[
                    DataPart(
                        data={
                            "selectedAgent": delegated_response["delegated_agent"],
                            "delegatedTaskId": delegated_response["delegated_task_id"],
                            "finalState": delegated_response["delegated_final_state"].value,
                            "statusUpdates": delegated_response["status_updates"],
                        }
                    )
                ],
            )

            for update in delegated_response["status_updates"]:
                if not isinstance(update, dict) or bool(update.get("final")):
                    continue
                state_value = _coerce_task_state(update.get("state"), default=TaskState.working)
                message_text = _format_delegation_status_text(
                    agent_name=delegated_response["delegated_agent"],
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

            for artifact_name, payload in delegated_response["child_artifacts"].items():
                if not isinstance(artifact_name, str) or not artifact_name.strip():
                    continue
                parts = _ka2a_parts_from_model_content(payload)
                if parts:
                    yield Artifact(name=f"{delegated_response['delegated_agent']}.{artifact_name}", parts=parts)

            response_parts = delegated_response["response_parts"]
            response_text = delegated_response["response_text"]
            yield Artifact(name="result", parts=response_parts)
            yield TaskStatus(
                state=delegated_response["delegated_final_state"],
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

        if (
            agent_name == "host"
            and tool_executor is not None
            and "create_multiple_choice" in tool_names
            and user_text_for_memory
            and _is_host_capability_picker_query(user_text_for_memory)
        ):
            agent_summaries = await _load_host_agent_summaries()
            try:
                interaction_output = await tool_executor.call_tool(
                    name="create_multiple_choice",
                    arguments=_host_capability_picker_arguments(agent_summaries),
                    ctx=tool_ctx,
                )
            except Exception:
                interaction_output = None

            if isinstance(interaction_output, dict):
                response_text = json.dumps(interaction_output, ensure_ascii=False)
                response_parts = [DataPart(data=interaction_output)]
                yield Artifact(name="result", parts=response_parts)
                yield TaskStatus(
                    state=TaskState.input_required,
                    message=Message(
                        role=Role.agent,
                        parts=response_parts,
                        context_id=task.context_id,
                    ),
                )
                return

        if (
            agent_name == "host"
            and tool_executor is not None
            and "delegate_to_agent" in tool_names
            and user_text_for_memory
            and not _is_host_introspection_query(user_text_for_memory)
        ):
            agent_summaries = await _load_host_agent_summaries()
            inferred_agent = _infer_domain_agent_name(user_text_for_memory)
            available_names = _available_agent_names(agent_summaries)
            if inferred_agent and available_names and inferred_agent not in available_names:
                if "create_multiple_choice" in tool_names:
                    try:
                        interaction_output = await tool_executor.call_tool(
                            name="create_multiple_choice",
                            arguments=_host_capability_picker_arguments(
                                agent_summaries,
                                description=(
                                    f"{_friendly_agent_label(inferred_agent)} is not currently available. "
                                    "Choose one of the areas that is available right now."
                                ),
                            ),
                            ctx=tool_ctx,
                        )
                    except Exception:
                        interaction_output = None

                    if isinstance(interaction_output, dict):
                        response_text = json.dumps(interaction_output, ensure_ascii=False)
                        response_parts = [DataPart(data=interaction_output)]
                        yield Artifact(name="result", parts=response_parts)
                        yield TaskStatus(
                            state=TaskState.input_required,
                            message=Message(
                                role=Role.agent,
                                parts=response_parts,
                                context_id=task.context_id,
                            ),
                        )
                        return

                response_text = (
                    f"{_friendly_agent_label(inferred_agent)} is not currently available. "
                    "Ask another question or choose a different available area."
                )
                response_parts = [TextPart(text=response_text)]
                yield Artifact(name="result", parts=response_parts)
                yield TaskStatus(
                    state=TaskState.completed,
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

                delegated_response = _coerce_delegated_response(delegated, fallback_agent_name=selected_agent)
                if delegated_response is None:
                    response_text = "Delegation did not return a usable result."
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

                yield Artifact(
                    name="delegation",
                    parts=[
                        DataPart(
                            data={
                                "selectedAgent": delegated_response["delegated_agent"],
                                "delegatedTaskId": delegated_response["delegated_task_id"],
                                "finalState": delegated_response["delegated_final_state"].value,
                                "statusUpdates": delegated_response["status_updates"],
                            }
                        )
                    ],
                )

                for update in delegated_response["status_updates"]:
                    if not isinstance(update, dict) or bool(update.get("final")):
                        continue
                    state_value = _coerce_task_state(update.get("state"), default=TaskState.working)
                    message_text = _format_delegation_status_text(
                        agent_name=delegated_response["delegated_agent"],
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

                for artifact_name, payload in delegated_response["child_artifacts"].items():
                    if not isinstance(artifact_name, str) or not artifact_name.strip():
                        continue
                    parts = _ka2a_parts_from_model_content(payload)
                    if parts:
                        yield Artifact(name=f"{delegated_response['delegated_agent']}.{artifact_name}", parts=parts)

                response_parts = delegated_response["response_parts"]
                response_text = delegated_response["response_text"]
                yield Artifact(name="result", parts=response_parts)
                yield TaskStatus(
                    state=delegated_response["delegated_final_state"],
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
                resp = await llm.ainvoke(messages2, tools=tool_specs)
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
                tool_call_names = {call.tool_call_id: call.name for call in tool_calls}
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
                delegated_output = next(
                    (
                        result.output
                        for result in tool_results
                        if not result.is_error
                        and isinstance(result.output, dict)
                        and tool_call_names.get(result.tool_call_id) == "delegate_to_agent"
                    ),
                    None,
                )
                delegated_response = _coerce_delegated_response(delegated_output)
                if delegated_response is not None:
                    yield Artifact(
                        name="delegation",
                        parts=[
                            DataPart(
                                data={
                                    "selectedAgent": delegated_response["delegated_agent"],
                                    "delegatedTaskId": delegated_response["delegated_task_id"],
                                    "finalState": delegated_response["delegated_final_state"].value,
                                    "statusUpdates": delegated_response["status_updates"],
                                }
                            )
                        ],
                    )
                    for update in delegated_response["status_updates"]:
                        if not isinstance(update, dict) or bool(update.get("final")):
                            continue
                        state_value = _coerce_task_state(update.get("state"), default=TaskState.working)
                        message_text = _format_delegation_status_text(
                            agent_name=delegated_response["delegated_agent"],
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
                    for artifact_name, payload in delegated_response["child_artifacts"].items():
                        if not isinstance(artifact_name, str) or not artifact_name.strip():
                            continue
                        parts = _ka2a_parts_from_model_content(payload)
                        if parts:
                            yield Artifact(name=f"{delegated_response['delegated_agent']}.{artifact_name}", parts=parts)
                    response_parts = delegated_response["response_parts"]
                    response_text = delegated_response["response_text"]
                    response_state_override = delegated_response["delegated_final_state"]
                    break

                interaction_output = next(
                    (
                        result.output
                        for result in tool_results
                        if not result.is_error and isinstance(result.output, dict)
                        and (
                            str(result.output.get("interaction_type") or "").strip()
                            or str(result.output.get("type") or "").strip().startswith("AGENT_")
                        )
                    ),
                    None,
                )
                if isinstance(interaction_output, dict):
                    response_text = json.dumps(interaction_output, ensure_ascii=False)
                    response_parts = [DataPart(data=interaction_output)]
                    break
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
        interaction_payload = _interaction_payload_from_parts(response_parts or [TextPart(text=response_text)])
        final_state = response_state_override or (
            TaskState.input_required if interaction_payload is not None else TaskState.completed
        )
        yield TaskStatus(state=final_state, message=agent_msg)

        if final_state == TaskState.input_required:
            return

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
