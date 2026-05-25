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
        + _render_relation_prompt_block(tools)
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

    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start >= 0 and end > start:
            candidate = raw[start : end + 1].strip()
            if candidate:
                return candidate
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
    text = " ".join((value or "").strip().lower().split())
    if not text:
        return ""
    substitutions = (
        (r"\bu\b", "you"),
        (r"\bur\b", "your"),
        (r"\bon[\s-]+boarding\b", "onboarding"),
        (r"\bon[\s-]+board\b", "onboarding"),
        (r"\bonoarding\b", "onboarding"),
    )
    for pattern, replacement in substitutions:
        text = re.sub(pattern, replacement, text)
    return " ".join(text.split())


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


_TOOL_EXECUTOR_SENTINEL = object()


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


ROUTER_AGENT_NAMES: set[str] = {"product", "inventory", "pos"}

SIMPLE_GREETING_QUERIES: set[str] = {
    "hello",
    "hello there",
    "hey",
    "hey there",
    "hi",
    "hi there",
    "good morning",
    "good afternoon",
    "good evening",
}


def _is_simple_greeting_query(value: str) -> bool:
    text = _normalize_user_text(value)
    return text in SIMPLE_GREETING_QUERIES


def _agent_intro_text(agent_name: str | None) -> str:
    normalized = str(agent_name or "").strip().lower()
    if normalized == "host":
        return "I’m your workspace host agent. What can I help you with?"
    return f"I’m your {_friendly_agent_label(normalized or 'assistant')} agent. What can I help you with?"


HOST_DOMAIN_AREA_PICKERS: dict[str, dict[str, Any]] = {
    "product": {
        "title": "Product Management",
        "description": "Choose the product area you want help with. You can also type a specific product question.",
        "options": [
            {"value": "product_discovery", "label": "Search and Product Discovery"},
            {"value": "marketplace_sourcing", "label": "Marketplace Sourcing"},
            {"value": "product_catalog_admin", "label": "Create or Update Products"},
            {"value": "product_merchandising", "label": "Merchandising and Attributes"},
            {"value": "product_pricing", "label": "Pricing and Price Rules"},
        ],
    },
    "inventory": {
        "title": "Inventory Management",
        "description": "Choose the inventory area you want help with. You can also type a specific inventory question.",
        "options": [
            {"value": "inventory_setup", "label": "Set Up Inventory"},
            {"value": "inventory_visibility", "label": "Stock and Warehouse Visibility"},
            {"value": "inventory_procurement", "label": "Purchase Orders and Receiving"},
            {"value": "inventory_fulfillment", "label": "Transfers, Adjustments, and Fulfillment"},
        ],
    },
    "pos": {
        "title": "Point of Sale (POS)",
        "description": "Choose the POS area you want help with. You can also type a specific POS question.",
        "options": [
            {"value": "pos_live", "label": "Live Cashier and Orders"},
            {"value": "pos_setup", "label": "POS Setup and Daily Sales"},
        ],
    },
}


HOST_DOMAIN_AREA_REQUESTS: dict[str, dict[str, str]] = {
    "product": {
        "product_discovery": (
            "The user selected Search and Product Discovery from the Product Management menu. "
            "Help them search the catalog, inspect product details, or review product analytics. "
            "Start with a short structured choice only if the next step is still ambiguous."
        ),
        "marketplace_sourcing": (
            "The user selected Marketplace Sourcing from the Product Management menu. "
            "Help them search online marketplaces, compare supplier offers, and shortlist products. "
            "Start by searching marketplaces or by asking a concise clarifying question about the product they want."
        ),
        "product_catalog_admin": (
            "The user selected Create or Update Products from the Product Management menu. "
            "Help them create, update, bulk seed, or export products. "
            "Start with a short structured choice or the next required step in that workflow."
        ),
        "product_merchandising": (
            "The user selected Merchandising and Attributes from the Product Management menu. "
            "Help them manage featured products, quick-sale state, attributes, or media. "
            "Start with a short structured choice or the next required step in that workflow."
        ),
        "product_pricing": (
            "The user selected Pricing and Price Rules from the Product Management menu. "
            "Help them inspect price history, pricing rules, or update prices. "
            "Start with a short structured choice or the next required step in that workflow."
        ),
    },
    "inventory": {
        "inventory_setup": (
            "The user selected Set Up Inventory from the Inventory Management menu. "
            "Help them create or configure stock locations, inventory categories, or inventory items. "
            "Start with a short structured choice or the next required setup step. "
            "Never ask for raw internal ids when lookups or selections can be used instead."
        ),
        "inventory_visibility": (
            "The user selected Stock and Warehouse Visibility from the Inventory Management menu. "
            "Help them inspect stock posture, warehouse contents, alerts, reservations, or movements. "
            "Start with a short structured choice only if the request is still ambiguous."
        ),
        "inventory_procurement": (
            "The user selected Purchase Orders and Receiving from the Inventory Management menu. "
            "Help them inspect purchase orders, receiving, suppliers, or purchase returns. "
            "Start with a short structured choice or the next required step in that workflow."
        ),
        "inventory_fulfillment": (
            "The user selected Transfers, Adjustments, and Fulfillment from the Inventory Management menu. "
            "Help them reserve stock, transfer inventory, adjust quantities, or handle fulfillment tasks. "
            "Start with a short structured choice or the next required step in that workflow."
        ),
    },
    "pos": {
        "pos_live": (
            "The user selected Live Cashier and Orders from the Point of Sale menu. "
            "Help them inspect the current session, live orders, held carts, checkout, or payments. "
            "Start with a short structured choice only if the next step is still ambiguous."
        ),
        "pos_setup": (
            "The user selected POS Setup and Daily Sales from the Point of Sale menu. "
            "Help them inspect terminals, tables, customers, discounts, or daily sales posture. "
            "Start with a short structured choice or the next required step in that workflow."
        ),
    },
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

    phrases = (
        "what agents",
        "what agents do you have",
        "which agents",
        "available agents",
        "registered agents",
        "agents that you have",
        "agents are registered",
        "currently registered",
        "currently active agents",
        "how many agents",
        "list agents",
        "show agents",
        "what specialists",
        "which specialists",
        "available specialists",
        "registered specialists",
        "which specialist agents",
        "what specialist agents",
        "who can you route to",
        "which agent can you route to",
        "which agents can you route to",
        "what agents can you route to",
        "is the",
        "is onboarding",
        "is inventory",
        "is pos",
        "is product",
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


def _is_host_availability_query(value: str) -> bool:
    text = _normalize_user_text(value)
    if not text:
        return False
    phrases = (
        "is the",
        "is onboarding",
        "active",
        "available",
        "not active",
        "not available",
        "why is it not",
        "why isn't",
        "why is onboarding",
        "error",
        "error message",
        "what is wrong",
    )
    return any(phrase in text for phrase in phrases)


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


def _agent_listing_names(agent_listing: dict[str, list[dict[str, Any]]] | None, key: str) -> set[str]:
    if not isinstance(agent_listing, dict):
        return set()
    return _available_agent_names(agent_listing.get(key))


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


def _host_domain_area_picker_arguments(agent_name: str) -> dict[str, Any] | None:
    config = HOST_DOMAIN_AREA_PICKERS.get(agent_name)
    if not isinstance(config, dict):
        return None
    return {
        "title": str(config.get("title") or _friendly_agent_label(agent_name)),
        "description": str(config.get("description") or ""),
        "options": list(config.get("options") or []),
        "multiple": False,
        "allow_input": True,
    }


def _is_host_domain_area_picker_payload(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        str(payload.get("interaction_type") or "").strip().lower() == "multiple_choice"
        and str(payload.get("workflow") or "").strip().lower() == "host_domain_area_picker"
        and str(payload.get("domain_agent") or "").strip().lower() in ROUTER_AGENT_NAMES
    )


def _interaction_additional_input(response: dict[str, Any] | None) -> str | None:
    if not isinstance(response, dict):
        return None
    additional_input = response.get("additional_input")
    if isinstance(additional_input, str) and additional_input.strip():
        return additional_input.strip()
    return None


def _host_domain_area_follow_up_request(payload: dict[str, Any], response: dict[str, Any]) -> tuple[str, str] | None:
    domain_agent = str(payload.get("domain_agent") or "").strip().lower()
    if domain_agent not in ROUTER_AGENT_NAMES:
        return None

    additional_input = _interaction_additional_input(response)
    if additional_input:
        return domain_agent, additional_input

    selected_value = _selected_interaction_value(response)
    if not selected_value:
        return None

    request = HOST_DOMAIN_AREA_REQUESTS.get(domain_agent, {}).get(selected_value)
    if not request:
        return None
    return domain_agent, request


def _host_follow_up_request_for_agent(agent_name: str) -> str:
    if agent_name == "onboarding":
        return (
            "Start a guided inventory onboarding flow. Ask the user what setup they want to complete first, "
            "then collect the required details step by step using structured interactions."
        )
    label = _friendly_agent_label(agent_name)
    return (
        f"The user selected {label} from the host menu. "
        "Briefly explain what kinds of tasks you can help with in this domain, "
        "using a concise user-facing summary."
    )


def _is_host_orchestration_payload(payload: dict[str, Any] | None, *, stage: str) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        str(payload.get("interaction_type") or "").strip().lower() == "multiple_choice"
        and str(payload.get("workflow") or "").strip().lower() == "host_orchestration"
        and str(payload.get("workflow_stage") or "").strip().lower() == stage
    )


def _host_orchestration_plan(query: str, agent_summaries: list[dict[str, Any]] | None) -> list[str]:
    text = _normalize_user_text(query)
    if not text:
        return []
    available_names = _available_agent_names(agent_summaries)
    candidates: list[tuple[str, int]] = []
    for agent_name, keywords in HOST_DOMAIN_KEYWORDS.items():
        if agent_name == "host":
            continue
        if available_names and agent_name not in available_names:
            continue
        score = sum(1 for keyword in keywords if keyword in text)
        if score > 0:
            candidates.append((agent_name, score))
    if not candidates:
        return []
    non_onboarding = [(name, score) for name, score in candidates if name != "onboarding"]
    if non_onboarding:
        candidates = non_onboarding
    candidates.sort(key=lambda item: item[1], reverse=True)
    plan: list[str] = []
    for name, _score in candidates:
        if name not in plan:
            plan.append(name)
    return plan


def _host_orchestration_continue_arguments(workflow_state: dict[str, Any]) -> dict[str, Any]:
    remaining = workflow_state.get("remaining_agents") if isinstance(workflow_state.get("remaining_agents"), list) else []
    next_agent = str(remaining[0] if remaining else "").strip()
    current_agent = str(workflow_state.get("current_agent") or "").strip()
    last_response_text = str(workflow_state.get("last_response_text") or "").strip()
    description = f"{_friendly_agent_label(current_agent)} finished the current step."
    if last_response_text:
        description = f"{description}\n\nLatest result: {last_response_text}"
    if next_agent:
        description = f"{description}\n\nContinue with {_friendly_agent_label(next_agent)} now?"
    return {
        "title": "Continue Workflow",
        "description": description,
        "options": [
            {"value": "continue_next", "label": f"Continue to {_friendly_agent_label(next_agent)}" if next_agent else "Continue"},
            {"value": "stop_here", "label": "Stop Here"},
        ],
        "multiple": False,
        "allow_input": False,
    }


def _host_orchestration_next_request(workflow_state: dict[str, Any], next_agent: str) -> str:
    original_request = str(workflow_state.get("original_request") or "").strip()
    completed_agents = workflow_state.get("completed_agents") if isinstance(workflow_state.get("completed_agents"), list) else []
    last_response_text = str(workflow_state.get("last_response_text") or "").strip()
    completed_labels = ", ".join(_friendly_agent_label(str(name)) for name in completed_agents if str(name).strip())
    blocks = [
        "Continue the user's multi-domain workflow.",
        f"Original user request: {original_request}" if original_request else "",
        f"Completed steps so far: {completed_labels}." if completed_labels else "",
        f"Latest completed step result: {last_response_text}" if last_response_text else "",
        f"Focus now on the {_friendly_agent_label(next_agent)} part of the workflow.",
        "Use structured interactions if more information is still required.",
    ]
    return "\n".join(block for block in blocks if block)


def _host_unavailable_agent_text(
    *,
    agent_name: str,
    available_names: set[str],
    registered_names: set[str] | None = None,
) -> str:
    label = _friendly_agent_label(agent_name)
    registered = set(registered_names or set())
    if agent_name in registered and agent_name not in available_names:
        if available_names:
            available_labels = ", ".join(_friendly_agent_label(name) for name in sorted(available_names))
            return (
                f"{label} is registered in the current agent directory, but it is not currently exposed to the host "
                f"for routing. The host currently routes to these available areas: {available_labels}. "
                "There is no specialist error message to show here because the host did not delegate this request. "
                "This looks like a host or gateway configuration issue, such as the downstream allowlist, not a "
                "downstream task failure."
            )
        return (
            f"{label} is registered in the current agent directory, but it is not currently exposed to the host "
            "for routing. There is no specialist error message to show here because delegation never started. "
            "This looks like a host or gateway configuration issue, such as the downstream allowlist."
        )
    if available_names:
        available_labels = ", ".join(_friendly_agent_label(name) for name in sorted(available_names))
        return (
            f"{label} is not active in the current agent directory. "
            f"The host currently sees these available areas: {available_labels}. "
            "There is no specialist error message to show here because the host did not delegate this request. "
            "This looks like an availability or deployment issue, not a downstream task failure."
        )
    return (
        f"{label} is not active in the current agent directory. "
        "The host cannot currently see any downstream specialist agents. "
        "There is no specialist error message to show here because delegation never started."
    )


ONBOARDING_SCOPE_LABELS: dict[str, str] = {
    "full_setup": "Full Inventory Setup",
    "stock_locations": "Stock Locations",
    "inventory_categories": "Inventory Categories",
    "inventory_setup": "Inventory Setup",
    "product_onboarding": "Product Onboarding",
}


RELATION_LOOKUP_REGISTRY: dict[str, dict[str, Any]] = {
    "inventory.list_inventory_categories": {
        "label": "Inventory Category",
        "model_tokens": {"inventorycategory"},
        "aliases": {
            "category",
            "categoryid",
            "defaultcategory",
            "defaultcategoryid",
            "inventorycategory",
            "inventorycategoryid",
        },
        "default_arguments": {"query": "", "limit": 25},
    },
    "inventory.list_stock_locations": {
        "label": "Stock Location",
        "model_tokens": {"stocklocation"},
        "aliases": {
            "defaultlocation",
            "defaultlocationid",
            "existinglocation",
            "existinglocationid",
            "fromlocationid",
            "primarylocation",
            "primarylocationid",
            "stocklocation",
            "stocklocationid",
            "tolocationid",
        },
        "default_arguments": {"limit": 25},
    },
    "inventory.search_stock_locations": {
        "label": "Stock Location",
        "model_tokens": {"stocklocation"},
        "aliases": {
            "defaultlocation",
            "defaultlocationid",
            "existinglocation",
            "existinglocationid",
            "fromlocationid",
            "primarylocation",
            "primarylocationid",
            "stocklocation",
            "stocklocationid",
            "tolocationid",
        },
        "default_arguments": {"query": "", "limit": 25},
    },
    "product.get_product_categories": {
        "label": "Product Category",
        "model_tokens": {"productcategory"},
        "aliases": {
            "category",
            "categoryid",
            "categoryrefid",
            "defaultcategory",
            "productcategory",
            "productcategoryid",
        },
        "default_arguments": {},
    },
    "product.search_products": {
        "label": "Product",
        "model_tokens": {"product"},
        "aliases": {"product", "productid"},
        "default_arguments": {"query": "", "limit": 25},
    },
    "inventory.list_inventory_items": {
        "label": "Inventory Item",
        "model_tokens": {"inventory", "inventoryitem"},
        "aliases": {"inventory", "inventoryid", "inventoryitem", "inventoryitemid"},
        "default_arguments": {},
    },
    "inventory.search_inventory_items": {
        "label": "Inventory Item",
        "model_tokens": {"inventory", "inventoryitem"},
        "aliases": {"inventory", "inventoryid", "inventoryitem", "inventoryitemid"},
        "default_arguments": {"query": "", "limit": 25},
    },
    "inventory.search_purchase_orders": {
        "label": "Purchase Order",
        "model_tokens": {"purchaseorder"},
        "aliases": {"purchaseorder", "purchaseorderid", "po", "poid", "order", "orderid"},
        "default_arguments": {"query": "", "limit": 25},
    },
}


def _normalize_relation_token(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _relation_text_match_score(text: str, alias: str) -> int:
    normalized_text = _normalize_relation_token(text)
    normalized_alias = _normalize_relation_token(alias)
    if not normalized_text or not normalized_alias:
        return 0

    prefixes = ("default", "related", "parent", "child", "from", "to", "selected", "primary")
    suffixes = ("id", "uuid", "ref", "reference", "identifier")

    if normalized_text == normalized_alias:
        return 500 + len(normalized_alias)
    if any(normalized_text == f"{normalized_alias}{suffix}" for suffix in suffixes):
        return 480 + len(normalized_alias)
    if any(normalized_text == f"{prefix}{normalized_alias}" for prefix in prefixes):
        return 460 + len(normalized_alias)
    if any(normalized_text == f"{prefix}{normalized_alias}{suffix}" for prefix in prefixes for suffix in suffixes):
        return 470 + len(normalized_alias)
    if any(normalized_text.endswith(f"{normalized_alias}{suffix}") for suffix in suffixes):
        return 300 + len(normalized_alias)
    if normalized_text.endswith(normalized_alias):
        return 200 + len(normalized_alias)
    return 0


def _relation_lookup_specs(tool_specs: list[ToolSpec]) -> list[dict[str, Any]]:
    available_names = {spec.name for spec in tool_specs}
    out: list[dict[str, Any]] = []
    for tool_name, config in RELATION_LOOKUP_REGISTRY.items():
        if tool_name not in available_names:
            continue
        out.append(
            {
                "lookup_tool": tool_name,
                "label": str(config.get("label") or tool_name),
                "model_tokens": {
                    _normalize_relation_token(item)
                    for item in config.get("model_tokens", set())
                    if _normalize_relation_token(str(item))
                },
                "aliases": {
                    _normalize_relation_token(item)
                    for item in config.get("aliases", set())
                    if _normalize_relation_token(str(item))
                },
                "default_arguments": dict(config.get("default_arguments") or {}),
            }
        )
    return out


def _iter_schema_leaf_fields(schema: dict[str, Any], *, prefix: str = "") -> list[tuple[str, dict[str, Any]]]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []

    leaves: list[tuple[str, dict[str, Any]]] = []
    for key, value in properties.items():
        if not isinstance(value, dict):
            continue
        path = f"{prefix}.{key}" if prefix else str(key)
        nested_properties = value.get("properties")
        if isinstance(nested_properties, dict):
            leaves.extend(_iter_schema_leaf_fields(value, prefix=path))
            continue
        leaves.append((path, value))
    return leaves


def _relation_model_tokens_from_field(path: str, field_schema: dict[str, Any]) -> set[str]:
    candidates: set[str] = set()
    description = str(field_schema.get("description") or "").strip()
    for match in re.findall(r"UUID of ([A-Za-z][A-Za-z0-9_() ]+)", description):
        cleaned = match.replace("(", " ").replace(")", " ")
        for token in reversed(cleaned.split()):
            normalized = _normalize_relation_token(token)
            if normalized:
                candidates.add(normalized)
                break

    normalized_path = _normalize_relation_token(path)
    field_name = path.split(".")[-1]
    normalized_field_name = _normalize_relation_token(field_name)
    if normalized_field_name.endswith("id"):
        normalized_field_name = normalized_field_name[:-2]
    for candidate in (
        normalized_path,
        normalized_field_name,
        normalized_field_name.removeprefix("default"),
        normalized_field_name.removeprefix("parent"),
        normalized_field_name.removeprefix("child"),
        normalized_field_name.removeprefix("from"),
        normalized_field_name.removeprefix("to"),
    ):
        if candidate:
            candidates.add(candidate)
    return {item for item in candidates if item}


def _relation_prompt_hints(tool_specs: list[ToolSpec]) -> list[tuple[str, str, str]]:
    relation_specs = _relation_lookup_specs(tool_specs)
    if not relation_specs:
        return []

    hints: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for spec in tool_specs:
        if not isinstance(spec.input_schema, dict):
            continue
        for path, field_schema in _iter_schema_leaf_fields(spec.input_schema):
            model_tokens = _relation_model_tokens_from_field(path, field_schema)
            for relation_spec in relation_specs:
                if not model_tokens.intersection(relation_spec["model_tokens"]):
                    continue
                hint = (spec.name, path, relation_spec["lookup_tool"])
                if hint in seen:
                    continue
                seen.add(hint)
                hints.append(hint)
    return hints


def _render_relation_prompt_block(tools: list[ToolSpec]) -> str:
    hints = _relation_prompt_hints(tools)
    if not hints:
        return ""

    lines = [
        "",
        "Relation/lookup rules:",
        "- Never ask the user to manually type backend IDs or UUIDs for relational fields.",
        "- For any relational field, fetch the available records first, present human-readable labels, and submit the matching internal ID only after the user selects an option.",
        "- When loading selectable reference data for the current tenant, prefer list/get-all tools over search tools whenever both are available.",
        "- If a list/get-all tool exposes optional query, limit, or filter arguments, treat those as internal defaults for the agent to supply. Do not tell the user the backend requires those parameters.",
        "- If only a search-style lookup tool exists, call it with an empty string and omit optional filters/null values instead of asking the user for a search term.",
        "- For create, update, move, or transfer workflows, gather enough information first. Do not mutate records until the required fields are known or the user has confirmed the proposed selections.",
        "- Prefer a conversational gather-and-confirm flow: understand the request, fetch any missing backend options, present a form or selection when needed, then execute the write once the user confirms.",
    ]
    for tool_name, field_path, lookup_tool in hints:
        lines.append(
            f"- For `{tool_name}.{field_path}`, fetch options with `{lookup_tool}` and present them as selectable labels."
        )
    return "\n" + "\n".join(lines)


def _with_interaction_metadata(payload: dict[str, Any], **metadata: Any) -> dict[str, Any]:
    enriched = dict(payload)
    enriched.update(metadata)
    return enriched


def _is_onboarding_payload(payload: dict[str, Any] | None, *, stage: str) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        str(payload.get("workflow") or "").strip().lower() == "inventory_onboarding"
        and str(payload.get("workflow_stage") or "").strip().lower() == stage
    )


def _onboarding_scope_picker_arguments(
    *,
    description: str = "Choose the setup area you want to complete first. I will guide you step by step.",
) -> dict[str, Any]:
    return {
        "title": "Start Inventory Onboarding",
        "description": description,
        "options": [
            {"value": "full_setup", "label": "Full Inventory Setup"},
            {"value": "stock_locations", "label": "Stock Locations"},
            {"value": "inventory_categories", "label": "Inventory Categories"},
            {"value": "inventory_setup", "label": "Inventory Setup"},
            {"value": "product_onboarding", "label": "Product Onboarding"},
        ],
        "multiple": False,
        "allow_input": True,
    }


def _select_options(options: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"value": value, "label": label} for value, label in options]


def _onboarding_wizard_steps(scope: str) -> list[dict[str, Any]]:
    if scope == "stock_locations":
        return [
            {
                "id": "locations",
                "title": "Stock Locations",
                "description": "Tell me how you want your stock locations organized.",
                "fields": [
                    {
                        "name": "primary_location_mode",
                        "type": "select",
                        "label": "Primary Location Source",
                        "required": True,
                        "options": _select_options(
                            [
                                ("new", "Create a New Primary Location"),
                                ("existing", "Use an Existing Location"),
                            ]
                        ),
                        "placeholder": "Choose how to set the primary location",
                    },
                    {
                        "name": "primary_location_id",
                        "type": "select",
                        "label": "Existing Primary Location",
                        "required": False,
                        "options": [],
                        "placeholder": "Select an existing stock location",
                        "show_when": {"field": "primary_location_mode", "equals": "existing"},
                    },
                    {
                        "name": "primary_location_name",
                        "type": "text",
                        "label": "Primary Location Name",
                        "required": False,
                        "placeholder": "Main Warehouse",
                        "show_when": {"field": "primary_location_mode", "equals": "new"},
                    },
                    {
                        "name": "primary_location_type",
                        "type": "select",
                        "label": "Primary Location Type",
                        "required": False,
                        "options": _select_options(
                            [
                                ("warehouse", "Warehouse"),
                                ("store", "Store"),
                                ("backroom", "Back Room"),
                                ("fulfillment", "Fulfillment Center"),
                                ("other", "Other"),
                            ]
                        ),
                        "show_when": {"field": "primary_location_mode", "equals": "new"},
                    },
                    {
                        "name": "additional_locations",
                        "type": "textarea",
                        "label": "Additional Locations",
                        "required": False,
                        "placeholder": "Front Store\nReturns Shelf\nOverflow Room",
                    },
                ],
            }
        ]
    if scope == "inventory_categories":
        return [
            {
                "id": "categories",
                "title": "Inventory Categories",
                "description": "List the categories you want available before product entry.",
                "fields": [
                    {
                        "name": "category_names",
                        "type": "textarea",
                        "label": "Category Names",
                        "required": True,
                        "placeholder": "Beverages\nSnacks\nCleaning Supplies",
                    }
                ],
            }
        ]
    if scope == "inventory_setup":
        return [
            {
                "id": "inventory",
                "title": "Inventory Setup",
                "description": "Define the first inventory item you want to create.",
                "fields": [
                    {
                        "name": "default_inventory_name",
                        "type": "text",
                        "label": "Inventory Name",
                        "required": True,
                        "placeholder": "Main Inventory",
                    },
                    {
                        "name": "inventory_description",
                        "type": "textarea",
                        "label": "Inventory Description",
                        "required": False,
                        "placeholder": "Primary sellable stock item for the business.",
                    },
                    {
                        "name": "related_stock_location_id",
                        "type": "select",
                        "label": "Primary Location for This Inventory",
                        "required": False,
                        "options": [],
                        "placeholder": "Select a stock location",
                    },
                    {
                        "name": "inventory_category_id",
                        "type": "select",
                        "label": "Default Category",
                        "required": False,
                        "options": [],
                        "placeholder": "Select an inventory category",
                    },
                ],
            }
        ]
    if scope == "product_onboarding":
        return [
            {
                "id": "products",
                "title": "Initial Product Onboarding",
                "description": "Tell me about the first products you want to seed into the catalog.",
                "fields": [
                    {
                        "name": "product_names",
                        "type": "textarea",
                        "label": "Product Names",
                        "required": True,
                        "placeholder": "Coca-Cola 50cl\nFanta 50cl\nSprite 50cl",
                    },
                    {
                        "name": "product_category_id",
                        "type": "select",
                        "label": "Default Product Category",
                        "required": False,
                        "options": [],
                        "placeholder": "Select a product category",
                    },
                    {
                        "name": "pos_ready",
                        "type": "boolean",
                        "label": "Make These Products POS-Ready",
                        "required": False,
                    },
                ],
            }
        ]

    return [
        {
            "id": "locations",
            "title": "Stock Locations",
            "description": "Set up the main places where stock will live.",
            "fields": [
                {
                    "name": "primary_location_mode",
                    "type": "select",
                    "label": "Primary Location Source",
                    "required": True,
                    "options": _select_options(
                        [
                            ("new", "Create a New Primary Location"),
                            ("existing", "Use an Existing Location"),
                        ]
                    ),
                    "placeholder": "Choose how to set the primary location",
                },
                {
                    "name": "primary_location_id",
                    "type": "select",
                    "label": "Existing Primary Location",
                    "required": False,
                    "options": [],
                    "placeholder": "Select an existing stock location",
                    "show_when": {"field": "primary_location_mode", "equals": "existing"},
                },
                {
                    "name": "primary_location_name",
                    "type": "text",
                    "label": "Primary Location Name",
                    "required": False,
                    "placeholder": "Main Warehouse",
                    "show_when": {"field": "primary_location_mode", "equals": "new"},
                },
                {
                    "name": "primary_location_type",
                    "type": "select",
                    "label": "Primary Location Type",
                    "required": False,
                    "options": _select_options(
                        [
                            ("warehouse", "Warehouse"),
                            ("store", "Store"),
                            ("backroom", "Back Room"),
                            ("fulfillment", "Fulfillment Center"),
                            ("other", "Other"),
                        ]
                    ),
                    "show_when": {"field": "primary_location_mode", "equals": "new"},
                },
                {
                    "name": "additional_locations",
                    "type": "textarea",
                    "label": "Additional Locations",
                    "required": False,
                    "placeholder": "Front Store\nReturns Shelf\nOverflow Room",
                },
            ],
        },
        {
            "id": "categories",
            "title": "Inventory Categories",
            "description": "Define the category structure you want ready before product entry.",
            "fields": [
                {
                    "name": "category_names",
                    "type": "textarea",
                    "label": "Category Names",
                    "required": True,
                    "placeholder": "Beverages\nSnacks\nCleaning Supplies",
                }
            ],
        },
        {
            "id": "inventory",
            "title": "Inventory Item",
            "description": "Define the first inventory item to create.",
            "fields": [
                {
                    "name": "default_inventory_name",
                    "type": "text",
                    "label": "Inventory Name",
                    "required": True,
                    "placeholder": "Main Inventory",
                },
                {
                    "name": "inventory_description",
                    "type": "textarea",
                    "label": "Inventory Description",
                    "required": False,
                    "placeholder": "Primary sellable stock item for the business.",
                },
                {
                    "name": "related_stock_location_id",
                    "type": "select",
                    "label": "Primary Location for This Inventory",
                    "required": False,
                    "options": [],
                    "placeholder": "Select a stock location",
                },
                {
                    "name": "inventory_category_id",
                    "type": "select",
                    "label": "Default Category",
                    "required": False,
                    "options": [],
                    "placeholder": "Select an inventory category",
                },
            ],
        },
        {
            "id": "products",
            "title": "Product Follow-Up",
            "description": "Decide whether you want to continue into initial product onboarding after the foundation setup.",
            "fields": [
                {
                    "name": "continue_to_product_onboarding",
                    "type": "boolean",
                    "label": "Continue to Product Onboarding After Foundation Setup",
                    "required": False,
                },
                {
                    "name": "initial_product_names",
                    "type": "textarea",
                    "label": "Optional Initial Product Names",
                    "required": False,
                    "placeholder": "Coca-Cola 50cl\nFanta 50cl",
                },
                {
                    "name": "product_category_id",
                    "type": "select",
                    "label": "Default Product Category",
                    "required": False,
                    "options": [],
                    "placeholder": "Select a product category",
                },
                {
                    "name": "pos_ready",
                    "type": "boolean",
                    "label": "Make These Products POS-Ready",
                    "required": False,
                },
            ],
        },
    ]


def _onboarding_wizard_arguments(scope: str) -> dict[str, Any]:
    label = ONBOARDING_SCOPE_LABELS.get(scope, "Inventory Onboarding")
    return {
        "title": f"{label} Wizard",
        "description": "Fill in the setup details and I will prepare the onboarding action plan.",
        "steps": _onboarding_wizard_steps(scope),
        "allow_back": True,
        "show_progress": True,
    }


def _split_inline_list_values(value: str) -> list[str]:
    entries: list[str] = []
    for raw in re.split(r"[\n,;]+|\band\b", value, flags=re.IGNORECASE):
        cleaned = raw.strip().strip("-").strip()
        if cleaned:
            entries.append(cleaned)
    return entries


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        key = re.sub(r"\s+", " ", value.strip().lower())
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(value.strip())
    return unique


def _normalize_location_type_value(value: str | None) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if "warehouse" in text:
        return "warehouse"
    if "store" in text or "showroom" in text or "shop" in text:
        return "store"
    if "backroom" in text or "back room" in text:
        return "backroom"
    if "fulfillment" in text:
        return "fulfillment"
    return "other"


def _extract_named_text_list(text: str, patterns: tuple[str, ...]) -> list[str]:
    items: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
            value = str(match.group("value") or "").strip()
            if value:
                items.extend(_split_inline_list_values(value))
    return _dedupe_preserving_order(items)


def _extract_first_named_value(text: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if not match:
            continue
        value = str(match.group("value") or "").strip()
        if value:
            return value
    return None


def _infer_onboarding_scope_from_text(text: str) -> str | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    has_location = any(token in normalized for token in ("warehouse", "location", "store", "backroom", "fulfillment"))
    has_category = "categor" in normalized
    has_inventory = "inventory" in normalized or "stock ledger" in normalized
    has_product = "product" in normalized or "sku" in normalized
    if sum(bool(flag) for flag in (has_location, has_category, has_inventory, has_product)) >= 2:
        return "full_setup"
    if has_location:
        return "stock_locations"
    if has_category:
        return "inventory_categories"
    if has_inventory:
        return "inventory_setup"
    if has_product:
        return "product_onboarding"
    if "onboarding" in normalized or "set up" in normalized or "setup" in normalized:
        return "full_setup"
    return None


def _parse_onboarding_prefill_from_text(scope: str, text: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    stripped = text.strip()
    if not stripped:
        return parsed

    primary_location_name = _extract_first_named_value(
        stripped,
        (
            r"(?:^|\n)\s*(?:primary|main)\s+location(?:\s+name)?\s*[:=-]\s*(?P<value>[^\n]+)",
            r"(?:^|\n)\s*main\s+warehouse\s*[:=-]\s*(?P<value>[^\n]+)",
        ),
    )
    if not primary_location_name:
        location_phrase = re.search(
            r"\b(?P<value>(?:main|primary)\s+[a-z0-9][a-z0-9&'()/ -]{1,80}?(?:warehouse|store|shop|showroom|backroom|back room|fulfillment(?: center)?))\b",
            stripped,
            flags=re.IGNORECASE,
        )
        if location_phrase:
            primary_location_name = str(location_phrase.group("value") or "").strip()
    if primary_location_name:
        parsed["primary_location_name"] = primary_location_name
        parsed["primary_location_type"] = _normalize_location_type_value(primary_location_name)
        parsed["primary_location_mode"] = "new"

    normalized = _normalize_user_text(stripped)
    if primary_location_name and ("existing" in normalized or "already have" in normalized or "use current" in normalized):
        parsed["primary_location_mode"] = "existing"

    explicit_location_type = _extract_first_named_value(
        stripped,
        (
            r"(?:^|\n)\s*(?:primary\s+)?location\s+type\s*[:=-]\s*(?P<value>[^\n]+)",
            r"(?:^|\n)\s*(?:primary\s+)?warehouse\s+type\s*[:=-]\s*(?P<value>[^\n]+)",
        ),
    )
    normalized_location_type = _normalize_location_type_value(explicit_location_type)
    if normalized_location_type:
        parsed["primary_location_type"] = normalized_location_type

    additional_locations = _extract_named_text_list(
        stripped,
        (
            r"(?:^|\n)\s*(?:additional|other|secondary)\s+locations?\s*[:=-]\s*(?P<value>[^\n]+(?:\n(?!\s*[A-Za-z][A-Za-z /'-]{0,40}\s*[:=-]).+)*)",
            r"(?:^|\n)\s*locations?\s*[:=-]\s*(?P<value>[^\n]+(?:\n(?!\s*[A-Za-z][A-Za-z /'-]{0,40}\s*[:=-]).+)*)",
        ),
    )
    if additional_locations and parsed.get("primary_location_name"):
        normalized_primary = re.sub(r"\s+", " ", str(parsed["primary_location_name"]).strip().lower())
        additional_locations = [
            item
            for item in additional_locations
            if re.sub(r"\s+", " ", item.strip().lower()) != normalized_primary
        ]
    if additional_locations:
        parsed["additional_locations"] = "\n".join(_dedupe_preserving_order(additional_locations))

    category_names = _extract_named_text_list(
        stripped,
        (
            r"(?:^|\n)\s*(?:inventory\s+)?categories?\s*[:=-]\s*(?P<value>[^\n]+(?:\n(?!\s*[A-Za-z][A-Za-z /'-]{0,40}\s*[:=-]).+)*)",
            r"\bcategories?\s+(?:are|like|such as)\s+(?P<value>[^.]+)",
        ),
    )
    if category_names:
        multiline_categories = "\n".join(_dedupe_preserving_order(category_names))
        parsed["category_names"] = multiline_categories
        parsed["inventory_category_name"] = _dedupe_preserving_order(category_names)[0]

    inventory_name = _extract_first_named_value(
        stripped,
        (
            r"(?:^|\n)\s*(?:inventory|stock\s+ledger)\s+name\s*[:=-]\s*(?P<value>[^\n]+)",
            r"(?:^|\n)\s*inventory\s*[:=-]\s*(?P<value>[^\n]+)",
        ),
    )
    if inventory_name:
        parsed["default_inventory_name"] = inventory_name

    inventory_description = _extract_first_named_value(
        stripped,
        (
            r"(?:^|\n)\s*(?:inventory\s+)?description\s*[:=-]\s*(?P<value>[^\n]+)",
            r"\bdescribe(?:d)?\s+as\s+(?P<value>[^.]+)",
        ),
    )
    if inventory_description:
        parsed["inventory_description"] = inventory_description

    related_location_name = _extract_first_named_value(
        stripped,
        (
            r"(?:^|\n)\s*(?:inventory|default|ledger)\s+location\s*[:=-]\s*(?P<value>[^\n]+)",
            r"(?:^|\n)\s*(?:primary\s+location\s+for\s+inventory)\s*[:=-]\s*(?P<value>[^\n]+)",
        ),
    )
    if related_location_name:
        parsed["related_stock_location_name"] = related_location_name

    product_names = _extract_named_text_list(
        stripped,
        (
            r"(?:^|\n)\s*(?:initial\s+)?products?\s*[:=-]\s*(?P<value>[^\n]+(?:\n(?!\s*[A-Za-z][A-Za-z /'-]{0,40}\s*[:=-]).+)*)",
            r"\bproducts?\s+(?:are|like|such as)\s+(?P<value>[^.]+)",
        ),
    )
    if product_names:
        parsed["initial_product_names"] = "\n".join(_dedupe_preserving_order(product_names))
        parsed["continue_to_product_onboarding"] = True

    product_category_name = _extract_first_named_value(
        stripped,
        (
            r"(?:^|\n)\s*product\s+category\s*[:=-]\s*(?P<value>[^\n]+)",
        ),
    )
    if product_category_name:
        parsed["product_category_name"] = product_category_name

    if "pos-ready" in normalized or "pos ready" in normalized:
        parsed["pos_ready"] = True
    if "do not add products" in normalized or "don't add products" in normalized:
        parsed["continue_to_product_onboarding"] = False

    if scope == "stock_locations":
        return {
            key: value
            for key, value in parsed.items()
            if key in {"primary_location_mode", "primary_location_name", "primary_location_type", "additional_locations"}
        }
    if scope == "inventory_categories":
        return {key: value for key, value in parsed.items() if key in {"category_names"}}
    if scope == "inventory_setup":
        return {
            key: value
            for key, value in parsed.items()
            if key
            in {
                "default_inventory_name",
                "inventory_description",
                "related_stock_location_name",
                "inventory_category_name",
            }
        }
    if scope == "product_onboarding":
        return {
            key: value
            for key, value in parsed.items()
            if key in {"initial_product_names", "product_category_name", "pos_ready"}
        }
    return parsed


def _prefill_match_option_value(field: dict[str, Any], desired_text: str) -> Any:
    desired = re.sub(r"\s+", " ", desired_text.strip().lower())
    options = field.get("options")
    if not desired or not isinstance(options, list):
        return None
    for option in options:
        if not isinstance(option, dict):
            continue
        option_label = re.sub(r"\s+", " ", str(option.get("label") or "").strip().lower())
        option_value_text = re.sub(r"\s+", " ", str(option.get("value") or "").strip().lower())
        if desired == option_label or desired == option_value_text or desired in option_label:
            return option.get("value")
    return None


def _prefill_value_for_wizard_field(field: dict[str, Any], prefill_data: dict[str, Any]) -> Any:
    field_name = str(field.get("name") or "").strip()
    if not field_name:
        return None
    if field_name in prefill_data:
        return prefill_data[field_name]

    desired_text: str | None = None
    if field_name == "related_stock_location_id":
        desired_text = (
            str(prefill_data.get("related_stock_location_name") or "").strip()
            or str(prefill_data.get("related_location_name") or "").strip()
            or str(prefill_data.get("primary_location_name") or "").strip()
        )
    elif field_name == "primary_location_id":
        if str(prefill_data.get("primary_location_mode") or "").strip().lower() == "existing":
            desired_text = str(prefill_data.get("primary_location_name") or "").strip()
    elif field_name == "inventory_category_id":
        desired_text = (
            str(prefill_data.get("inventory_category_name") or "").strip()
            or str(prefill_data.get("category_name") or "").strip()
        )
    elif field_name == "product_category_id":
        desired_text = (
            str(prefill_data.get("product_category_name") or "").strip()
            or str(prefill_data.get("category_name") or "").strip()
        )
    if desired_text:
        return _prefill_match_option_value(field, desired_text)
    return None


def _build_onboarding_existing_responses(
    scope: str,
    *,
    wizard_payload: dict[str, Any],
    prefill_data: dict[str, Any],
) -> dict[str, Any]:
    steps = wizard_payload.get("steps")
    if not isinstance(steps, list):
        steps = _onboarding_wizard_steps(scope)
    existing_responses: dict[str, Any] = {}
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        fields = step.get("fields")
        if not isinstance(fields, list):
            continue
        step_response: dict[str, Any] = {}
        for field in fields:
            if not isinstance(field, dict):
                continue
            value = _prefill_value_for_wizard_field(field, prefill_data)
            if value in ("", None, [], {}):
                continue
            step_response[str(field.get("name") or "").strip()] = value
        if step_response:
            existing_responses[f"step_{index}"] = step_response
    return existing_responses


def _wizard_label_field_name(field_name: str) -> str | None:
    name = str(field_name or "").strip()
    if not name:
        return None
    if name.endswith("_id"):
        return f"{name[:-3]}_label"
    if name.endswith("_label"):
        return name
    return f"{name}_label"


def _wizard_option_label(field: dict[str, Any], value: Any) -> str | None:
    options = field.get("options")
    if not isinstance(options, list):
        return None
    for option in options:
        if not isinstance(option, dict):
            continue
        option_value = option.get("value")
        if option_value is None or str(option_value) != str(value):
            continue
        label = str(option.get("label") or option_value).strip()
        return label or None
    return None


def _split_multiline_values(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    entries: list[str] = []
    for raw in re.split(r"[\n,]+", value):
        cleaned = raw.strip().strip("-").strip()
        if cleaned:
            entries.append(cleaned)
    return entries


def _normalize_onboarding_wizard_data(
    scope: str,
    response: dict[str, Any],
    *,
    wizard_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    responses = response.get("all_responses") if isinstance(response.get("all_responses"), dict) else {}
    payload_steps = wizard_payload.get("steps") if isinstance(wizard_payload, dict) else None
    steps = payload_steps if isinstance(payload_steps, list) and payload_steps else _onboarding_wizard_steps(scope)
    step_values: dict[str, dict[str, Any]] = {}
    flat: dict[str, Any] = {}

    for index, step in enumerate(steps):
        raw_step = responses.get(f"step_{index}")
        if not isinstance(raw_step, dict):
            continue
        step_id = str(step.get("id") or f"step_{index}")
        step_values[step_id] = raw_step
        field_map = {
            str(field.get("name") or "").strip(): field
            for field in step.get("fields", [])
            if isinstance(field, dict) and str(field.get("name") or "").strip()
        }
        for key, value in raw_step.items():
            if value in ("", None, [], {}):
                continue
            flat[key] = value
            field = field_map.get(key)
            if not isinstance(field, dict):
                continue
            label_value = _wizard_option_label(field, value)
            label_key = _wizard_label_field_name(key)
            if label_value and label_key and label_key not in flat:
                flat[label_key] = label_value

    return {
        "scope": scope,
        "steps": step_values,
        "flat": flat,
        "raw_response": response,
    }


def _onboarding_summary_text(scope: str, data: dict[str, Any]) -> str:
    flat = data.get("flat") if isinstance(data.get("flat"), dict) else {}
    lines = [f"Scope: {ONBOARDING_SCOPE_LABELS.get(scope, scope.replace('_', ' ').title())}"]

    primary_location_mode = str(flat.get("primary_location_mode_label") or flat.get("primary_location_mode") or "").strip().lower()
    primary_location = (
        str(flat.get("primary_location_label") or "").strip()
        or str(flat.get("primary_location_name") or "").strip()
    )
    primary_location_type = (
        str(flat.get("primary_location_type_label") or "").strip()
        or str(flat.get("primary_location_type") or "").strip()
    )
    if primary_location:
        label = primary_location
        if primary_location_type:
            label = f"{label} ({primary_location_type})"
        prefix = "Existing primary location" if primary_location_mode == "existing" else "Primary location"
        lines.append(f"{prefix}: {label}")

    additional_locations = _split_multiline_values(flat.get("additional_locations"))
    if additional_locations:
        lines.append("Additional locations: " + ", ".join(additional_locations))

    categories = _split_multiline_values(flat.get("category_names"))
    if categories:
        lines.append("Categories: " + ", ".join(categories))

    inventory_name = str(flat.get("default_inventory_name") or "").strip()
    if inventory_name:
        lines.append(f"Inventory item: {inventory_name}")

    inventory_description = str(flat.get("inventory_description") or "").strip()
    if inventory_description:
        lines.append(f"Inventory description: {inventory_description}")

    related_location_name = (
        str(flat.get("related_stock_location_label") or "").strip()
        or str(flat.get("related_location_name") or "").strip()
    )
    if related_location_name:
        lines.append(f"Ledger location: {related_location_name}")

    category_name = (
        str(flat.get("inventory_category_label") or "").strip()
        or str(flat.get("category_name") or "").strip()
    )
    if category_name:
        lines.append(f"Default category: {category_name}")

    product_names = _split_multiline_values(flat.get("product_names") or flat.get("initial_product_names"))
    if product_names:
        lines.append("Products: " + ", ".join(product_names))

    product_category = (
        str(flat.get("product_category_label") or "").strip()
        or str(flat.get("product_category") or "").strip()
    )
    if product_category:
        lines.append(f"Product category: {product_category}")

    if isinstance(flat.get("pos_ready"), bool):
        lines.append(f"POS ready: {'Yes' if flat['pos_ready'] else 'No'}")

    if isinstance(flat.get("continue_to_product_onboarding"), bool):
        lines.append(
            "Continue to product onboarding: "
            + ("Yes" if flat["continue_to_product_onboarding"] else "No")
        )

    return "\n".join(lines)


def _onboarding_review_picker_arguments(summary: str) -> dict[str, Any]:
    return {
        "title": "Review Onboarding Plan",
        "description": summary + "\n\nChoose what you want me to do next.",
        "options": [
            {"value": "create_now", "label": "Create This Setup"},
            {"value": "revise_answers", "label": "Revise My Answers"},
            {"value": "cancel_onboarding", "label": "Cancel For Now"},
        ],
        "multiple": False,
        "allow_input": True,
    }


def _onboarding_target_agent(scope: str) -> str:
    return "product" if scope == "product_onboarding" else "inventory"


def _onboarding_creation_request(scope: str, data: dict[str, Any]) -> str:
    serialized = json.dumps(data, ensure_ascii=False)
    if scope == "product_onboarding":
        return (
            "Create the initial product onboarding setup using the available product write tools if possible. "
            "Perform the requested product creation work rather than only describing it. "
            "If any required detail is missing, ask one concise follow-up question.\n"
            f"Collected onboarding data JSON:\n{serialized}"
        )
    return (
        "Create the requested inventory foundation setup using the available inventory write tools if possible. "
        "Create stock locations, inventory categories, and inventory items as applicable to the collected data, "
        "rather than only describing them. If any required detail is missing, ask one concise follow-up question.\n"
        f"Collected onboarding data JSON:\n{serialized}"
    )


def _is_inventory_setup_payload(payload: dict[str, Any] | None, *, stage: str) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        str(payload.get("workflow") or "").strip().lower() == "inventory_setup_mutation"
        and str(payload.get("workflow_stage") or "").strip().lower() == stage
    )


def _inventory_setup_action_from_text(text: str) -> str | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    if any(token in normalized for token in ("parent of", "child of", "as parent of", "assign as parent", "set parent of")):
        return "update_stock_location_parent"
    if "location" in normalized and any(token in normalized for token in ("create", "add", "new", "set up", "setup")):
        return "create_stock_location"
    if "inventory" in normalized and any(token in normalized for token in ("create", "add", "new", "set up", "setup")):
        return "create_inventory_item"
    return None


def _inventory_setup_action_label(action: str) -> str:
    mapping = {
        "create_inventory_item": "Create Inventory Item",
        "create_stock_location": "Create Stock Location",
        "update_stock_location_parent": "Update Stock Location Parent",
    }
    return mapping.get(action, action.replace("_", " ").title())


def _clean_extracted_phrase(value: str | None) -> str | None:
    text = str(value or "").strip().strip(".").strip()
    if not text:
        return None
    return text


def _parse_inventory_setup_prefill_from_text(action: str, text: str) -> dict[str, Any]:
    prefill: dict[str, Any] = {}
    if action == "create_inventory_item":
        prefill.update(_parse_onboarding_prefill_from_text("inventory_setup", text))
        direct_category = _extract_first_named_value(
            text,
            (
                r"(?:^|\n)\s*(?:inventory\s+)?category\s*[:=-]\s*(?P<value>[^\n]+)",
                r"\bcategory\s+(?:is|should be|as)\s+(?P<value>[^.]+)",
            ),
        )
        if direct_category:
            prefill["inventory_category_name"] = direct_category
        return prefill

    if action == "create_stock_location":
        name = _extract_first_named_value(
            text,
            (
                r"(?:^|\n)\s*(?:stock\s+)?location\s+name\s*[:=-]\s*(?P<value>[^\n]+)",
                r"\b(?:create|add|new)\s+(?:a\s+)?(?:stock\s+)?location\s+(?:called|named)\s+(?P<value>[^,\n.]+)",
            ),
        )
        if name:
            prefill["location_name"] = name
            prefill["location_type_name"] = _normalize_location_type_value(name)
        location_type = _extract_first_named_value(
            text,
            (
                r"(?:^|\n)\s*location\s+type\s*[:=-]\s*(?P<value>[^\n]+)",
                r"\btype\s+(?:is|should be|as)\s+(?P<value>[^,\n.]+)",
            ),
        )
        if location_type:
            prefill["location_type_name"] = location_type
        parent_name = _extract_first_named_value(
            text,
            (
                r"(?:^|\n)\s*parent\s+location\s*[:=-]\s*(?P<value>[^\n]+)",
                r"\bunder\s+(?P<value>[^,\n.]+)",
            ),
        )
        if parent_name:
            prefill["parent_location_name"] = parent_name
        return prefill

    if action == "update_stock_location_parent":
        patterns = (
            r"\bmake\s+(?P<child>.+?)\s+child\s+of\s+(?P<parent>[^.]+)",
            r"\bset\s+parent\s+of\s+(?P<child>.+?)\s+to\s+(?P<parent>[^.]+)",
            r"\bassign\s+(?P<parent>.+?)\s+as\s+parent\s+of\s+(?P<child>[^.]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            child_name = _clean_extracted_phrase(match.group("child"))
            parent_name = _clean_extracted_phrase(match.group("parent"))
            if child_name:
                prefill["location_name"] = child_name
            if parent_name:
                prefill["parent_location_name"] = parent_name
            if prefill:
                return prefill
        return prefill

    return prefill


async def _load_lookup_options_by_tool_name(
    lookup_tool: str,
    *,
    tool_specs: list[ToolSpec],
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
) -> list[dict[str, Any]]:
    relation_spec = next(
        (item for item in _relation_lookup_specs(tool_specs) if item.get("lookup_tool") == lookup_tool),
        None,
    )
    if relation_spec is None:
        return []
    return await _load_relation_options(
        relation_spec,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
        cache={},
    )


async def _load_stock_location_type_options(
    *,
    tool_names: set[str],
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
) -> list[dict[str, Any]]:
    if "inventory.list_stock_location_types" not in tool_names:
        return []
    try:
        output = await tool_executor.call_tool(
            name="inventory.list_stock_location_types",
            arguments={"limit": 25},
            ctx=tool_ctx,
        )
    except Exception:
        return []
    coerced = _coerce_mapping_from_tool_output(output)
    if not isinstance(coerced, dict):
        return []
    results = coerced.get("results")
    if not isinstance(results, list):
        return []
    options: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        name = _first_string(item, ["name", "label"])
        if not name:
            continue
        options.append({"value": name, "label": name})
    return options


def _inventory_setup_prefill_option_value(options: list[dict[str, Any]], desired_name: str | None) -> Any:
    desired = re.sub(r"\s+", " ", str(desired_name or "").strip().lower())
    if not desired:
        return None
    for option in options:
        if not isinstance(option, dict):
            continue
        label = re.sub(r"\s+", " ", str(option.get("label") or "").strip().lower())
        value = re.sub(r"\s+", " ", str(option.get("value") or "").strip().lower())
        if desired == label or desired == value or desired in label:
            return option.get("value")
    return None


async def _inventory_setup_dynamic_form_payload(
    *,
    action: str,
    text: str,
    tool_names: set[str],
    tool_specs: list[ToolSpec],
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
) -> dict[str, Any] | None:
    prefill = _parse_inventory_setup_prefill_from_text(action, text)
    stock_location_options: list[dict[str, Any]] = []
    inventory_category_options: list[dict[str, Any]] = []
    location_type_options: list[dict[str, Any]] = []

    if action in {"create_stock_location", "update_stock_location_parent"}:
        stock_location_options = await _load_lookup_options_by_tool_name(
            "inventory.list_stock_locations" if "inventory.list_stock_locations" in tool_names else "inventory.search_stock_locations",
            tool_specs=tool_specs,
            tool_executor=tool_executor,
            tool_ctx=tool_ctx,
        )
    if action == "create_inventory_item":
        inventory_category_options = await _load_lookup_options_by_tool_name(
            "inventory.list_inventory_categories",
            tool_specs=tool_specs,
            tool_executor=tool_executor,
            tool_ctx=tool_ctx,
        )
    if action == "create_stock_location":
        location_type_options = await _load_stock_location_type_options(
            tool_names=tool_names,
            tool_executor=tool_executor,
            tool_ctx=tool_ctx,
        )

    if action == "create_inventory_item":
        fields = [
            {
                "name": "default_inventory_name",
                "type": "text",
                "label": "Inventory Name",
                "required": True,
                "placeholder": "Main Inventory",
            },
            {
                "name": "inventory_description",
                "type": "textarea",
                "label": "Inventory Description",
                "required": False,
                "placeholder": "Primary stock ledger for the business.",
            },
            {
                "name": "inventory_category_id",
                "type": "select",
                "label": "Inventory Category",
                "required": False,
                "options": inventory_category_options,
                "placeholder": "Select a category if this inventory should belong to one",
            },
        ]
        current_values = {
            "default_inventory_name": prefill.get("default_inventory_name"),
            "inventory_description": prefill.get("inventory_description"),
            "inventory_category_id": _inventory_setup_prefill_option_value(
                inventory_category_options,
                str(prefill.get("inventory_category_name") or "").strip() or None,
            ),
        }
        description = (
            "I translated your request into an inventory setup form. Confirm or edit the details before I create anything. "
            "Category is optional and will be left blank unless you choose one."
        )
    elif action == "create_stock_location":
        fields = [
            {
                "name": "location_name",
                "type": "text",
                "label": "Location Name",
                "required": True,
                "placeholder": "Returns Rack",
            },
            {
                "name": "location_type_name",
                "type": "select",
                "label": "Location Type",
                "required": False,
                "options": location_type_options,
                "placeholder": "Select a location type",
            },
            {
                "name": "parent_id",
                "type": "select",
                "label": "Parent Location",
                "required": False,
                "options": stock_location_options,
                "placeholder": "Select a parent location",
            },
            {
                "name": "structural",
                "type": "boolean",
                "label": "Structural Location",
                "required": False,
            },
        ]
        current_values = {
            "location_name": prefill.get("location_name"),
            "location_type_name": str(prefill.get("location_type_name") or "").strip() or None,
            "parent_id": _inventory_setup_prefill_option_value(
                stock_location_options,
                str(prefill.get("parent_location_name") or "").strip() or None,
            ),
        }
        description = "Confirm the stock location details before I create it."
    elif action == "update_stock_location_parent":
        fields = [
            {
                "name": "location_id",
                "type": "select",
                "label": "Location to Update",
                "required": True,
                "options": stock_location_options,
                "placeholder": "Select the location you want to update",
            },
            {
                "name": "parent_id",
                "type": "select",
                "label": "New Parent Location",
                "required": True,
                "options": stock_location_options,
                "placeholder": "Select the new parent location",
            },
        ]
        current_values = {
            "location_id": _inventory_setup_prefill_option_value(
                stock_location_options,
                str(prefill.get("location_name") or "").strip() or None,
            ),
            "parent_id": _inventory_setup_prefill_option_value(
                stock_location_options,
                str(prefill.get("parent_location_name") or "").strip() or None,
            ),
        }
        description = "Choose the child location and the parent location, then I will apply the relationship update."
    else:
        return None

    current_values = {
        key: value
        for key, value in current_values.items()
        if value not in (None, "", [], {})
    }
    payload = {
        "interaction_type": "dynamic_form",
        "title": _inventory_setup_action_label(action),
        "description": description,
        "fields": fields,
        "current_values": current_values,
    }
    return _with_interaction_metadata(
        payload,
        workflow="inventory_setup_mutation",
        workflow_stage="form",
        mutation_action=action,
    )


def _inventory_setup_form_response_data(response: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    return data if isinstance(data, dict) else {}


def _inventory_setup_execute_action(
    *,
    action: str,
    form_data: dict[str, Any],
    tool_specs: list[ToolSpec],
) -> tuple[str, dict[str, Any], list[str]]:
    if action == "create_inventory_item":
        operation = _build_inventory_operation(
            tool_specs=tool_specs,
            company_context=None,
            inventory_name=str(form_data.get("default_inventory_name") or "").strip(),
            inventory_description=str(form_data.get("inventory_description") or "").strip() or None,
            related_location_id=None,
            related_location_name=None,
            category_id=str(form_data.get("inventory_category_id") or "").strip() or None,
            category_name=None,
            category_ref=None,
        )
        return operation["tool_name"], operation["arguments"], list(operation.get("missing_required") or [])

    if action == "create_stock_location":
        operation = _build_stock_location_operation(
            tool_specs=tool_specs,
            company_context=None,
            location_name=str(form_data.get("location_name") or "").strip(),
            location_type=str(form_data.get("location_type_name") or "").strip() or None,
            primary=False,
            structural=bool(form_data.get("structural")),
            parent_location_id=str(form_data.get("parent_id") or "").strip() or None,
        )
        return operation["tool_name"], operation["arguments"], list(operation.get("missing_required") or [])

    if action == "update_stock_location_parent":
        spec = _tool_spec_by_name(tool_specs, "inventory.update_stock_location")
        payload_spec = _nested_object_tool_spec(spec, "payload")
        arguments: dict[str, Any] = {}
        _set_schema_arg(arguments, spec, ["location_id", "locationId"], str(form_data.get("location_id") or "").strip() or None)
        payload_arguments: dict[str, Any] = {}
        _set_schema_arg(payload_arguments, payload_spec, ["parent_id", "parentId"], str(form_data.get("parent_id") or "").strip() or None)
        if payload_arguments:
            arguments["payload"] = payload_arguments
        filtered = _filtered_tool_arguments(spec, arguments)
        return "inventory.update_stock_location", filtered, _missing_required_arguments(spec, filtered)

    return "", {}, ["unsupported_action"]


def _product_catalog_admin_action_from_text(text: str) -> str | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    if "product" in normalized and any(token in normalized for token in ("update", "edit", "rename", "change")):
        return "update_product"
    if "product" in normalized and any(token in normalized for token in ("create", "add", "new", "set up", "setup")):
        return "create_product"
    return None


def _inventory_fulfillment_action_from_text(text: str) -> str | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    if any(token in normalized for token in ("transfer", "move stock", "move inventory", "relocate")):
        return "transfer_location_stock"
    if any(token in normalized for token in ("adjust", "increase stock", "decrease stock", "remove stock", "add stock")):
        return "adjust_inventory_item_stock"
    return None


def _product_catalog_admin_action_label(action: str) -> str:
    mapping = {
        "create_product": "Create Product",
        "update_product": "Update Product",
    }
    return mapping.get(action, action.replace("_", " ").title())


def _inventory_fulfillment_action_label(action: str) -> str:
    mapping = {
        "adjust_inventory_item_stock": "Adjust Inventory Stock",
        "transfer_location_stock": "Transfer Stock",
    }
    return mapping.get(action, action.replace("_", " ").title())


def _extract_decimal_text(text: str) -> str | None:
    match = re.search(r"\b(\d+(?:\.\d+)?)\b", text)
    return match.group(1) if match else None


def _parse_product_catalog_admin_prefill_from_text(action: str, text: str) -> dict[str, Any]:
    prefill: dict[str, Any] = {}
    name = _extract_first_named_value(
        text,
        (
            r"(?:^|\n)\s*product\s+name\s*[:=-]\s*(?P<value>[^\n]+)",
            r"\b(?:create|add|new|update|edit)\s+(?:a\s+)?product\s+(?:called|named)?\s*(?P<value>[^,\n.]+)",
            r"\b(?:for|of)\s+product\s+(?P<value>[^,\n.]+)",
        ),
    )
    if name:
        prefill["name"] = name
    category_name = _extract_first_named_value(
        text,
        (
            r"(?:^|\n)\s*(?:product\s+)?category\s*[:=-]\s*(?P<value>[^\n]+)",
            r"\bcategory\s+(?:is|should be|as)\s+(?P<value>[^,\n.]+)",
        ),
    )
    if category_name:
        prefill["category_name"] = category_name
    description = _extract_first_named_value(
        text,
        (
            r"(?:^|\n)\s*description\s*[:=-]\s*(?P<value>[^\n]+)",
        ),
    )
    if description:
        prefill["description"] = description
    price = _extract_decimal_text(text)
    if price and any(token in _normalize_user_text(text) for token in ("price", "base price", "sell for", "selling price")):
        prefill["base_price"] = price
    if "quick sale" in _normalize_user_text(text):
        prefill["quick_sale"] = True
    return prefill


def _parse_inventory_fulfillment_prefill_from_text(action: str, text: str) -> dict[str, Any]:
    prefill: dict[str, Any] = {}
    item_name = _extract_first_named_value(
        text,
        (
            r"(?:^|\n)\s*(?:inventory\s+)?item\s*[:=-]\s*(?P<value>[^\n]+)",
            r"\b(?:transfer|adjust|move)\s+(?P<value>[^,\n]+?)\s+(?:from|to|by|into)\b",
        ),
    )
    if item_name:
        prefill["inventory_item_name"] = item_name
    from_location = _extract_first_named_value(
        text,
        (
            r"(?:^|\n)\s*from\s+location\s*[:=-]\s*(?P<value>[^\n]+)",
            r"\bfrom\s+(?P<value>.+?)\s+to\b",
        ),
    )
    if from_location:
        prefill["from_location_name"] = from_location
    to_location = _extract_first_named_value(
        text,
        (
            r"(?:^|\n)\s*to\s+location\s*[:=-]\s*(?P<value>[^\n]+)",
            r"\bto\s+(?P<value>.+?)(?:\s+quantity\b|$|[.,])",
        ),
    )
    if to_location:
        prefill["to_location_name"] = to_location
    quantity = _extract_decimal_text(text)
    if quantity:
        prefill["quantity"] = quantity
    normalized = _normalize_user_text(text)
    if action == "adjust_inventory_item_stock":
        if any(token in normalized for token in ("remove", "decrease", "reduce", "deduct")):
            prefill["adjustment_type"] = "remove"
        elif any(token in normalized for token in ("add", "increase", "restock")):
            prefill["adjustment_type"] = "add"
    return prefill


async def _product_catalog_admin_dynamic_form_payload(
    *,
    action: str,
    text: str,
    tool_specs: list[ToolSpec],
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
) -> dict[str, Any] | None:
    prefill = _parse_product_catalog_admin_prefill_from_text(action, text)
    category_options = await _load_lookup_options_by_tool_name(
        "product.get_product_categories",
        tool_specs=tool_specs,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
    )
    product_options = await _load_lookup_options_by_tool_name(
        "product.search_products",
        tool_specs=tool_specs,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
    )
    common_fields = [
        {"name": "name", "type": "text", "label": "Product Name", "required": True, "placeholder": "Men's Oxford Shirt"},
        {"name": "description", "type": "textarea", "label": "Description", "required": False, "placeholder": "Short product description"},
        {"name": "category_ref_id", "type": "select", "label": "Product Category", "required": False, "options": category_options, "placeholder": "Select a category"},
        {"name": "base_price", "type": "text", "label": "Base Price", "required": False, "placeholder": "25000"},
        {"name": "quick_sale", "type": "boolean", "label": "Quick Sale", "required": False},
        {"name": "pos_category", "type": "text", "label": "POS Category", "required": False, "placeholder": "Clothing"},
    ]
    current_values = {
        "name": prefill.get("name"),
        "description": prefill.get("description"),
        "category_ref_id": _inventory_setup_prefill_option_value(category_options, prefill.get("category_name")),
        "base_price": prefill.get("base_price"),
        "quick_sale": prefill.get("quick_sale"),
    }
    if action == "update_product":
        fields = [
            {"name": "product_id", "type": "select", "label": "Product to Update", "required": True, "options": product_options, "placeholder": "Select a product"},
            *common_fields,
        ]
        current_values["product_id"] = _inventory_setup_prefill_option_value(product_options, prefill.get("name"))
        description = "Confirm the product changes before I update it."
    elif action == "create_product":
        fields = common_fields
        description = "I translated your request into a product form. Confirm or edit it before I create anything."
    else:
        return None
    return _with_interaction_metadata(
        {
            "interaction_type": "dynamic_form",
            "title": _product_catalog_admin_action_label(action),
            "description": description,
            "fields": fields,
            "current_values": {key: value for key, value in current_values.items() if value not in (None, "", [], {})},
        },
        workflow="product_catalog_admin_mutation",
        workflow_stage="form",
        mutation_action=action,
    )


def _product_catalog_admin_execute_action(
    *,
    action: str,
    form_data: dict[str, Any],
    tool_specs: list[ToolSpec],
) -> tuple[str, dict[str, Any], list[str]]:
    tool_name = "product.create_product" if action == "create_product" else "product.update_product"
    spec = _tool_spec_by_name(tool_specs, tool_name)
    payload_spec = _nested_object_tool_spec(spec, "payload")
    arguments: dict[str, Any] = {}
    if action == "update_product":
        _set_schema_arg(arguments, spec, ["product_id", "productId"], str(form_data.get("product_id") or "").strip() or None)
    payload_arguments: dict[str, Any] = {}
    _set_schema_arg(payload_arguments, payload_spec, ["name"], str(form_data.get("name") or "").strip() or None)
    _set_schema_arg(payload_arguments, payload_spec, ["description"], str(form_data.get("description") or "").strip() or None)
    _set_schema_arg(payload_arguments, payload_spec, ["category_ref_id", "categoryRefId"], str(form_data.get("category_ref_id") or "").strip() or None)
    _set_schema_arg(payload_arguments, payload_spec, ["base_price", "basePrice"], str(form_data.get("base_price") or "").strip() or None)
    if "quick_sale" in form_data:
        _set_schema_arg(payload_arguments, payload_spec, ["quick_sale", "quickSale"], bool(form_data.get("quick_sale")))
    _set_schema_arg(payload_arguments, payload_spec, ["pos_category", "posCategory"], str(form_data.get("pos_category") or "").strip() or None)
    if payload_arguments:
        arguments["payload"] = payload_arguments
    filtered = _filtered_tool_arguments(spec, arguments)
    return tool_name, filtered, _missing_required_arguments(spec, filtered)


async def _inventory_fulfillment_dynamic_form_payload(
    *,
    action: str,
    text: str,
    tool_names: set[str],
    tool_specs: list[ToolSpec],
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
) -> dict[str, Any] | None:
    prefill = _parse_inventory_fulfillment_prefill_from_text(action, text)
    stock_location_lookup = "inventory.list_stock_locations" if "inventory.list_stock_locations" in tool_names else "inventory.search_stock_locations"
    inventory_item_lookup = "inventory.list_inventory_items" if "inventory.list_inventory_items" in tool_names else "inventory.search_inventory_items"
    location_options = await _load_lookup_options_by_tool_name(
        stock_location_lookup,
        tool_specs=tool_specs,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
    )
    inventory_item_options = await _load_lookup_options_by_tool_name(
        inventory_item_lookup,
        tool_specs=tool_specs,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
    )
    if action == "transfer_location_stock":
        fields = [
            {"name": "inventory_item_id", "type": "select", "label": "Inventory Item", "required": True, "options": inventory_item_options, "placeholder": "Select an inventory item"},
            {"name": "from_location_id", "type": "select", "label": "Source Location", "required": True, "options": location_options, "placeholder": "Select the source location"},
            {"name": "to_location_id", "type": "select", "label": "Destination Location", "required": True, "options": location_options, "placeholder": "Select the destination location"},
            {"name": "quantity", "type": "text", "label": "Quantity", "required": True, "placeholder": "10"},
            {"name": "reason", "type": "text", "label": "Reason", "required": False, "placeholder": "Rebalancing stock"},
            {"name": "notes", "type": "textarea", "label": "Notes", "required": False, "placeholder": "Optional transfer notes"},
        ]
        current_values = {
            "inventory_item_id": _inventory_setup_prefill_option_value(inventory_item_options, prefill.get("inventory_item_name")),
            "from_location_id": _inventory_setup_prefill_option_value(location_options, prefill.get("from_location_name")),
            "to_location_id": _inventory_setup_prefill_option_value(location_options, prefill.get("to_location_name")),
            "quantity": prefill.get("quantity"),
        }
        description = "Confirm the stock transfer details before I move anything."
    elif action == "adjust_inventory_item_stock":
        fields = [
            {"name": "inventory_item_id", "type": "select", "label": "Inventory Item", "required": True, "options": inventory_item_options, "placeholder": "Select an inventory item"},
            {"name": "stock_location_id", "type": "select", "label": "Stock Location", "required": False, "options": location_options, "placeholder": "Select a location if the adjustment is location-specific"},
            {"name": "quantity", "type": "text", "label": "Quantity Change", "required": True, "placeholder": "5"},
            {"name": "adjustment_type", "type": "select", "label": "Adjustment Type", "required": True, "options": [{"value": "add", "label": "Add"}, {"value": "remove", "label": "Remove"}], "placeholder": "Select adjustment type"},
            {"name": "reason", "type": "text", "label": "Reason", "required": False, "placeholder": "Cycle count correction"},
            {"name": "notes", "type": "textarea", "label": "Notes", "required": False, "placeholder": "Optional adjustment notes"},
        ]
        current_values = {
            "inventory_item_id": _inventory_setup_prefill_option_value(inventory_item_options, prefill.get("inventory_item_name")),
            "stock_location_id": _inventory_setup_prefill_option_value(location_options, prefill.get("from_location_name")),
            "quantity": prefill.get("quantity"),
            "adjustment_type": prefill.get("adjustment_type"),
        }
        description = "Confirm the stock adjustment details before I apply it."
    else:
        return None
    return _with_interaction_metadata(
        {
            "interaction_type": "dynamic_form",
            "title": _inventory_fulfillment_action_label(action),
            "description": description,
            "fields": fields,
            "current_values": {key: value for key, value in current_values.items() if value not in (None, "", [], {})},
        },
        workflow="inventory_fulfillment_mutation",
        workflow_stage="form",
        mutation_action=action,
    )


def _inventory_fulfillment_execute_action(
    *,
    action: str,
    form_data: dict[str, Any],
    tool_specs: list[ToolSpec],
) -> tuple[str, dict[str, Any], list[str]]:
    if action == "transfer_location_stock":
        spec = _tool_spec_by_name(tool_specs, "inventory.transfer_location_stock")
        payload_spec = _nested_object_tool_spec(spec, "payload")
        arguments: dict[str, Any] = {}
        from_location_id = str(form_data.get("from_location_id") or "").strip() or None
        _set_schema_arg(arguments, spec, ["location_id", "locationId"], from_location_id)
        payload_arguments: dict[str, Any] = {}
        transfer_line = {
            "inventory_item_id": str(form_data.get("inventory_item_id") or "").strip() or None,
            "from_location_id": from_location_id,
            "to_location_id": str(form_data.get("to_location_id") or "").strip() or None,
            "quantity": str(form_data.get("quantity") or "").strip() or None,
            "notes": str(form_data.get("notes") or "").strip() or None,
        }
        _set_schema_arg(payload_arguments, payload_spec, ["transfers"], [transfer_line])
        _set_schema_arg(payload_arguments, payload_spec, ["reason"], str(form_data.get("reason") or "").strip() or None)
        _set_schema_arg(payload_arguments, payload_spec, ["notes"], str(form_data.get("notes") or "").strip() or None)
        if payload_arguments:
            arguments["payload"] = payload_arguments
        filtered = _filtered_tool_arguments(spec, arguments)
        return "inventory.transfer_location_stock", filtered, _missing_required_arguments(spec, filtered)

    if action == "adjust_inventory_item_stock":
        spec = _tool_spec_by_name(tool_specs, "inventory.adjust_inventory_item_stock")
        payload_spec = _nested_object_tool_spec(spec, "payload")
        arguments: dict[str, Any] = {}
        _set_schema_arg(arguments, spec, ["inventory_item_id", "inventoryItemId"], str(form_data.get("inventory_item_id") or "").strip() or None)
        payload_arguments: dict[str, Any] = {}
        adjustment_line = {
            "inventory_item_id": str(form_data.get("inventory_item_id") or "").strip() or None,
            "stock_location_id": str(form_data.get("stock_location_id") or "").strip() or None,
            "quantity": str(form_data.get("quantity") or "").strip() or None,
            "adjustment_type": str(form_data.get("adjustment_type") or "").strip() or None,
            "notes": str(form_data.get("notes") or "").strip() or None,
        }
        _set_schema_arg(payload_arguments, payload_spec, ["adjustments"], [adjustment_line])
        _set_schema_arg(payload_arguments, payload_spec, ["reason"], str(form_data.get("reason") or "").strip() or None)
        _set_schema_arg(payload_arguments, payload_spec, ["notes"], str(form_data.get("notes") or "").strip() or None)
        if payload_arguments:
            arguments["payload"] = payload_arguments
        filtered = _filtered_tool_arguments(spec, arguments)
        return "inventory.adjust_inventory_item_stock", filtered, _missing_required_arguments(spec, filtered)

    return "", {}, ["unsupported_action"]


def _inventory_procurement_action_from_text(text: str) -> str | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    if "purchase order" in normalized and (
        any(token in normalized for token in ("add item", "add line item", "add inventory", "add sku", "add product"))
        or ("add " in normalized and " to purchase order" in normalized)
    ):
        return "add_purchase_order_line_item"
    return None


def _product_merchandising_action_from_text(text: str) -> str | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    if any(
        token in normalized
        for token in ("quick sale", "featured", "merchandis", "pos category", "product category")
    ):
        return "update_product_merchandising"
    return None


def _product_pricing_action_from_text(text: str) -> str | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    if "pricing strategy" in normalized or "margin strategy" in normalized or "price strategy" in normalized:
        return "create_pricing_strategy"
    if "pricing rule" in normalized or "discount rule" in normalized or "promo rule" in normalized:
        return "create_pricing_rule"
    return None


def _inventory_procurement_action_label(action: str) -> str:
    mapping = {
        "add_purchase_order_line_item": "Add Purchase Order Line Item",
    }
    return mapping.get(action, action.replace("_", " ").title())


def _product_merchandising_action_label(action: str) -> str:
    mapping = {
        "update_product_merchandising": "Update Product Merchandising",
    }
    return mapping.get(action, action.replace("_", " ").title())


def _product_pricing_action_label(action: str) -> str:
    mapping = {
        "create_pricing_strategy": "Create Pricing Strategy",
        "create_pricing_rule": "Create Pricing Rule",
    }
    return mapping.get(action, action.replace("_", " ").title())


def _parse_inventory_procurement_prefill_from_text(action: str, text: str) -> dict[str, Any]:
    _ = action
    prefill: dict[str, Any] = {}
    order_name = _extract_first_named_value(
        text,
        (
            r"(?:^|\n)\s*(?:purchase\s+order|po)\s*[:=-]\s*(?P<value>[^\n]+)",
            r"\bto\s+purchase\s+order\s+(?P<value>[^,\n.]+)",
        ),
    )
    if order_name:
        prefill["purchase_order_name"] = order_name
    item_name = _extract_first_named_value(
        text,
        (
            r"(?:^|\n)\s*(?:inventory\s+item|item|sku)\s*[:=-]\s*(?P<value>[^\n]+)",
            r"\badd\s+(?P<value>.+?)\s+to\s+purchase\s+order\b",
        ),
    )
    if item_name:
        prefill["inventory_item_name"] = item_name
    quantity = _extract_first_named_value(
        text,
        (
            r"(?:^|\n)\s*quantity\s*[:=-]?\s*(?P<value>\d+(?:\.\d+)?)",
            r"\bqty\s*[:=-]?\s*(?P<value>\d+(?:\.\d+)?)",
        ),
    )
    if not quantity:
        quantity = _extract_decimal_text(text)
    if quantity:
        prefill["quantity"] = quantity
    unit_price = _extract_first_named_value(
        text,
        (
            r"(?:^|\n)\s*(?:unit\s+price|price)\s*[:=-]\s*(?P<value>[^\n]+)",
        ),
    )
    if unit_price:
        prefill["unit_price"] = _clean_extracted_phrase(unit_price)
    return prefill


def _parse_product_merchandising_prefill_from_text(action: str, text: str) -> dict[str, Any]:
    _ = action
    prefill = _parse_product_catalog_admin_prefill_from_text("update_product", text)
    normalized = _normalize_user_text(text)
    if "quick sale" in normalized:
        if any(token in normalized for token in ("disable", "turn off", "remove", "not")):
            prefill["quick_sale"] = False
        else:
            prefill["quick_sale"] = True
    if "featured" in normalized:
        if any(token in normalized for token in ("unfeature", "disable", "remove", "not featured")):
            prefill["is_featured"] = False
        else:
            prefill["is_featured"] = True
    pos_category = _extract_first_named_value(
        text,
        (
            r"(?:^|\n)\s*pos\s+category\s*[:=-]\s*(?P<value>[^\n]+)",
        ),
    )
    if pos_category:
        prefill["pos_category"] = pos_category
    return prefill


def _parse_product_pricing_prefill_from_text(action: str, text: str) -> dict[str, Any]:
    prefill: dict[str, Any] = {}
    name = _extract_first_named_value(
        text,
        (
            r"(?:^|\n)\s*(?:strategy|rule)\s+name\s*[:=-]\s*(?P<value>[^\n]+)",
            r"\b(?:create|add)\s+(?:a\s+)?(?:pricing\s+strategy|pricing\s+rule)\s+(?:called|named)?\s*(?P<value>[^,\n.]+)",
        ),
    )
    if name:
        prefill["name"] = name
    if action == "create_pricing_strategy":
        normalized = _normalize_user_text(text)
        if "margin" in normalized:
            prefill["strategy"] = "margin"
        elif "multiplier" in normalized:
            prefill["strategy"] = "multiplier"
        elif "fixed" in normalized:
            prefill["strategy"] = "fixed"
        elif "tier" in normalized:
            prefill["strategy"] = "tiered"
        elif "dynamic" in normalized:
            prefill["strategy"] = "dynamic"
        margin = _extract_first_named_value(text, (r"(?:margin|margin percentage)\s*[:=-]?\s*(?P<value>\d+(?:\.\d+)?)",))
        if margin:
            prefill["margin_percentage"] = margin
    else:
        normalized = _normalize_user_text(text)
        if "promo" in normalized:
            prefill["rule_type"] = "PROMO"
        elif "season" in normalized:
            prefill["rule_type"] = "SEASONAL"
        elif "volume" in normalized:
            prefill["rule_type"] = "VOLUME"
        elif "clearance" in normalized:
            prefill["rule_type"] = "CLEARANCE"
        elif "bundle" in normalized:
            prefill["rule_type"] = "BUNDLE"
        else:
            prefill["rule_type"] = "PROMO"
        value = _extract_first_named_value(text, (r"(?:discount|value)\s*[:=-]?\s*(?P<value>\d+(?:\.\d+)?)",))
        if value:
            prefill["value"] = value
        category_name = _extract_first_named_value(
            text,
            (
                r"(?:^|\n)\s*(?:product\s+)?category\s*[:=-]\s*(?P<value>[^\n]+)",
            ),
        )
        if category_name:
            prefill["category_name"] = category_name
    product_name = _extract_first_named_value(
        text,
        (
            r"(?:^|\n)\s*product\s*[:=-]\s*(?P<value>[^\n]+)",
            r"\bfor\s+product\s+(?P<value>[^,\n.]+)",
        ),
    )
    if product_name:
        prefill["product_name"] = product_name
    return prefill


async def _inventory_procurement_dynamic_form_payload(
    *,
    action: str,
    text: str,
    tool_names: set[str],
    tool_specs: list[ToolSpec],
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
) -> dict[str, Any] | None:
    prefill = _parse_inventory_procurement_prefill_from_text(action, text)
    po_options = await _load_lookup_options_by_tool_name(
        "inventory.search_purchase_orders",
        tool_specs=tool_specs,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
    )
    inventory_item_lookup = "inventory.list_inventory_items" if "inventory.list_inventory_items" in tool_names else "inventory.search_inventory_items"
    inventory_item_options = await _load_lookup_options_by_tool_name(
        inventory_item_lookup,
        tool_specs=tool_specs,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
    )
    if action != "add_purchase_order_line_item":
        return None
    return _with_interaction_metadata(
        {
            "interaction_type": "dynamic_form",
            "title": _inventory_procurement_action_label(action),
            "description": "Confirm the purchase-order line item details before I add it.",
            "fields": [
                {"name": "purchase_order_id", "type": "select", "label": "Purchase Order", "required": True, "options": po_options, "placeholder": "Select a purchase order"},
                {"name": "inventory_item_id", "type": "select", "label": "Inventory Item", "required": True, "options": inventory_item_options, "placeholder": "Select an inventory item"},
                {"name": "quantity", "type": "text", "label": "Quantity", "required": True, "placeholder": "20"},
                {"name": "unit_price", "type": "text", "label": "Unit Price", "required": True, "placeholder": "15000"},
                {"name": "description", "type": "textarea", "label": "Description", "required": False, "placeholder": "Optional line-item description"},
            ],
            "current_values": {
                key: value
                for key, value in {
                    "purchase_order_id": _inventory_setup_prefill_option_value(po_options, prefill.get("purchase_order_name")),
                    "inventory_item_id": _inventory_setup_prefill_option_value(inventory_item_options, prefill.get("inventory_item_name")),
                    "quantity": prefill.get("quantity"),
                    "unit_price": prefill.get("unit_price"),
                }.items()
                if value not in (None, "", [], {})
            },
        },
        workflow="inventory_procurement_mutation",
        workflow_stage="form",
        mutation_action=action,
    )


def _inventory_procurement_execute_action(
    *,
    action: str,
    form_data: dict[str, Any],
    tool_specs: list[ToolSpec],
) -> tuple[str, dict[str, Any], list[str]]:
    if action != "add_purchase_order_line_item":
        return "", {}, ["unsupported_action"]
    spec = _tool_spec_by_name(tool_specs, "inventory.add_purchase_order_line_item")
    payload_spec = _nested_object_tool_spec(spec, "payload")
    arguments: dict[str, Any] = {}
    _set_schema_arg(arguments, spec, ["purchase_order_id", "purchaseOrderId"], str(form_data.get("purchase_order_id") or "").strip() or None)
    payload_arguments: dict[str, Any] = {}
    _set_schema_arg(payload_arguments, payload_spec, ["inventory_item_id", "inventoryItemId"], str(form_data.get("inventory_item_id") or "").strip() or None)
    _set_schema_arg(payload_arguments, payload_spec, ["quantity"], str(form_data.get("quantity") or "").strip() or None)
    _set_schema_arg(payload_arguments, payload_spec, ["unit_price", "unitPrice"], str(form_data.get("unit_price") or "").strip() or None)
    _set_schema_arg(payload_arguments, payload_spec, ["description"], str(form_data.get("description") or "").strip() or None)
    if payload_arguments:
        arguments["payload"] = payload_arguments
    filtered = _filtered_tool_arguments(spec, arguments)
    return "inventory.add_purchase_order_line_item", filtered, _missing_required_arguments(spec, filtered)


async def _product_merchandising_dynamic_form_payload(
    *,
    action: str,
    text: str,
    tool_specs: list[ToolSpec],
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
) -> dict[str, Any] | None:
    prefill = _parse_product_merchandising_prefill_from_text(action, text)
    product_options = await _load_lookup_options_by_tool_name(
        "product.search_products",
        tool_specs=tool_specs,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
    )
    category_options = await _load_lookup_options_by_tool_name(
        "product.get_product_categories",
        tool_specs=tool_specs,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
    )
    if action != "update_product_merchandising":
        return None
    return _with_interaction_metadata(
        {
            "interaction_type": "dynamic_form",
            "title": _product_merchandising_action_label(action),
            "description": "Confirm the merchandising changes before I update the product.",
            "fields": [
                {"name": "product_id", "type": "select", "label": "Product", "required": True, "options": product_options, "placeholder": "Select a product"},
                {"name": "category_ref_id", "type": "select", "label": "Product Category", "required": False, "options": category_options, "placeholder": "Select a category"},
                {"name": "quick_sale", "type": "boolean", "label": "Quick Sale", "required": False},
                {"name": "is_featured", "type": "boolean", "label": "Featured", "required": False},
                {"name": "pos_category", "type": "text", "label": "POS Category", "required": False, "placeholder": "Clothing"},
            ],
            "current_values": {
                key: value
                for key, value in {
                    "product_id": _inventory_setup_prefill_option_value(product_options, prefill.get("name")),
                    "category_ref_id": _inventory_setup_prefill_option_value(category_options, prefill.get("category_name")),
                    "quick_sale": prefill.get("quick_sale"),
                    "is_featured": prefill.get("is_featured"),
                    "pos_category": prefill.get("pos_category"),
                }.items()
                if value not in (None, "", [], {})
            },
        },
        workflow="product_merchandising_mutation",
        workflow_stage="form",
        mutation_action=action,
    )


def _product_merchandising_execute_action(
    *,
    action: str,
    form_data: dict[str, Any],
    tool_specs: list[ToolSpec],
) -> tuple[str, dict[str, Any], list[str]]:
    if action != "update_product_merchandising":
        return "", {}, ["unsupported_action"]
    spec = _tool_spec_by_name(tool_specs, "product.update_product")
    payload_spec = _nested_object_tool_spec(spec, "payload")
    arguments: dict[str, Any] = {}
    _set_schema_arg(arguments, spec, ["product_id", "productId"], str(form_data.get("product_id") or "").strip() or None)
    payload_arguments: dict[str, Any] = {}
    _set_schema_arg(payload_arguments, payload_spec, ["category_ref_id", "categoryRefId"], str(form_data.get("category_ref_id") or "").strip() or None)
    if "quick_sale" in form_data:
        _set_schema_arg(payload_arguments, payload_spec, ["quick_sale", "quickSale"], bool(form_data.get("quick_sale")))
    if "is_featured" in form_data:
        _set_schema_arg(payload_arguments, payload_spec, ["is_featured", "isFeatured"], bool(form_data.get("is_featured")))
    _set_schema_arg(payload_arguments, payload_spec, ["pos_category", "posCategory"], str(form_data.get("pos_category") or "").strip() or None)
    if payload_arguments:
        arguments["payload"] = payload_arguments
    filtered = _filtered_tool_arguments(spec, arguments)
    return "product.update_product", filtered, _missing_required_arguments(spec, filtered)


async def _product_pricing_dynamic_form_payload(
    *,
    action: str,
    text: str,
    tool_specs: list[ToolSpec],
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
) -> dict[str, Any] | None:
    prefill = _parse_product_pricing_prefill_from_text(action, text)
    product_options = await _load_lookup_options_by_tool_name(
        "product.search_products",
        tool_specs=tool_specs,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
    )
    category_options = await _load_lookup_options_by_tool_name(
        "product.get_product_categories",
        tool_specs=tool_specs,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
    )
    if action == "create_pricing_strategy":
        return _with_interaction_metadata(
            {
                "interaction_type": "dynamic_form",
                "title": _product_pricing_action_label(action),
                "description": "Confirm the pricing strategy details before I create it.",
                "fields": [
                    {"name": "name", "type": "text", "label": "Strategy Name", "required": True, "placeholder": "Fashion Margin Strategy"},
                    {"name": "strategy", "type": "select", "label": "Strategy Type", "required": True, "options": [{"value": "margin", "label": "Margin"}, {"value": "multiplier", "label": "Multiplier"}, {"value": "fixed", "label": "Fixed"}, {"value": "dynamic", "label": "Dynamic"}, {"value": "tiered", "label": "Tiered"}], "placeholder": "Select a strategy"},
                    {"name": "product_id", "type": "select", "label": "Product", "required": False, "options": product_options, "placeholder": "Select a product"},
                    {"name": "margin_percentage", "type": "text", "label": "Margin Percentage", "required": False, "placeholder": "25"},
                    {"name": "market_multiplier", "type": "text", "label": "Market Multiplier", "required": False, "placeholder": "1.2"},
                    {"name": "min_price", "type": "text", "label": "Minimum Price", "required": False, "placeholder": "10000"},
                    {"name": "max_price", "type": "text", "label": "Maximum Price", "required": False, "placeholder": "50000"},
                ],
                "current_values": {
                    key: value
                    for key, value in {
                        "name": prefill.get("name"),
                        "strategy": prefill.get("strategy"),
                        "product_id": _inventory_setup_prefill_option_value(product_options, prefill.get("product_name")),
                        "margin_percentage": prefill.get("margin_percentage"),
                    }.items()
                    if value not in (None, "", [], {})
                },
            },
            workflow="product_pricing_mutation",
            workflow_stage="form",
            mutation_action=action,
        )
    if action == "create_pricing_rule":
        return _with_interaction_metadata(
            {
                "interaction_type": "dynamic_form",
                "title": _product_pricing_action_label(action),
                "description": "Confirm the pricing rule details before I create it.",
                "fields": [
                    {"name": "name", "type": "text", "label": "Rule Name", "required": True, "placeholder": "Weekend Promo"},
                    {"name": "rule_type", "type": "select", "label": "Rule Type", "required": True, "options": [{"value": "PROMO", "label": "Promo"}, {"value": "SEASONAL", "label": "Seasonal"}, {"value": "VOLUME", "label": "Volume"}, {"value": "CLEARANCE", "label": "Clearance"}, {"value": "BUNDLE", "label": "Bundle"}], "placeholder": "Select a rule type"},
                    {"name": "product_id", "type": "select", "label": "Product", "required": False, "options": product_options, "placeholder": "Select a product"},
                    {"name": "category_ref_id", "type": "select", "label": "Product Category", "required": False, "options": category_options, "placeholder": "Select a category"},
                    {"name": "discount_type", "type": "select", "label": "Discount Type", "required": True, "options": [{"value": "PERCENTAGE", "label": "Percentage"}, {"value": "FIXED_AMOUNT", "label": "Fixed Amount"}, {"value": "FIXED_PRICE", "label": "Fixed Price"}], "placeholder": "Select a discount type"},
                    {"name": "value", "type": "text", "label": "Discount Value", "required": True, "placeholder": "10"},
                    {"name": "description", "type": "textarea", "label": "Description", "required": False, "placeholder": "Optional rule description"},
                ],
                "current_values": {
                    key: value
                    for key, value in {
                        "name": prefill.get("name"),
                        "rule_type": prefill.get("rule_type"),
                        "product_id": _inventory_setup_prefill_option_value(product_options, prefill.get("product_name")),
                        "category_ref_id": _inventory_setup_prefill_option_value(category_options, prefill.get("category_name")),
                        "value": prefill.get("value"),
                        "discount_type": "PERCENTAGE",
                    }.items()
                    if value not in (None, "", [], {})
                },
            },
            workflow="product_pricing_mutation",
            workflow_stage="form",
            mutation_action=action,
        )
    return None


def _product_pricing_execute_action(
    *,
    action: str,
    form_data: dict[str, Any],
    tool_specs: list[ToolSpec],
) -> tuple[str, dict[str, Any], list[str]]:
    tool_name = "product.create_pricing_strategy" if action == "create_pricing_strategy" else "product.create_pricing_rule"
    spec = _tool_spec_by_name(tool_specs, tool_name)
    payload_spec = _nested_object_tool_spec(spec, "payload")
    payload_arguments: dict[str, Any] = {}
    if action == "create_pricing_strategy":
        for key in ("name", "strategy", "product_id", "margin_percentage", "market_multiplier", "min_price", "max_price"):
            _set_schema_arg(payload_arguments, payload_spec, [key], str(form_data.get(key) or "").strip() or None)
    else:
        for key in ("name", "rule_type", "product_id", "category_ref_id", "discount_type", "value", "description"):
            _set_schema_arg(payload_arguments, payload_spec, [key], str(form_data.get(key) or "").strip() or None)
    arguments = {"payload": payload_arguments} if payload_arguments else {}
    filtered = _filtered_tool_arguments(spec, arguments)
    return tool_name, filtered, _missing_required_arguments(spec, filtered)


def _normalize_operation_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


def _normalized_schema_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _tool_spec_by_name(tool_specs: list[ToolSpec], name: str) -> ToolSpec | None:
    for spec in tool_specs:
        if spec.name == name:
            return spec
    return None


def _tool_schema_properties(spec: ToolSpec | None) -> dict[str, Any]:
    if spec is None or not isinstance(spec.input_schema, dict):
        return {}
    properties = spec.input_schema.get("properties")
    return properties if isinstance(properties, dict) else {}


def _tool_schema_required(spec: ToolSpec | None) -> list[str]:
    if spec is None or not isinstance(spec.input_schema, dict):
        return []
    required = spec.input_schema.get("required")
    if not isinstance(required, list):
        return []
    return [str(item).strip() for item in required if isinstance(item, str) and item.strip()]


def _classify_error_text(error_text: str | None) -> dict[str, Any]:
    raw = str(error_text or "").strip()
    lowered = raw.lower()
    if not raw:
        return {"error_kind": "unknown", "error_summary": "An unknown error occurred.", "retryable": True}
    if any(token in lowered for token in ("tlsv1_alert", "tlsv1 alert", "ssl:", "certificate verify failed", "wrong version number")):
        return {
            "error_kind": "tls",
            "error_summary": "TLS handshake failed while connecting to the upstream service.",
            "retryable": True,
        }
    if any(token in lowered for token in ("could not resolve host", "name or service not known", "nodename nor servname provided", "temporary failure in name resolution")):
        return {
            "error_kind": "dns",
            "error_summary": "The upstream service hostname could not be resolved.",
            "retryable": True,
        }
    if "timed out" in lowered or "timeout" in lowered:
        return {
            "error_kind": "timeout",
            "error_summary": "The upstream service timed out before it responded.",
            "retryable": True,
        }
    if any(
        token in lowered
        for token in (
            "requires a forwarded bearer token",
            "requires request-scoped mcp credentials",
            "requires a static token",
            "unauthorized",
            "forbidden",
            "401",
            "403",
            "authentication",
        )
    ):
        return {
            "error_kind": "auth",
            "error_summary": "The upstream service rejected the request because authentication is missing or invalid.",
            "retryable": False,
        }
    if "is not registered" in lowered or "no downstream specialist agents are registered" in lowered:
        return {
            "error_kind": "registry",
            "error_summary": "The required specialist agent is not currently visible in the registry.",
            "retryable": True,
        }
    if "unknown tool" in lowered or "not available from mcp server" in lowered:
        return {
            "error_kind": "tool_unavailable",
            "error_summary": "The required tool is not exposed by the current agent configuration.",
            "retryable": False,
        }
    return {"error_kind": "unknown", "error_summary": raw, "retryable": True}


def _service_label_from_tool_name(tool_name: str | None) -> str:
    name = str(tool_name or "").strip().lower()
    if name.startswith("inventory."):
        return "inventory"
    if name.startswith("product."):
        return "product"
    if name.startswith("users."):
        return "users"
    if name.startswith("pos."):
        return "pos"
    return "service"


def _friendly_discovery_issue_message(failure: dict[str, Any], *, tool_name: str | None = None) -> str | None:
    if not isinstance(failure, dict):
        return None
    summary = str(failure.get("error_summary") or "").strip()
    if not summary:
        summary = _classify_error_text(failure.get("error")).get("error_summary", "").strip()
    if not summary:
        return None
    label = str(
        failure.get("server_id")
        or failure.get("executor_label")
        or _service_label_from_tool_name(tool_name)
    ).strip()
    return f"{label}: {summary}"


def _classify_failed_operation(item: dict[str, Any]) -> dict[str, Any]:
    reason = str(item.get("reason") or "").strip().lower()
    tool_name = str(item.get("tool_name") or "").strip() or None
    if reason == "missing_required_arguments":
        missing = [
            str(value).strip()
            for value in item.get("missing", [])
            if isinstance(item.get("missing"), list) and str(value).strip()
        ]
        missing_text = ", ".join(missing) if missing else "required fields"
        return {
            "error_kind": "schema_mismatch",
            "error_summary": f"The tool schema requires additional fields: {missing_text}.",
            "retryable": False,
        }
    if reason == "tool_unavailable":
        discovery_failures = item.get("discovery_failures")
        if isinstance(discovery_failures, list):
            for failure in discovery_failures:
                message = _friendly_discovery_issue_message(failure, tool_name=tool_name)
                if message:
                    classified = _classify_error_text(failure.get("error"))
                    return {
                        "error_kind": classified["error_kind"],
                        "error_summary": message,
                        "retryable": bool(classified.get("retryable", True)),
                    }
        service_label = _service_label_from_tool_name(tool_name)
        return {
            "error_kind": "tool_unavailable",
            "error_summary": f"The required {service_label} tool is not currently available.",
            "retryable": False,
        }
    if reason == "tool_error":
        classified = _classify_error_text(item.get("error"))
        service_label = _service_label_from_tool_name(tool_name)
        if classified["error_kind"] == "unknown":
            return {
                "error_kind": "tool_error",
                "error_summary": f"The {service_label} operation failed: {str(item.get('error') or '').strip()}",
                "retryable": True,
            }
        return classified
    return _classify_error_text(item.get("error"))


def _nested_object_tool_spec(spec: ToolSpec | None, key: str) -> ToolSpec | None:
    properties = _tool_schema_properties(spec)
    nested = properties.get(key)
    if not isinstance(nested, dict):
        return None
    nested_properties = nested.get("properties")
    nested_required = nested.get("required")
    if not isinstance(nested_properties, dict) and not isinstance(nested_required, list):
        return None
    return ToolSpec(
        name=f"{spec.name}.{key}" if spec is not None else key,
        description="",
        input_schema={
            "type": "object",
            "properties": nested_properties if isinstance(nested_properties, dict) else {},
            "required": nested_required if isinstance(nested_required, list) else [],
        },
    )


def _match_schema_key(spec: ToolSpec | None, candidates: list[str]) -> str | None:
    if not candidates:
        return None
    properties = _tool_schema_properties(spec)
    if not properties:
        return candidates[0]

    normalized_map = {_normalized_schema_key(key): key for key in properties}
    for candidate in candidates:
        if candidate in properties:
            return candidate
        normalized = _normalized_schema_key(candidate)
        if normalized in normalized_map:
            return normalized_map[normalized]
    return None


def _set_schema_arg(arguments: dict[str, Any], spec: ToolSpec | None, candidates: list[str], value: Any) -> None:
    if value in (None, "", [], {}):
        return
    matched = _match_schema_key(spec, candidates)
    if matched:
        arguments[matched] = value


def _filtered_tool_arguments(spec: ToolSpec | None, arguments: dict[str, Any]) -> dict[str, Any]:
    properties = _tool_schema_properties(spec)
    if not properties:
        return {key: value for key, value in arguments.items() if value not in (None, "", [], {})}
    return {
        key: value
        for key, value in arguments.items()
        if key in properties and value not in (None, "", [], {})
    }


def _missing_required_arguments(spec: ToolSpec | None, arguments: dict[str, Any]) -> list[str]:
    return [key for key in _tool_schema_required(spec) if key not in arguments]


def _first_string(mapping: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _coerce_mapping_from_tool_output(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        for key in ("structuredContent", "structured_content", "data", "result"):
            nested = value.get(key)
            if isinstance(nested, dict):
                return nested
        content = value.get("content")
        if isinstance(content, list):
            nested = _coerce_mapping_from_tool_output(content)
            if isinstance(nested, dict):
                return nested
        if isinstance(content, str):
            nested = _extract_json_object_from_text(content)
            if isinstance(nested, dict):
                return nested
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                if isinstance(item.get("data"), dict):
                    return item["data"]
                if isinstance(item.get("structuredContent"), dict):
                    return item["structuredContent"]
                if isinstance(item.get("text"), str):
                    raw = _extract_json_object_from_text(item["text"])
                    if raw is not None:
                        return raw
    if isinstance(value, str):
        return _extract_json_object_from_text(value)
    return None


def _extract_company_context(value: Any) -> dict[str, Any] | None:
    obj = _coerce_mapping_from_tool_output(value)
    if not isinstance(obj, dict):
        return None

    nested_candidates = [obj]
    for key in ("company", "profile", "activeCompany", "active_company", "companyProfile", "company_profile"):
        nested = obj.get(key)
        if isinstance(nested, dict):
            nested_candidates.append(nested)

    for candidate in nested_candidates:
        identifier = _first_string(candidate, ["id", "profile_id", "profileId", "company_id", "companyId"])
        name = _first_string(candidate, ["name", "company_name", "companyName", "title"])
        if identifier or name:
            return {"id": identifier, "name": name}
    return None


def _company_context_arguments(spec: ToolSpec | None, company_context: dict[str, Any] | None) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    if not isinstance(company_context, dict):
        return arguments
    _set_schema_arg(
        arguments,
        spec,
        ["company_id", "companyId", "profile_id", "profileId", "company_profile_id", "companyProfileId"],
        company_context.get("id"),
    )
    _set_schema_arg(
        arguments,
        spec,
        ["company_name", "companyName", "profile_name", "profileName", "workspace_name", "workspaceName"],
        company_context.get("name"),
    )
    return arguments


def _onboarding_resume_picker_arguments(workflow_state: dict[str, Any]) -> dict[str, Any]:
    scope = str(workflow_state.get("scope") or "full_setup").strip() or "full_setup"
    summary = str(workflow_state.get("summary") or "").strip()
    description = (
        f"You have an unfinished {ONBOARDING_SCOPE_LABELS.get(scope, scope.replace('_', ' ').title())} workflow."
    )
    if summary:
        description = f"{description}\n\nLatest saved plan:\n{summary}"
    description = f"{description}\n\nChoose whether to resume it or start a new onboarding flow."
    return {
        "title": "Resume Inventory Onboarding",
        "description": description,
        "options": [
            {"value": "resume_saved", "label": "Resume Saved Onboarding"},
            {"value": "start_over", "label": "Start Over"},
            {"value": "cancel_saved", "label": "Cancel Saved Onboarding"},
        ],
        "multiple": False,
        "allow_input": True,
    }


def _onboarding_operation_summary(
    *,
    created_operations: dict[str, Any],
    failed_operations: list[dict[str, Any]],
) -> str:
    created_labels: list[str] = []
    for payload in created_operations.values():
        if isinstance(payload, dict):
            label = str(payload.get("label") or "").strip()
            if label:
                created_labels.append(label)

    lines: list[str] = []
    if created_labels:
        lines.append("Completed: " + ", ".join(created_labels))
    if failed_operations:
        failed_labels = [str(item.get("label") or "").strip() for item in failed_operations if str(item.get("label") or "").strip()]
        blocker_messages: list[str] = []
        seen_blockers: set[str] = set()
        retryable_values: list[bool] = []
        for item in failed_operations:
            classified = _classify_failed_operation(item)
            retryable_values.append(bool(classified.get("retryable", True)))
            message = str(classified.get("error_summary") or "").strip()
            if not message or message in seen_blockers:
                continue
            seen_blockers.add(message)
            blocker_messages.append(message)
        if blocker_messages:
            lines.append("Blocking issues: " + "; ".join(blocker_messages[:3]))
        if failed_labels:
            if blocker_messages and len(failed_labels) > 3:
                retry_guidance = "Retry may succeed once the blocked service recovers." if any(retryable_values) else "Retry will not help until the configuration is fixed."
                lines.append(f"Still pending: {len(failed_labels)} onboarding steps are blocked. {retry_guidance}")
            else:
                lines.append("Still pending: " + ", ".join(failed_labels))
    return "\n".join(lines)


def _onboarding_retry_picker_arguments(
    *,
    summary: str,
    created_operations: dict[str, Any],
    failed_operations: list[dict[str, Any]],
) -> dict[str, Any]:
    description = summary
    if created_operations or failed_operations:
        extra = _onboarding_operation_summary(
            created_operations=created_operations,
            failed_operations=failed_operations,
        )
        if extra:
            description = f"{description}\n\n{extra}"
    description = f"{description}\n\nSome setup steps still need attention. Choose what to do next."
    return {
        "title": "Resolve Onboarding Issues",
        "description": description,
        "options": [
            {"value": "retry_failed", "label": "Retry Failed Steps"},
            {"value": "revise_answers", "label": "Revise My Answers"},
            {"value": "cancel_onboarding", "label": "Cancel For Now"},
        ],
        "multiple": False,
        "allow_input": True,
    }


def _onboarding_completed_text(created_operations: dict[str, Any]) -> str:
    counts = {
        "stock_location": 0,
        "inventory_category": 0,
        "inventory": 0,
        "product": 0,
    }
    for payload in created_operations.values():
        if not isinstance(payload, dict):
            continue
        operation_type = str(payload.get("operation_type") or "").strip()
        if operation_type in counts:
            counts[operation_type] += 1

    parts: list[str] = []
    if counts["stock_location"]:
        parts.append(f"{counts['stock_location']} stock location" + ("s" if counts["stock_location"] != 1 else ""))
    if counts["inventory_category"]:
        parts.append(
            f"{counts['inventory_category']} inventory categor" + ("ies" if counts["inventory_category"] != 1 else "y")
        )
    if counts["inventory"]:
        parts.append(f"{counts['inventory']} inventory item" + ("s" if counts["inventory"] != 1 else ""))
    if counts["product"]:
        parts.append(f"{counts['product']} product" + ("s" if counts["product"] != 1 else ""))

    if not parts:
        return "No onboarding records were created."
    if len(parts) == 1:
        return f"Created {parts[0]} for onboarding."
    return "Created " + ", ".join(parts[:-1]) + f", and {parts[-1]} for onboarding."


def _tool_discovery_failures_for_name(tool_executor: ToolExecutor | None, tool_name: str) -> list[dict[str, Any]]:
    if tool_executor is None:
        return []
    failures_getter = getattr(tool_executor, "list_tool_failures", None)
    if not callable(failures_getter):
        return []
    try:
        failures = failures_getter()
    except Exception:
        return []
    if not isinstance(failures, list):
        return []

    relevant: list[dict[str, Any]] = []
    bare_tool_name = tool_name.split(".", 1)[-1]
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        prefix = str(failure.get("tool_name_prefix") or "").strip()
        allowed_tools = failure.get("allowed_tools")
        allowed_tool_names = {
            str(item).strip()
            for item in allowed_tools
            if isinstance(allowed_tools, list) and str(item).strip()
        }
        if prefix and tool_name.startswith(prefix):
            relevant.append(dict(failure))
            continue
        if allowed_tool_names and (tool_name in allowed_tool_names or bare_tool_name in allowed_tool_names):
            relevant.append(dict(failure))

    if relevant:
        return relevant
    if len(failures) == 1 and isinstance(failures[0], dict):
        return [dict(failures[0])]
    return []


def _annotate_failed_operation(item: dict[str, Any]) -> dict[str, Any]:
    classified = _classify_failed_operation(item)
    return {
        **item,
        "error_kind": classified.get("error_kind"),
        "error_summary": classified.get("error_summary"),
        "retryable": classified.get("retryable"),
    }


def _created_result_ref(semantic_key: str, *path: str) -> dict[str, Any]:
    return {
        "__ka2a_created_ref__": {
            "semantic_key": semantic_key,
            "path": list(path),
        }
    }


def _extract_created_result_value(payload: Any, path: list[str]) -> Any:
    current = payload
    for part in path:
        if not isinstance(current, dict):
            current = None
            break
        current = current.get(part)
        if current in (None, "", [], {}):
            break
    if current not in (None, "", [], {}):
        return current

    if path and path[-1] == "id" and isinstance(payload, dict):
        direct_id = payload.get("id")
        if direct_id not in (None, "", [], {}):
            return direct_id
    return None


def _resolve_created_result_ref_value(ref: dict[str, Any], created_map: dict[str, dict[str, Any]]) -> Any:
    meta = ref.get("__ka2a_created_ref__")
    if not isinstance(meta, dict):
        return ref
    semantic_key = str(meta.get("semantic_key") or "").strip()
    if not semantic_key:
        raise ValueError("Missing semantic_key for created-result reference.")
    created_entry = created_map.get(semantic_key)
    if not isinstance(created_entry, dict):
        raise KeyError(f"Missing created operation dependency: {semantic_key}")
    result_payload = created_entry.get("result")
    path = [str(item).strip() for item in meta.get("path") or [] if str(item).strip()]
    resolved = _extract_created_result_value(result_payload, path) if path else result_payload
    if resolved in (None, "", [], {}):
        raise KeyError(f"Unable to resolve dependency output for {semantic_key}")
    return resolved


def _resolve_created_result_refs(value: Any, created_map: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, dict):
        if "__ka2a_created_ref__" in value:
            return _resolve_created_result_ref_value(value, created_map)
        return {key: _resolve_created_result_refs(item, created_map) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_created_result_refs(item, created_map) for item in value]
    return value


def _build_stock_location_operation(
    *,
    tool_specs: list[ToolSpec],
    company_context: dict[str, Any] | None,
    location_name: str,
    location_type: str | None,
    primary: bool,
    structural: bool | None = None,
    parent_location_id: str | None = None,
    parent_location_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tool_name = "inventory.create_stock_location"
    spec = _tool_spec_by_name(tool_specs, tool_name)
    arguments = _company_context_arguments(spec, company_context)
    base_argument_keys = set(arguments)
    structural_value = primary if structural is None else structural
    parent_value: Any = parent_location_id
    if parent_value in (None, "", [], {}) and parent_location_ref is not None:
        parent_value = parent_location_ref
    _set_schema_arg(arguments, spec, ["name", "location_name", "locationName", "stock_location_name", "stockLocationName"], location_name)
    _set_schema_arg(arguments, spec, ["type", "location_type", "locationType", "stock_location_type", "stockLocationType"], location_type)
    _set_schema_arg(arguments, spec, ["is_primary", "isPrimary", "primary"], primary)
    _set_schema_arg(arguments, spec, ["structural", "is_structural", "isStructural"], structural_value)
    parent_id_key = _match_schema_key(spec, ["parent_id", "parentId"])
    if parent_id_key and parent_value not in (None, "", [], {}):
        arguments[parent_id_key] = parent_value
    location_type_name_key = _match_schema_key(spec, ["location_type_name", "locationTypeName"])
    if location_type_name_key and location_type not in (None, "", [], {}):
        arguments[location_type_name_key] = location_type

    payload_spec = _nested_object_tool_spec(spec, "payload")
    payload_required = "payload" in _tool_schema_required(spec)
    top_level_args_added = bool(set(arguments) - base_argument_keys)
    if payload_spec is not None and (payload_required or not top_level_args_added):
        payload_arguments: dict[str, Any] = {}
        _set_schema_arg(
            payload_arguments,
            payload_spec,
            ["name", "location_name", "locationName", "stock_location_name", "stockLocationName"],
            location_name,
        )
        _set_schema_arg(
            payload_arguments,
            payload_spec,
            ["type", "location_type", "locationType", "stock_location_type", "stockLocationType"],
            location_type,
        )
        _set_schema_arg(payload_arguments, payload_spec, ["structural", "is_structural", "isStructural"], structural_value)
        payload_parent_id_key = _match_schema_key(payload_spec, ["parent_id", "parentId"])
        if payload_parent_id_key and parent_value not in (None, "", [], {}):
            payload_arguments[payload_parent_id_key] = parent_value
        payload_location_type_name_key = _match_schema_key(payload_spec, ["location_type_name", "locationTypeName"])
        if payload_location_type_name_key and location_type not in (None, "", [], {}):
            payload_arguments[payload_location_type_name_key] = location_type
        if payload_arguments:
            arguments["payload"] = payload_arguments
    return {
        "tool_name": tool_name,
        "label": f"stock location '{location_name}'",
        "operation_type": "stock_location",
        "semantic_key": f"{tool_name}:{_normalize_operation_key(location_name)}",
        "arguments": _filtered_tool_arguments(spec, arguments),
        "missing_required": _missing_required_arguments(spec, _filtered_tool_arguments(spec, arguments)),
    }


def _build_inventory_category_operation(
    *,
    tool_specs: list[ToolSpec],
    company_context: dict[str, Any] | None,
    category_name: str,
    default_location_id: str | None = None,
    default_location_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tool_name = "inventory.create_inventory_category"
    spec = _tool_spec_by_name(tool_specs, tool_name)
    arguments = _company_context_arguments(spec, company_context)
    base_argument_keys = set(arguments)
    location_value: Any = default_location_id
    if location_value in (None, "", [], {}) and default_location_ref is not None:
        location_value = default_location_ref
    _set_schema_arg(arguments, spec, ["name", "category_name", "categoryName", "title"], category_name)
    default_location_key = _match_schema_key(
        spec,
        ["default_location_id", "defaultLocationId", "stock_location_id", "stockLocationId"],
    )
    if default_location_key and location_value not in (None, "", [], {}):
        arguments[default_location_key] = location_value

    payload_spec = _nested_object_tool_spec(spec, "payload")
    payload_required = "payload" in _tool_schema_required(spec)
    top_level_args_added = bool(set(arguments) - base_argument_keys)
    if payload_spec is not None and (payload_required or not top_level_args_added):
        payload_arguments: dict[str, Any] = {}
        _set_schema_arg(payload_arguments, payload_spec, ["name", "category_name", "categoryName", "title"], category_name)
        payload_location_key = _match_schema_key(
            payload_spec,
            ["default_location_id", "defaultLocationId", "stock_location_id", "stockLocationId"],
        )
        if payload_location_key and location_value not in (None, "", [], {}):
            payload_arguments[payload_location_key] = location_value
        if payload_arguments:
            arguments["payload"] = payload_arguments
    return {
        "tool_name": tool_name,
        "label": f"inventory category '{category_name}'",
        "operation_type": "inventory_category",
        "semantic_key": f"{tool_name}:{_normalize_operation_key(category_name)}",
        "arguments": _filtered_tool_arguments(spec, arguments),
        "missing_required": _missing_required_arguments(spec, _filtered_tool_arguments(spec, arguments)),
    }


def _build_inventory_operation(
    *,
    tool_specs: list[ToolSpec],
    company_context: dict[str, Any] | None,
    inventory_name: str,
    inventory_description: str | None,
    related_location_id: str | None,
    related_location_name: str | None,
    category_id: str | None,
    category_name: str | None,
    category_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tool_name = "inventory.create_inventory_item"
    spec = _tool_spec_by_name(tool_specs, tool_name)
    arguments = _company_context_arguments(spec, company_context)
    base_argument_keys = set(arguments)
    resolved_category_id: Any = category_id
    if resolved_category_id in (None, "", [], {}) and category_ref is not None:
        resolved_category_id = category_ref
    _set_schema_arg(arguments, spec, ["name", "name_snapshot", "inventory_name", "inventoryName", "nameSnapshot", "title"], inventory_name)
    _set_schema_arg(arguments, spec, ["description", "inventory_description", "inventoryDescription", "notes"], inventory_description)

    location_id_key = _match_schema_key(
        spec,
        ["location_id", "locationId", "stock_location_id", "stockLocationId", "default_location_id", "defaultLocationId"],
    )
    if location_id_key and related_location_id not in (None, "", [], {}):
        arguments[location_id_key] = related_location_id
    else:
        _set_schema_arg(
            arguments,
            spec,
            ["location_name", "locationName", "stock_location_name", "stockLocationName", "default_location_name", "defaultLocationName"],
            related_location_name,
        )

    category_id_key = _match_schema_key(
        spec,
        ["category_id", "categoryId", "inventory_category_id", "inventoryCategoryId", "default_category_id", "defaultCategoryId"],
    )
    if category_id_key and resolved_category_id not in (None, "", [], {}):
        arguments[category_id_key] = resolved_category_id
    else:
        _set_schema_arg(
            arguments,
            spec,
            ["category_name", "categoryName", "inventory_category_name", "inventoryCategoryName", "default_category_name", "defaultCategoryName"],
            category_name,
        )

    payload_spec = _nested_object_tool_spec(spec, "payload")
    payload_required = "payload" in _tool_schema_required(spec)
    top_level_inventory_args_added = bool(set(arguments) - base_argument_keys)
    if payload_spec is not None and (payload_required or not top_level_inventory_args_added):
        payload_arguments: dict[str, Any] = {}
        _set_schema_arg(
            payload_arguments,
            payload_spec,
            ["name", "name_snapshot", "inventory_name", "inventoryName", "nameSnapshot", "title"],
            inventory_name,
        )
        _set_schema_arg(
            payload_arguments,
            payload_spec,
            ["description", "inventory_description", "inventoryDescription", "notes"],
            inventory_description,
        )
        payload_location_id_key = _match_schema_key(
            payload_spec,
            ["location_id", "locationId", "stock_location_id", "stockLocationId", "default_location_id", "defaultLocationId"],
        )
        if payload_location_id_key and related_location_id not in (None, "", [], {}):
            payload_arguments[payload_location_id_key] = related_location_id
        else:
            _set_schema_arg(
                payload_arguments,
                payload_spec,
                ["location_name", "locationName", "stock_location_name", "stockLocationName", "default_location_name", "defaultLocationName"],
                related_location_name,
            )
        payload_category_id_key = _match_schema_key(
            payload_spec,
            ["category_id", "categoryId", "inventory_category_id", "inventoryCategoryId", "default_category_id", "defaultCategoryId"],
        )
        if payload_category_id_key and resolved_category_id not in (None, "", [], {}):
            payload_arguments[payload_category_id_key] = resolved_category_id
        else:
            _set_schema_arg(
                payload_arguments,
                payload_spec,
                ["category_name", "categoryName", "inventory_category_name", "inventoryCategoryName", "default_category_name", "defaultCategoryName"],
                category_name,
            )
        if payload_arguments:
            arguments["payload"] = payload_arguments
    return {
        "tool_name": tool_name,
        "label": f"inventory item '{inventory_name}'",
        "operation_type": "inventory",
        "semantic_key": f"{tool_name}:{_normalize_operation_key(inventory_name)}",
        "arguments": _filtered_tool_arguments(spec, arguments),
        "missing_required": _missing_required_arguments(spec, _filtered_tool_arguments(spec, arguments)),
    }


def _build_product_operation(
    *,
    tool_specs: list[ToolSpec],
    company_context: dict[str, Any] | None,
    product_name: str,
    product_category_id: str | None,
    product_category: str | None,
    pos_ready: bool | None,
) -> dict[str, Any]:
    tool_name = "product.create_product"
    spec = _tool_spec_by_name(tool_specs, tool_name)
    arguments = _company_context_arguments(spec, company_context)
    base_argument_keys = set(arguments)
    _set_schema_arg(arguments, spec, ["name", "product_name", "productName", "title"], product_name)
    product_category_id_key = _match_schema_key(
        spec,
        ["category_id", "categoryId", "product_category_id", "productCategoryId", "default_category_id", "defaultCategoryId"],
    )
    if product_category_id_key and product_category_id not in (None, "", [], {}):
        arguments[product_category_id_key] = product_category_id
    else:
        _set_schema_arg(arguments, spec, ["category_name", "categoryName", "product_category", "productCategory", "category"], product_category)
    _set_schema_arg(arguments, spec, ["pos_ready", "posReady", "pos_visible", "posVisible", "quick_sale", "quickSale"], pos_ready)
    payload_spec = _nested_object_tool_spec(spec, "payload")
    payload_required = "payload" in _tool_schema_required(spec)
    top_level_product_args_added = bool(set(arguments) - base_argument_keys)
    if payload_spec is not None and (payload_required or not top_level_product_args_added):
        payload_arguments: dict[str, Any] = {}
        _set_schema_arg(payload_arguments, payload_spec, ["name", "product_name", "productName", "title"], product_name)
        payload_category_id_key = _match_schema_key(
            payload_spec,
            ["category_id", "categoryId", "product_category_id", "productCategoryId", "default_category_id", "defaultCategoryId"],
        )
        if payload_category_id_key and product_category_id not in (None, "", [], {}):
            payload_arguments[payload_category_id_key] = product_category_id
        else:
            _set_schema_arg(
                payload_arguments,
                payload_spec,
                ["category_name", "categoryName", "product_category", "productCategory", "category"],
                product_category,
            )
        _set_schema_arg(
            payload_arguments,
            payload_spec,
            ["pos_ready", "posReady", "pos_visible", "posVisible", "quick_sale", "quickSale"],
            pos_ready,
        )
        if payload_arguments:
            arguments["payload"] = payload_arguments
    return {
        "tool_name": tool_name,
        "label": f"product '{product_name}'",
        "operation_type": "product",
        "semantic_key": f"{tool_name}:{_normalize_operation_key(product_name)}",
        "arguments": _filtered_tool_arguments(spec, arguments),
        "missing_required": _missing_required_arguments(spec, _filtered_tool_arguments(spec, arguments)),
    }


def _onboarding_plan_operations(
    *,
    scope: str,
    onboarding_data: dict[str, Any],
    tool_specs: list[ToolSpec],
    company_context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    flat = onboarding_data.get("flat") if isinstance(onboarding_data.get("flat"), dict) else {}
    operations: list[dict[str, Any]] = []
    primary_location_ref: dict[str, Any] | None = None
    first_category_ref: dict[str, Any] | None = None
    primary_location_id = str(flat.get("primary_location_id") or "").strip() or None
    primary_location_name = str(flat.get("primary_location_name") or "").strip()
    primary_location_type = str(flat.get("primary_location_type") or "").strip() or None
    primary_location_mode = str(flat.get("primary_location_mode") or "").strip().lower() or (
        "existing" if primary_location_id else "new"
    )

    if scope in {"stock_locations", "full_setup"}:
        if primary_location_mode == "existing" and primary_location_id:
            primary_location_ref = primary_location_id
        elif primary_location_name:
            primary_operation = _build_stock_location_operation(
                tool_specs=tool_specs,
                company_context=company_context,
                location_name=primary_location_name,
                location_type=primary_location_type,
                primary=True,
                structural=True,
            )
            operations.append(primary_operation)
            primary_location_ref = _created_result_ref(primary_operation["semantic_key"], "location", "id")
        for location_name in _split_multiline_values(flat.get("additional_locations")):
            operations.append(
                _build_stock_location_operation(
                    tool_specs=tool_specs,
                    company_context=company_context,
                    location_name=location_name,
                    location_type=None,
                    primary=False,
                    structural=False,
                    parent_location_id=primary_location_id if primary_location_mode == "existing" else None,
                    parent_location_ref=primary_location_ref if isinstance(primary_location_ref, dict) else None,
                )
            )

    categories = _split_multiline_values(flat.get("category_names"))
    if scope in {"inventory_categories", "full_setup"}:
        for category_name in categories:
            category_operation = _build_inventory_category_operation(
                tool_specs=tool_specs,
                company_context=company_context,
                category_name=category_name,
                default_location_id=primary_location_id if primary_location_mode == "existing" else None,
                default_location_ref=primary_location_ref if isinstance(primary_location_ref, dict) else None,
            )
            operations.append(category_operation)
            if first_category_ref is None:
                first_category_ref = _created_result_ref(category_operation["semantic_key"], "category", "id")

    if scope in {"inventory_setup", "full_setup"}:
        inventory_name = str(flat.get("default_inventory_name") or "").strip()
        if inventory_name:
            operations.append(
                _build_inventory_operation(
                    tool_specs=tool_specs,
                    company_context=company_context,
                    inventory_name=inventory_name,
                    inventory_description=str(flat.get("inventory_description") or "").strip() or None,
                    related_location_id=str(flat.get("related_stock_location_id") or "").strip() or None,
                    related_location_name=(
                        str(flat.get("related_stock_location_label") or "").strip()
                        or str(flat.get("related_location_name") or "").strip()
                        or str(flat.get("primary_location_label") or "").strip()
                        or str(flat.get("primary_location_name") or "").strip()
                        or None
                    ),
                    category_id=str(flat.get("inventory_category_id") or "").strip() or None,
                    category_name=(
                        str(flat.get("inventory_category_label") or "").strip()
                        or str(flat.get("category_name") or "").strip()
                        or (categories[0] if categories else None)
                    ),
                    category_ref=first_category_ref,
                )
            )

    should_create_products = scope == "product_onboarding" or (
        scope == "full_setup" and flat.get("continue_to_product_onboarding") is True
    )
    if should_create_products:
        product_names = _split_multiline_values(flat.get("product_names") or flat.get("initial_product_names"))
        product_category = (
            str(flat.get("product_category_label") or "").strip()
            or str(flat.get("product_category") or "").strip()
            or None
        )
        product_category_id = str(flat.get("product_category_id") or "").strip() or None
        pos_ready = flat.get("pos_ready")
        pos_ready_value = pos_ready if isinstance(pos_ready, bool) else None
        for product_name in product_names:
            operations.append(
                _build_product_operation(
                    tool_specs=tool_specs,
                    company_context=company_context,
                    product_name=product_name,
                    product_category_id=product_category_id,
                    product_category=product_category or (categories[0] if categories else None),
                    pos_ready=pos_ready_value,
                )
            )

    return operations


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


def _coerce_agent_summaries(value: Any, *, key: str = "agents") -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    agents = value.get(key)
    if not isinstance(agents, list):
        return []
    out: list[dict[str, Any]] = []
    for item in agents:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("name"):
            out.append(item)
    return out


def _coerce_agent_listing(value: Any) -> dict[str, list[dict[str, Any]]]:
    visible_agents = _coerce_agent_summaries(value, key="agents")
    registered_agents = _coerce_agent_summaries(value, key="registered_agents")
    hidden_agents = _coerce_agent_summaries(value, key="hidden_agents")
    if not registered_agents and visible_agents:
        registered_agents = list(visible_agents)
    if not hidden_agents and registered_agents:
        visible_names = _available_agent_names(visible_agents)
        hidden_agents = [item for item in registered_agents if str(item.get("name") or "") not in visible_names]
    return {
        "agents": visible_agents,
        "registered_agents": registered_agents,
        "hidden_agents": hidden_agents,
    }


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


def _select_router_delegation_agent(query: str, agents: list[dict[str, Any]]) -> str | None:
    if not agents:
        return None
    if len(agents) == 1:
        return str(agents[0].get("name") or "").strip() or None

    normalized_query = _normalize_relation_token(query)
    for agent in agents:
        agent_name = str(agent.get("name") or "").strip()
        if agent_name and _normalize_relation_token(agent_name) == normalized_query:
            return agent_name
        for skill in agent.get("skills") or []:
            if not isinstance(skill, dict):
                continue
            skill_name = str(skill.get("name") or "").strip()
            skill_id = str(skill.get("id") or "").strip()
            if skill_name and _normalize_relation_token(skill_name) == normalized_query:
                return agent_name
            if skill_id and _normalize_relation_token(skill_id) == normalized_query:
                return agent_name

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
    response_parts = _augment_delegated_response_parts(
        response_parts,
        delegated_agent=delegated_agent,
        delegated_task_id=delegated_task_id,
        fallback_text=response_text,
    )
    if not response_text and response_parts:
        response_text = _text_from_parts(response_parts)
    if not response_parts:
        response_parts = [TextPart(text="(no result)")]
        response_text = "(no result)"
    if _interaction_payload_from_parts(response_parts) is not None and delegated_final_state == TaskState.completed:
        delegated_final_state = TaskState.input_required
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


def _annotate_delegated_interaction_payload(
    payload: dict[str, Any],
    *,
    delegated_agent: str,
    delegated_task_id: str | None,
) -> dict[str, Any]:
    enriched = dict(payload)
    enriched["delegated_agent"] = delegated_agent
    if delegated_task_id:
        enriched["delegated_task_id"] = delegated_task_id
    enriched["delegated_via_host"] = True
    return enriched


def _onboarding_scope_value_from_label(label: str) -> str:
    lowered = re.sub(r"\s+", " ", label.strip().lower())
    if "full" in lowered and "setup" in lowered:
        return "full_setup"
    if "stock" in lowered and "location" in lowered:
        return "stock_locations"
    if "categor" in lowered:
        return "inventory_categories"
    if "ledger" in lowered or ("inventory" in lowered and "setup" in lowered):
        return "inventory_setup"
    if "product" in lowered:
        return "product_onboarding"
    slug = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    return slug or "option"


def _fallback_onboarding_interaction_payload(
    text: str,
    *,
    delegated_agent: str,
    delegated_task_id: str | None,
) -> dict[str, Any] | None:
    if delegated_agent != "onboarding":
        return None
    if "choose from the following options" not in text.lower():
        return None
    option_labels = [
        match.group(1).strip()
        for match in re.finditer(r"(?m)^\s*\d+[.)]\s+(.+?)\s*$", text)
        if match.group(1).strip()
    ]
    if len(option_labels) < 2:
        return None
    payload = _with_interaction_metadata(
        {
            "interaction_type": "multiple_choice",
            **_onboarding_scope_picker_arguments(),
        },
        workflow="inventory_onboarding",
        workflow_stage="scope_picker",
    )
    payload["options"] = [
        {"value": _onboarding_scope_value_from_label(label), "label": label}
        for label in option_labels
    ]
    return _annotate_delegated_interaction_payload(
        payload,
        delegated_agent=delegated_agent,
        delegated_task_id=delegated_task_id,
    )


def _augment_delegated_response_parts(
    parts: list[Any],
    *,
    delegated_agent: str,
    delegated_task_id: str | None,
    fallback_text: str,
) -> list[Any]:
    if not parts and not fallback_text:
        return parts

    augmented: list[Any] = []
    interaction_found = False
    for part in parts:
        payload: dict[str, Any] | None = None
        if isinstance(part, DataPart):
            payload = _interaction_payload_from_obj(part.data)
        elif isinstance(part, TextPart):
            payload = _interaction_payload_from_text(part.text)
        if payload is None:
            augmented.append(part)
            continue
        augmented.append(
            DataPart(
                data=_annotate_delegated_interaction_payload(
                    payload,
                    delegated_agent=delegated_agent,
                    delegated_task_id=delegated_task_id,
                )
            )
        )
        interaction_found = True

    if interaction_found:
        return augmented

    fallback_payload = _fallback_onboarding_interaction_payload(
        fallback_text,
        delegated_agent=delegated_agent,
        delegated_task_id=delegated_task_id,
    )
    if fallback_payload is not None:
        return [DataPart(data=fallback_payload)]
    return augmented or parts


def _matching_relation_specs_for_texts(tool_specs: list[ToolSpec], *texts: str | None) -> list[dict[str, Any]]:
    normalized_texts = [_normalize_relation_token(text) for text in texts if _normalize_relation_token(text)]
    if not normalized_texts:
        return []

    scored_matches: list[tuple[int, int, dict[str, Any]]] = []
    for relation_spec in _relation_lookup_specs(tool_specs):
        aliases = set(relation_spec["aliases"]) | set(relation_spec["model_tokens"])
        if not aliases:
            continue
        best_score = 0
        best_alias_len = 0
        for text in normalized_texts:
            for alias in aliases:
                score = _relation_text_match_score(text, alias)
                if score > best_score:
                    best_score = score
                    best_alias_len = len(alias)
        if best_score <= 0:
            continue
        scored_matches.append((best_score, best_alias_len, relation_spec))
    scored_matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [relation_spec for _, _, relation_spec in scored_matches]


def _relation_items_from_lookup_output(lookup_tool: str, output: Any) -> list[dict[str, Any]]:
    coerced = _coerce_mapping_from_tool_output(output)
    if isinstance(coerced, dict):
        output = coerced

    if not isinstance(output, dict):
        return output if isinstance(output, list) else []

    if lookup_tool == "inventory.list_inventory_categories":
        category_payload = output.get("category")
        if isinstance(category_payload, dict):
            results = category_payload.get("results")
            if isinstance(results, list):
                return results
        if isinstance(category_payload, list):
            return category_payload
        fallback = _find_relation_items(category_payload)
        if fallback:
            return fallback

    if lookup_tool in {
        "inventory.list_stock_locations",
        "inventory.search_stock_locations",
        "product.get_product_categories",
        "product.search_products",
        "inventory.list_inventory_items",
        "inventory.search_inventory_items",
        "inventory.search_purchase_orders",
    }:
        results = output.get("results")
        if isinstance(results, list):
            return results

    return _find_relation_items(output)


def _relation_item_has_identifier_and_label(item: dict[str, Any]) -> bool:
    return bool(
        _first_string(item, ["id", "uuid", "value"])
        and _first_string(
            item,
            ["name", "title", "label", "inventory_item_name", "stock_location_name", "category", "order_no", "po_number"],
        )
    )


def _find_relation_items(value: Any, *, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 6:
        return []
    if isinstance(value, list):
        dict_items = [item for item in value if isinstance(item, dict)]
        if dict_items and any(_relation_item_has_identifier_and_label(item) for item in dict_items):
            return dict_items
        for item in value:
            nested = _find_relation_items(item, depth=depth + 1)
            if nested:
                return nested
        return []
    if isinstance(value, dict):
        preferred_keys = (
            "results",
            "items",
            "data",
            "result",
            "category",
            "categories",
            "location",
            "locations",
            "records",
        )
        seen_keys: set[str] = set()
        for key in preferred_keys:
            seen_keys.add(key)
            nested = _find_relation_items(value.get(key), depth=depth + 1)
            if nested:
                return nested
        for key, nested_value in value.items():
            if key in seen_keys:
                continue
            nested = _find_relation_items(nested_value, depth=depth + 1)
            if nested:
                return nested
    return []


def _relation_option_from_item(lookup_tool: str, item: dict[str, Any]) -> dict[str, Any] | None:
    identifier = _first_string(item, ["id", "uuid", "value"])
    if not identifier:
        return None

    label = _first_string(
        item,
        [
            "name",
            "title",
            "label",
            "inventory_item_name",
            "stock_location_name",
            "category",
            "order_no",
            "po_number",
        ],
    )
    if not label:
        return None

    description_parts: list[str] = []
    if lookup_tool == "inventory.search_stock_locations":
        location_type = _first_string(item, ["location_type"])
        physical_address = _first_string(item, ["physical_address"])
        if location_type:
            description_parts.append(location_type)
        if physical_address:
            description_parts.append(physical_address)
    elif lookup_tool in {"inventory.list_inventory_categories", "product.get_product_categories"}:
        description = _first_string(item, ["description"])
        if description:
            description_parts.append(description)
    elif lookup_tool == "product.search_products":
        category = _first_string(item, ["category"])
        sku = _first_string(item, ["sku"])
        if category:
            description_parts.append(category)
        if sku:
            description_parts.append(f"SKU: {sku}")
    elif lookup_tool in {"inventory.list_inventory_items", "inventory.search_inventory_items"}:
        category = _first_string(item, ["inventory_category", "category"])
        if category:
            description_parts.append(category)
    elif lookup_tool == "inventory.search_purchase_orders":
        supplier_name = _first_string(item, ["supplier_name", "supplier"])
        status = _first_string(item, ["status"])
        if supplier_name:
            description_parts.append(supplier_name)
        if status:
            description_parts.append(status)

    option: dict[str, Any] = {"value": identifier, "label": label}
    if description_parts:
        option["description"] = " | ".join(description_parts[:2])
    return option


async def _load_relation_options(
    relation_spec: dict[str, Any],
    *,
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
    cache: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    lookup_tool = str(relation_spec.get("lookup_tool") or "").strip()
    if not lookup_tool:
        return []
    cached = cache.get(lookup_tool)
    if cached is not None:
        return cached

    try:
        output = await tool_executor.call_tool(
            name=lookup_tool,
            arguments=dict(relation_spec.get("default_arguments") or {}),
            ctx=tool_ctx,
        )
    except Exception:
        cache[lookup_tool] = []
        return []

    options: list[dict[str, Any]] = []
    for item in _relation_items_from_lookup_output(lookup_tool, output):
        if not isinstance(item, dict):
            continue
        option = _relation_option_from_item(lookup_tool, item)
        if option is not None:
            options.append(option)
    cache[lookup_tool] = options
    return options


def _sanitize_relation_prompt_text(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return value
    text = re.sub(r"\bids?\b", "options", text, flags=re.IGNORECASE)
    text = re.sub(r"\buuids?\b", "options", text, flags=re.IGNORECASE)
    text = re.sub(r"\bidentifier[s]?\b", "options", text, flags=re.IGNORECASE)
    return text


def _relation_tool_error_needs_select_recovery(error_text: str | None) -> bool:
    lowered = str(error_text or "").strip().lower()
    if not lowered:
        return False
    markers = (
        "invalid uuid",
        "not a valid uuid",
        "badly formed hexadecimal uuid",
        "uuid",
    )
    return any(marker in lowered for marker in markers)


async def _recover_relation_error_as_interaction(
    *,
    tool_name: str,
    error_text: str | None,
    tool_specs: list[ToolSpec],
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
) -> dict[str, Any] | None:
    if not _relation_tool_error_needs_select_recovery(error_text):
        return None

    spec = _tool_spec_by_name(tool_specs, tool_name)
    if spec is None or not isinstance(spec.input_schema, dict):
        return None

    relation_cache: dict[str, list[dict[str, Any]]] = {}
    fields: list[dict[str, Any]] = []
    seen_field_names: set[str] = set()

    for path, field_schema in _iter_schema_leaf_fields(spec.input_schema):
        relation_specs = _matching_relation_specs_for_texts(tool_specs, path, str(field_schema.get("description") or ""))
        if not relation_specs:
            continue
        options = await _load_relation_options(
            relation_specs[0],
            tool_executor=tool_executor,
            tool_ctx=tool_ctx,
            cache=relation_cache,
        )
        if not options:
            continue
        field_name = path.split(".")[-1].strip()
        if not field_name or field_name in seen_field_names:
            continue
        seen_field_names.add(field_name)
        fields.append(
            {
                "name": field_name,
                "type": "select",
                "label": relation_specs[0]["label"],
                "required": True,
                "options": options,
                "placeholder": f"Select {relation_specs[0]['label']}",
            }
        )

    if not fields:
        return None

    service_label = _service_label_from_tool_name(tool_name).title()
    friendly_tool_name = tool_name.split(".", 1)[-1].replace("_", " ").strip().title()
    description = _sanitize_relation_prompt_text(
        f"The last {service_label.lower()} operation could not continue because one or more relation options were invalid. "
        "Choose the correct backend options below and I will continue without asking for raw IDs."
    )
    return {
        "interaction_type": "dynamic_form",
        "title": f"{friendly_tool_name} Relation Setup",
        "description": description,
        "fields": fields,
    }


async def _rewrite_relation_interaction_payload(
    payload: dict[str, Any],
    *,
    tool_specs: list[ToolSpec],
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
) -> dict[str, Any] | None:
    interaction_type = str(payload.get("interaction_type") or "").strip().lower()
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    relation_cache: dict[str, list[dict[str, Any]]] = {}

    if interaction_type == "dynamic_form":
        fields = payload.get("fields")
        if not isinstance(fields, list):
            return None
        rewritten_fields: list[dict[str, Any]] = []
        changed = False
        for field in fields:
            if not isinstance(field, dict):
                rewritten_fields.append(field)
                continue
            field_name = str(field.get("name") or "").strip()
            field_label = str(field.get("label") or "").strip()
            field_description = str(field.get("description") or "").strip()
            relation_specs = _matching_relation_specs_for_texts(tool_specs, field_name, field_label, field_description)
            if not relation_specs:
                relation_specs = _matching_relation_specs_for_texts(tool_specs, title, description)
            if not relation_specs:
                rewritten_fields.append(field)
                continue
            options = await _load_relation_options(
                relation_specs[0],
                tool_executor=tool_executor,
                tool_ctx=tool_ctx,
                cache=relation_cache,
            )
            if not options:
                rewritten_fields.append(field)
                continue
            rewritten_field = dict(field)
            rewritten_field["type"] = "select"
            rewritten_field["options"] = options
            rewritten_field["placeholder"] = f"Select {relation_specs[0]['label']}"
            rewritten_fields.append(rewritten_field)
            changed = True
        if changed:
            rewritten = dict(payload)
            rewritten["fields"] = rewritten_fields
            if description:
                rewritten["description"] = _sanitize_relation_prompt_text(description)
            return rewritten
        return None

    if interaction_type == "data_table_review":
        rows = payload.get("rows")
        if not isinstance(rows, list):
            return None
        rewritten_fields: list[dict[str, Any]] = []
        changed = False
        for row in rows:
            if not isinstance(row, list) or not row:
                continue
            row_label = str(row[0] or "").strip()
            row_value = str(row[1] or "").strip() if len(row) > 1 else ""
            relation_specs = _matching_relation_specs_for_texts(tool_specs, row_label, title, description)
            if relation_specs:
                options = await _load_relation_options(
                    relation_specs[0],
                    tool_executor=tool_executor,
                    tool_ctx=tool_ctx,
                    cache=relation_cache,
                )
                if options:
                    rewritten_fields.append(
                        {
                            "name": re.sub(r"[^a-z0-9]+", "_", row_label.lower()).strip("_") or "selection",
                            "type": "select",
                            "label": row_label,
                            "required": True,
                            "options": options,
                            "placeholder": f"Select {relation_specs[0]['label']}",
                        }
                    )
                    changed = True
                    continue
            rewritten_fields.append(
                {
                    "name": re.sub(r"[^a-z0-9]+", "_", row_label.lower()).strip("_") or "field",
                    "type": "text",
                    "label": row_label,
                    "required": False,
                    **({"placeholder": row_value} if row_value else {}),
                }
            )
        if changed:
            rewritten = {
                key: value
                for key, value in payload.items()
                if key
                not in {"interaction_type", "headers", "rows", "editable_columns", "allow_add_rows", "allow_delete_rows"}
            }
            rewritten["interaction_type"] = "dynamic_form"
            rewritten["fields"] = rewritten_fields
            if description:
                rewritten["description"] = _sanitize_relation_prompt_text(description)
            return rewritten
        return None

    if interaction_type == "multiple_choice":
        relation_specs = _matching_relation_specs_for_texts(tool_specs, title, description)
        if len(relation_specs) != 1:
            return None
        options = await _load_relation_options(
            relation_specs[0],
            tool_executor=tool_executor,
            tool_ctx=tool_ctx,
            cache=relation_cache,
        )
        if not options:
            return None
        rewritten = dict(payload)
        rewritten["options"] = options
        if description:
            rewritten["description"] = _sanitize_relation_prompt_text(description)
        return rewritten

    if interaction_type == "wizard_flow":
        steps = payload.get("steps")
        if not isinstance(steps, list):
            return None
        rewritten_steps: list[dict[str, Any]] = []
        changed = False
        for step in steps:
            if not isinstance(step, dict):
                rewritten_steps.append(step)
                continue
            fields = step.get("fields")
            if not isinstance(fields, list):
                rewritten_steps.append(step)
                continue
            rewritten_fields: list[dict[str, Any]] = []
            step_changed = False
            for field in fields:
                if not isinstance(field, dict):
                    rewritten_fields.append(field)
                    continue
                field_type = str(field.get("type") or "").strip().lower()
                if field_type not in {"text", "select"}:
                    rewritten_fields.append(field)
                    continue
                field_name = str(field.get("name") or "").strip()
                field_label = str(field.get("label") or "").strip()
                field_description = str(field.get("description") or "").strip()
                relation_specs = _matching_relation_specs_for_texts(
                    tool_specs,
                    field_name,
                    field_label,
                    field_description,
                )
                if not relation_specs:
                    rewritten_fields.append(field)
                    continue
                options = await _load_relation_options(
                    relation_specs[0],
                    tool_executor=tool_executor,
                    tool_ctx=tool_ctx,
                    cache=relation_cache,
                )
                if not options:
                    rewritten_fields.append(field)
                    continue
                rewritten_field = dict(field)
                rewritten_field["type"] = "select"
                rewritten_field["options"] = options
                rewritten_field["placeholder"] = f"Select {relation_specs[0]['label']}"
                rewritten_fields.append(rewritten_field)
                step_changed = True
            if step_changed:
                rewritten_step = dict(step)
                rewritten_step["fields"] = rewritten_fields
                rewritten_steps.append(rewritten_step)
                changed = True
            else:
                rewritten_steps.append(step)
        if changed:
            rewritten = dict(payload)
            rewritten["steps"] = rewritten_steps
            if description:
                rewritten["description"] = _sanitize_relation_prompt_text(description)
            return rewritten
        return None

    return None


async def _rewrite_relation_interaction_parts(
    parts: list[Any],
    *,
    tool_specs: list[ToolSpec],
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
) -> list[Any]:
    rewritten_parts: list[Any] = []
    changed = False
    for part in parts:
        payload: dict[str, Any] | None = None
        if isinstance(part, DataPart):
            payload = _interaction_payload_from_obj(part.data)
        elif isinstance(part, TextPart):
            payload = _interaction_payload_from_text(part.text)

        if payload is None:
            rewritten_parts.append(part)
            continue

        rewritten = await _rewrite_relation_interaction_payload(
            payload,
            tool_specs=tool_specs,
            tool_executor=tool_executor,
            tool_ctx=tool_ctx,
        )
        if rewritten is None:
            rewritten_parts.append(part)
            continue

        rewritten_parts.append(DataPart(data=rewritten))
        changed = True

    return rewritten_parts if changed else parts


async def _rewrite_relation_interaction_dict(
    payload: dict[str, Any],
    *,
    tool_specs: list[ToolSpec],
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
) -> dict[str, Any]:
    rewritten = await _rewrite_relation_interaction_payload(
        payload,
        tool_specs=tool_specs,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
    )
    return rewritten if isinstance(rewritten, dict) else payload


def _delegated_interaction_context(payload: dict[str, Any] | None) -> dict[str, str | None] | None:
    if not isinstance(payload, dict):
        return None
    delegated_agent = str(payload.get("delegated_agent") or "").strip()
    if not delegated_agent:
        return None
    if _interaction_payload_from_obj(payload) is None:
        return None
    delegated_task_id = str(payload.get("delegated_task_id") or "").strip() or None
    return {
        "agent_name": delegated_agent,
        "delegated_task_id": delegated_task_id,
    }


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


def make_langgraph_chat_processor_from_env(
    *,
    agent_name: str | None = None,
    system_prompt_override: str | None = None,
    tool_executor_override: ToolExecutor | None | object = _TOOL_EXECUTOR_SENTINEL,
) -> TaskProcessor:
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

    system_prompt = system_prompt_override if system_prompt_override is not None else resolve_system_prompt_from_env()

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
                try:
                    obj = obj(agent_name=agent_name)
                except TypeError:
                    obj = obj()
            if not hasattr(obj, "list_tools") or not hasattr(obj, "call_tool"):
                raise ValueError("KA2A_TOOL_EXECUTOR must be a ToolExecutor or a callable returning one.")
            return obj  # type: ignore[return-value]

        return None

    if tool_executor_override is _TOOL_EXECUTOR_SENTINEL:
        tool_executor = _build_tool_executor()
    else:
        tool_executor = tool_executor_override

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

    async def _load_workflow_state(*, context_id: str, metadata: dict[str, Any] | None) -> dict[str, Any] | None:
        memory = await _load_memory(context_id=context_id, metadata=metadata)
        if memory is None or not isinstance(memory.workflow_state, dict):
            return None
        return memory.workflow_state

    async def _save_workflow_state(
        *,
        context_id: str,
        metadata: dict[str, Any] | None,
        workflow_state: dict[str, Any] | None,
    ) -> None:
        existing = await _load_memory(context_id=context_id, metadata=metadata)
        memory = ContextMemory(
            summary=existing.summary if existing else None,
            profile=existing.profile if existing else None,
            workflow_state=workflow_state if isinstance(workflow_state, dict) and workflow_state else None,
            updated_at=existing.updated_at if existing else None,
        )
        await _save_memory(context_id=context_id, metadata=metadata, memory=memory)

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
            workflow_state=existing.workflow_state if existing and isinstance(existing.workflow_state, dict) else None,
        )
        await _save_memory(context_id=context_id, metadata=metadata, memory=new_memory)

    async def _proc(
        task: Task,
        message: Message,
        configuration: TaskConfiguration | None,
        metadata: dict[str, Any] | None,
    ) -> AsyncIterator[TaskEvent]:
        _ = configuration
        user_text_for_memory = "\n".join([part.text for part in message.parts if isinstance(part, TextPart)]).strip()

        if _is_simple_greeting_query(user_text_for_memory):
            response_text = _agent_intro_text(agent_name)
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
            return

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
        host_agent_listing: dict[str, list[dict[str, Any]]] | None = None

        async def _load_host_agent_listing() -> dict[str, list[dict[str, Any]]]:
            nonlocal host_agent_listing
            if host_agent_listing is not None:
                return host_agent_listing
            if agent_name != "host" or tool_executor is None or "list_available_agents" not in tool_names:
                host_agent_listing = {"agents": [], "registered_agents": [], "hidden_agents": []}
                return host_agent_listing
            try:
                listed_agents = await tool_executor.call_tool(
                    name="list_available_agents",
                    arguments={},
                    ctx=tool_ctx,
                )
                host_agent_listing = _coerce_agent_listing(listed_agents)
            except Exception:
                host_agent_listing = {"agents": [], "registered_agents": [], "hidden_agents": []}
            return host_agent_listing

        last_interaction_payload = _last_agent_interaction_payload(task)
        interaction_response = _interaction_response_from_text(user_text_for_memory)
        saved_workflow_state = await _load_workflow_state(context_id=task.context_id, metadata=metadata)

        async def _update_host_orchestration_state_after_delegation(
            *,
            delegated_response: dict[str, Any],
            original_request: str,
            orchestration_plan: list[str] | None,
            prior_completed_agents: list[str] | None = None,
        ) -> dict[str, Any] | None:
            nonlocal saved_workflow_state
            if agent_name != "host":
                return None
            prior_state = saved_workflow_state if isinstance(saved_workflow_state, dict) else None
            prior_plan = prior_state.get("plan") if isinstance(prior_state, dict) and isinstance(prior_state.get("plan"), list) else []
            plan = [str(name).strip() for name in (orchestration_plan or prior_plan) if str(name).strip()]
            if not plan:
                if prior_state and str(prior_state.get("workflow") or "").strip().lower() == "host_orchestration":
                    await _save_workflow_state(context_id=task.context_id, metadata=metadata, workflow_state=None)
                    saved_workflow_state = None
                return None

            prior_completed = (
                [str(name).strip() for name in prior_state.get("completed_agents") or [] if str(name).strip()]
                if isinstance(prior_state, dict)
                else [str(name).strip() for name in (prior_completed_agents or []) if str(name).strip()]
            )
            delegated_agent = str(delegated_response.get("delegated_agent") or "").strip()
            interaction_payload = _interaction_payload_from_parts(delegated_response.get("response_parts") or [])
            remaining_agents = [name for name in plan if name not in prior_completed and name != delegated_agent]

            if delegated_response.get("delegated_final_state") == TaskState.input_required and isinstance(interaction_payload, dict):
                workflow_state = {
                    "workflow": "host_orchestration",
                    "status": "awaiting_specialist_input",
                    "stage": "specialist_interaction",
                    "original_request": original_request,
                    "plan": plan,
                    "current_agent": delegated_agent,
                    "completed_agents": prior_completed,
                    "remaining_agents": remaining_agents,
                    "last_response_text": str(delegated_response.get("response_text") or "").strip(),
                    "pending_interaction": interaction_payload,
                }
                delegated_task_id = str(delegated_response.get("delegated_task_id") or "").strip()
                if delegated_task_id:
                    workflow_state["delegated_task_id"] = delegated_task_id
                await _save_workflow_state(
                    context_id=task.context_id,
                    metadata=metadata,
                    workflow_state=workflow_state,
                )
                saved_workflow_state = workflow_state
                return None

            completed_agents = list(prior_completed)
            if delegated_response.get("delegated_final_state") == TaskState.completed and delegated_agent and delegated_agent not in completed_agents:
                completed_agents.append(delegated_agent)
            remaining_agents = [name for name in plan if name not in completed_agents]

            if (
                delegated_response.get("delegated_final_state") == TaskState.completed
                and remaining_agents
                and tool_executor is not None
                and "create_multiple_choice" in tool_names
            ):
                try:
                    interaction_output = await tool_executor.call_tool(
                        name="create_multiple_choice",
                        arguments=_host_orchestration_continue_arguments(
                            {
                                "current_agent": delegated_agent,
                                "remaining_agents": remaining_agents,
                                "last_response_text": str(delegated_response.get("response_text") or "").strip(),
                            }
                        ),
                        ctx=tool_ctx,
                    )
                except Exception:
                    interaction_output = None
                if isinstance(interaction_output, dict):
                    interaction_output = _with_interaction_metadata(
                        interaction_output,
                        workflow="host_orchestration",
                        workflow_stage="continue_prompt",
                        current_agent=delegated_agent,
                        next_agent=remaining_agents[0],
                        original_request=original_request,
                        plan=plan,
                        completed_agents=completed_agents,
                        remaining_agents=remaining_agents,
                        last_response_text=str(delegated_response.get("response_text") or "").strip(),
                    )
                    workflow_state = {
                        "workflow": "host_orchestration",
                        "status": "awaiting_continue_confirmation",
                        "stage": "continue_prompt",
                        "original_request": original_request,
                        "plan": plan,
                        "current_agent": delegated_agent,
                        "completed_agents": completed_agents,
                        "remaining_agents": remaining_agents,
                        "last_response_text": str(delegated_response.get("response_text") or "").strip(),
                        "pending_interaction": interaction_output,
                    }
                    await _save_workflow_state(
                        context_id=task.context_id,
                        metadata=metadata,
                        workflow_state=workflow_state,
                    )
                    saved_workflow_state = workflow_state
                    return interaction_output

            if prior_state and str(prior_state.get("workflow") or "").strip().lower() == "host_orchestration":
                await _save_workflow_state(context_id=task.context_id, metadata=metadata, workflow_state=None)
                saved_workflow_state = None
            return None

        if (
            agent_name == "host"
            and tool_executor is not None
            and "list_available_agents" in tool_names
            and user_text_for_memory
            and _is_host_introspection_query(user_text_for_memory)
            and not _is_host_capability_picker_query(user_text_for_memory)
        ):
            agent_listing = await _load_host_agent_listing()
            available_names = sorted(_agent_listing_names(agent_listing, "agents"))
            registered_names = sorted(_agent_listing_names(agent_listing, "registered_agents"))
            inferred_agent = _infer_domain_agent_name(user_text_for_memory)
            if registered_names:
                labels = ", ".join(_friendly_agent_label(name) for name in registered_names)
                if inferred_agent and _is_host_availability_query(user_text_for_memory):
                    if inferred_agent in set(available_names):
                        label = _friendly_agent_label(inferred_agent)
                        if available_names != registered_names:
                            visible_labels = ", ".join(_friendly_agent_label(name) for name in available_names)
                            response_text = (
                                f"{label} is currently exposed to the host for routing. "
                                f"Currently registered specialist agents: {labels}. "
                                f"The host is currently configured to route to: {visible_labels}."
                            )
                        else:
                            response_text = (
                                f"{label} is currently exposed to the host for routing. "
                                f"Currently registered specialist agents: {labels}."
                            )
                    else:
                        response_text = _host_unavailable_agent_text(
                            agent_name=inferred_agent,
                            available_names=set(available_names),
                            registered_names=set(registered_names),
                        )
                elif available_names != registered_names:
                    visible_labels = ", ".join(_friendly_agent_label(name) for name in available_names)
                    if visible_labels:
                        response_text = (
                            f"Currently registered specialist agents: {labels}. "
                            f"The host is currently configured to route to: {visible_labels}."
                        )
                    else:
                        response_text = (
                            f"Currently registered specialist agents: {labels}. "
                            "None of them are currently exposed to the host for routing."
                        )
                else:
                    response_text = f"Currently registered specialist agents: {labels}."
            else:
                response_text = "No downstream specialist agents are currently visible in the agent directory."
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

        if (
            agent_name == "host"
            and tool_executor is not None
            and "delegate_to_agent" in tool_names
            and interaction_response is not None
            and _is_host_orchestration_payload(last_interaction_payload, stage="continue_prompt")
        ):
            orchestration_state = (
                saved_workflow_state
                if isinstance(saved_workflow_state, dict)
                and str(saved_workflow_state.get("workflow") or "").strip().lower() == "host_orchestration"
                else last_interaction_payload
            )
            selected_value = _selected_interaction_value(interaction_response) or "stop_here"
            if selected_value != "continue_next":
                await _save_workflow_state(context_id=task.context_id, metadata=metadata, workflow_state=None)
                saved_workflow_state = None
                response_text = "Okay. I’ll stop here. Ask whenever you want to continue the remaining work."
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

            remaining_agents = (
                [str(name).strip() for name in orchestration_state.get("remaining_agents") or [] if str(name).strip()]
                if isinstance(orchestration_state, dict) and isinstance(orchestration_state.get("remaining_agents"), list)
                else []
            )
            next_agent = remaining_agents[0] if remaining_agents else ""
            if not next_agent:
                await _save_workflow_state(context_id=task.context_id, metadata=metadata, workflow_state=None)
                saved_workflow_state = None
                response_text = "There are no remaining specialist steps to continue."
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
                return

            yield TaskStatus(
                state=TaskState.working,
                message=Message(
                    role=Role.agent,
                    parts=[TextPart(text=f"Continuing the workflow with the {_friendly_agent_label(next_agent)} specialist.")],
                    context_id=task.context_id,
                ),
            )

            try:
                delegated = await tool_executor.call_tool(
                    name="delegate_to_agent",
                    arguments={
                        "request": _host_orchestration_next_request(
                            orchestration_state if isinstance(orchestration_state, dict) else {},
                            next_agent,
                        ),
                        "agent_name": next_agent,
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

            delegated_response = _coerce_delegated_response(delegated, fallback_agent_name=next_agent)
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

            orchestration_output = await _update_host_orchestration_state_after_delegation(
                delegated_response=delegated_response,
                original_request=(
                    str(orchestration_state.get("original_request") or "").strip()
                    if isinstance(orchestration_state, dict)
                    else ""
                ),
                orchestration_plan=(
                    [str(name).strip() for name in orchestration_state.get("plan") or [] if str(name).strip()]
                    if isinstance(orchestration_state, dict)
                    else []
                ),
                prior_completed_agents=(
                    [str(name).strip() for name in orchestration_state.get("completed_agents") or [] if str(name).strip()]
                    if isinstance(orchestration_state, dict)
                    else []
                ),
            )
            if isinstance(orchestration_output, dict):
                response_parts = [DataPart(data=orchestration_output)]
                response_text = json.dumps(orchestration_output, ensure_ascii=False)
                yield Artifact(name="result", parts=response_parts)
                yield TaskStatus(
                    state=TaskState.input_required,
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

            agent_summaries = (await _load_host_agent_listing()).get("agents")
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

            if selected_value in ROUTER_AGENT_NAMES and "create_multiple_choice" in tool_names:
                picker_arguments = _host_domain_area_picker_arguments(selected_value)
                if picker_arguments is not None:
                    try:
                        interaction_output = await tool_executor.call_tool(
                            name="create_multiple_choice",
                            arguments=picker_arguments,
                            ctx=tool_ctx,
                        )
                    except Exception:
                        interaction_output = None
                    if isinstance(interaction_output, dict):
                        interaction_output["workflow"] = "host_domain_area_picker"
                        interaction_output["workflow_stage"] = "area_picker"
                        interaction_output["domain_agent"] = selected_value
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

            orchestration_output = await _update_host_orchestration_state_after_delegation(
                delegated_response=delegated_response,
                original_request=(
                    str(saved_workflow_state.get("original_request") or "").strip()
                    if isinstance(saved_workflow_state, dict)
                    else ""
                ),
                orchestration_plan=(
                    [str(name).strip() for name in saved_workflow_state.get("plan") or [] if str(name).strip()]
                    if isinstance(saved_workflow_state, dict)
                    else []
                ),
            )
            if isinstance(orchestration_output, dict):
                response_parts = [DataPart(data=orchestration_output)]
                response_text = json.dumps(orchestration_output, ensure_ascii=False)
                yield Artifact(name="result", parts=response_parts)
                yield TaskStatus(
                    state=TaskState.input_required,
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
            and "delegate_to_agent" in tool_names
            and interaction_response is not None
            and _is_host_domain_area_picker_payload(last_interaction_payload)
        ):
            follow_up = _host_domain_area_follow_up_request(last_interaction_payload, interaction_response)
            if follow_up is None:
                response_parts = [DataPart(data=last_interaction_payload)]
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

            delegated_agent_name, delegated_request = follow_up
            yield TaskStatus(
                state=TaskState.working,
                message=Message(
                    role=Role.agent,
                    parts=[TextPart(text=f"Delegating this request to the {delegated_agent_name} specialist agent.")],
                    context_id=task.context_id,
                ),
            )

            try:
                delegated = await tool_executor.call_tool(
                    name="delegate_to_agent",
                    arguments={
                        "request": delegated_request,
                        "agent_name": delegated_agent_name,
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

            delegated_response = _coerce_delegated_response(delegated, fallback_agent_name=delegated_agent_name)
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
            and "delegate_to_agent" in tool_names
            and user_text_for_memory
            and interaction_response is None
            and _is_host_domain_area_picker_payload(last_interaction_payload)
        ):
            delegated_agent_name = str(last_interaction_payload.get("domain_agent") or "").strip().lower()
            if delegated_agent_name in ROUTER_AGENT_NAMES:
                yield TaskStatus(
                    state=TaskState.working,
                    message=Message(
                        role=Role.agent,
                        parts=[TextPart(text=f"Delegating this request to the {delegated_agent_name} specialist agent.")],
                        context_id=task.context_id,
                    ),
                )

                try:
                    delegated = await tool_executor.call_tool(
                        name="delegate_to_agent",
                        arguments={
                            "request": user_text_for_memory,
                            "agent_name": delegated_agent_name,
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

                delegated_response = _coerce_delegated_response(delegated, fallback_agent_name=delegated_agent_name)
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

        delegated_interaction = _delegated_interaction_context(last_interaction_payload)
        if (
            agent_name == "host"
            and tool_executor is not None
            and "delegate_to_agent" in tool_names
            and interaction_response is not None
            and delegated_interaction is not None
        ):
            delegated_agent_name = str(delegated_interaction.get("agent_name") or "").strip()
            delegated_task_id = str(delegated_interaction.get("delegated_task_id") or "").strip() or None

            yield TaskStatus(
                state=TaskState.working,
                message=Message(
                    role=Role.agent,
                    parts=[
                        TextPart(
                            text=f"Passing your response back to the {_friendly_agent_label(delegated_agent_name)} specialist."
                        )
                    ],
                    context_id=task.context_id,
                ),
            )

            try:
                delegated = await tool_executor.call_tool(
                    name="delegate_to_agent",
                    arguments={
                        "request": user_text_for_memory,
                        "agent_name": delegated_agent_name,
                        **({"delegated_task_id": delegated_task_id} if delegated_task_id else {}),
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

            delegated_response = _coerce_delegated_response(delegated, fallback_agent_name=delegated_agent_name)
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
            agent_summaries = (await _load_host_agent_listing()).get("agents")
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

        if agent_name == "onboarding" and tool_executor is not None:
            saved_workflow_state = await _load_workflow_state(context_id=task.context_id, metadata=metadata)
            active_company_context: dict[str, Any] | None = None

            async def _maybe_active_company_context() -> dict[str, Any] | None:
                nonlocal active_company_context
                if active_company_context is not None:
                    return active_company_context
                if saved_workflow_state and isinstance(saved_workflow_state.get("company_context"), dict):
                    active_company_context = saved_workflow_state["company_context"]
                    return active_company_context
                if "users.get_active_company_profile" not in tool_names:
                    return None
                try:
                    output = await tool_executor.call_tool(
                        name="users.get_active_company_profile",
                        arguments={},
                        ctx=tool_ctx,
                    )
                except Exception:
                    return None
                active_company_context = _extract_company_context(output)
                return active_company_context

            if (
                interaction_response is not None
                and _is_onboarding_payload(last_interaction_payload, stage="resume_prompt")
                and "create_multiple_choice" in tool_names
            ):
                resume_action = _selected_interaction_value(interaction_response) or "cancel_saved"
                saved_pending = (
                    saved_workflow_state.get("pending_interaction")
                    if isinstance(saved_workflow_state, dict) and isinstance(saved_workflow_state.get("pending_interaction"), dict)
                    else None
                )
                if resume_action == "resume_saved" and isinstance(saved_pending, dict):
                    response_text = json.dumps(saved_pending, ensure_ascii=False)
                    response_parts = [DataPart(data=saved_pending)]
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
                if resume_action == "start_over":
                    saved_workflow_state = None
                    await _save_workflow_state(context_id=task.context_id, metadata=metadata, workflow_state=None)
                else:
                    await _save_workflow_state(context_id=task.context_id, metadata=metadata, workflow_state=None)
                    response_text = "Saved onboarding was canceled. When you are ready, I can start a fresh onboarding flow."
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

            if (
                interaction_response is not None
                and _is_onboarding_payload(last_interaction_payload, stage="scope_picker")
                and "create_wizard_flow" in tool_names
            ):
                selected_scope = _selected_interaction_value(interaction_response) or "full_setup"
                try:
                    interaction_output = await tool_executor.call_tool(
                        name="create_wizard_flow",
                        arguments=_onboarding_wizard_arguments(selected_scope),
                        ctx=tool_ctx,
                    )
                except Exception:
                    interaction_output = None

                if isinstance(interaction_output, dict):
                    interaction_output = await _rewrite_relation_interaction_dict(
                        interaction_output,
                        tool_specs=tool_specs,
                        tool_executor=tool_executor,
                        tool_ctx=tool_ctx,
                    )
                    interaction_output = _with_interaction_metadata(
                        interaction_output,
                        workflow="inventory_onboarding",
                        workflow_stage="wizard",
                        onboarding_scope=selected_scope,
                    )
                    workflow_state = {
                        "workflow": "inventory_onboarding",
                        "status": "collecting",
                        "stage": "wizard",
                        "scope": selected_scope,
                        "pending_interaction": interaction_output,
                    }
                    company_context = await _maybe_active_company_context()
                    if company_context:
                        workflow_state["company_context"] = company_context
                    await _save_workflow_state(
                        context_id=task.context_id,
                        metadata=metadata,
                        workflow_state=workflow_state,
                    )
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
                interaction_response is not None
                and _is_onboarding_payload(last_interaction_payload, stage="wizard")
                and "create_multiple_choice" in tool_names
            ):
                selected_scope = str(last_interaction_payload.get("onboarding_scope") or "full_setup").strip() or "full_setup"
                if bool(interaction_response.get("skipped")):
                    workflow_state = {
                        "workflow": "inventory_onboarding",
                        "status": "paused",
                        "stage": "wizard",
                        "scope": selected_scope,
                        "pending_interaction": last_interaction_payload,
                    }
                    partial_responses = interaction_response.get("partial_responses")
                    if isinstance(partial_responses, dict):
                        workflow_state["existing_responses"] = partial_responses
                    company_context = await _maybe_active_company_context()
                    if company_context:
                        workflow_state["company_context"] = company_context
                    await _save_workflow_state(
                        context_id=task.context_id,
                        metadata=metadata,
                        workflow_state=workflow_state,
                    )
                    response_text = "Onboarding paused. When you are ready, I can resume the saved setup workflow."
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

                onboarding_data = _normalize_onboarding_wizard_data(
                    selected_scope,
                    interaction_response,
                    wizard_payload=last_interaction_payload,
                )
                company_context = await _maybe_active_company_context()
                if company_context:
                    onboarding_data["company_context"] = company_context
                summary = _onboarding_summary_text(selected_scope, onboarding_data)
                try:
                    interaction_output = await tool_executor.call_tool(
                        name="create_multiple_choice",
                        arguments=_onboarding_review_picker_arguments(summary),
                        ctx=tool_ctx,
                    )
                except Exception:
                    interaction_output = None

                if isinstance(interaction_output, dict):
                    interaction_output = _with_interaction_metadata(
                        interaction_output,
                        workflow="inventory_onboarding",
                        workflow_stage="review",
                        onboarding_scope=selected_scope,
                        onboarding_data=onboarding_data,
                        onboarding_summary=summary,
                    )
                    workflow_state = {
                        "workflow": "inventory_onboarding",
                        "status": "awaiting_review",
                        "stage": "review",
                        "scope": selected_scope,
                        "summary": summary,
                        "onboarding_data": onboarding_data,
                        "pending_interaction": interaction_output,
                        "created_operations": (
                            saved_workflow_state.get("created_operations")
                            if isinstance(saved_workflow_state, dict) and isinstance(saved_workflow_state.get("created_operations"), dict)
                            else {}
                        ),
                    }
                    if company_context:
                        workflow_state["company_context"] = company_context
                    await _save_workflow_state(
                        context_id=task.context_id,
                        metadata=metadata,
                        workflow_state=workflow_state,
                    )
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

            if interaction_response is not None and (
                _is_onboarding_payload(last_interaction_payload, stage="review")
                or _is_onboarding_payload(last_interaction_payload, stage="retry")
            ):
                selected_action = _selected_interaction_value(interaction_response) or "cancel_onboarding"
                selected_scope = str(last_interaction_payload.get("onboarding_scope") or "full_setup").strip() or "full_setup"
                onboarding_data = (
                    last_interaction_payload.get("onboarding_data")
                    if isinstance(last_interaction_payload.get("onboarding_data"), dict)
                    else {}
                )
                onboarding_summary = str(last_interaction_payload.get("onboarding_summary") or "").strip() or _onboarding_summary_text(selected_scope, onboarding_data)
                created_operations = (
                    last_interaction_payload.get("created_operations")
                    if isinstance(last_interaction_payload.get("created_operations"), dict)
                    else (
                        saved_workflow_state.get("created_operations")
                        if isinstance(saved_workflow_state, dict) and isinstance(saved_workflow_state.get("created_operations"), dict)
                        else {}
                    )
                )
                failed_operations = (
                    last_interaction_payload.get("failed_operations")
                    if isinstance(last_interaction_payload.get("failed_operations"), list)
                    else []
                )
                company_context = (
                    last_interaction_payload.get("company_context")
                    if isinstance(last_interaction_payload.get("company_context"), dict)
                    else await _maybe_active_company_context()
                )

                if selected_action == "revise_answers" and "create_wizard_flow" in tool_names:
                    try:
                        interaction_output = await tool_executor.call_tool(
                            name="create_wizard_flow",
                            arguments=_onboarding_wizard_arguments(selected_scope),
                            ctx=tool_ctx,
                        )
                    except Exception:
                        interaction_output = None

                    if isinstance(interaction_output, dict):
                        interaction_output = await _rewrite_relation_interaction_dict(
                            interaction_output,
                            tool_specs=tool_specs,
                            tool_executor=tool_executor,
                            tool_ctx=tool_ctx,
                        )
                        interaction_output = _with_interaction_metadata(
                            interaction_output,
                            workflow="inventory_onboarding",
                            workflow_stage="wizard",
                            onboarding_scope=selected_scope,
                        )
                        raw_response = (
                            onboarding_data.get("raw_response")
                            if isinstance(onboarding_data.get("raw_response"), dict)
                            else {}
                        )
                        existing_responses = (
                            raw_response.get("all_responses")
                            if isinstance(raw_response.get("all_responses"), dict)
                            else saved_workflow_state.get("existing_responses")
                            if isinstance(saved_workflow_state, dict) and isinstance(saved_workflow_state.get("existing_responses"), dict)
                            else {}
                        )
                        if existing_responses:
                            interaction_output["existing_responses"] = existing_responses
                        workflow_state = {
                            "workflow": "inventory_onboarding",
                            "status": "collecting",
                            "stage": "wizard",
                            "scope": selected_scope,
                            "summary": onboarding_summary,
                            "onboarding_data": onboarding_data,
                            "pending_interaction": interaction_output,
                            "created_operations": created_operations,
                        }
                        if company_context:
                            workflow_state["company_context"] = company_context
                        await _save_workflow_state(
                            context_id=task.context_id,
                            metadata=metadata,
                            workflow_state=workflow_state,
                        )
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

                if selected_action == "cancel_onboarding":
                    await _save_workflow_state(context_id=task.context_id, metadata=metadata, workflow_state=None)
                    response_text = "Onboarding canceled for now. When you are ready, I can restart the setup flow."
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
                        parts=[TextPart(text="Applying the onboarding setup plan now.")],
                        context_id=task.context_id,
                    ),
                )

                planned_operations = _onboarding_plan_operations(
                    scope=selected_scope,
                    onboarding_data=onboarding_data,
                    tool_specs=tool_specs,
                    company_context=company_context,
                )
                created_map = {
                    key: value for key, value in created_operations.items() if isinstance(value, dict)
                }
                failed_items: list[dict[str, Any]] = []
                any_tool_executed = False

                for operation in planned_operations:
                    semantic_key = str(operation.get("semantic_key") or "").strip()
                    if not semantic_key or semantic_key in created_map:
                        continue
                    tool_name = str(operation.get("tool_name") or "").strip()
                    if tool_name not in tool_names:
                        discovery_failures = _tool_discovery_failures_for_name(tool_executor, tool_name)
                        failed_items.append(
                            _annotate_failed_operation(
                                {
                                    "label": operation.get("label"),
                                    "tool_name": tool_name,
                                    "reason": "tool_unavailable",
                                    **({"discovery_failures": discovery_failures} if discovery_failures else {}),
                                }
                            )
                        )
                        continue
                    missing_required = operation.get("missing_required")
                    if isinstance(missing_required, list) and missing_required:
                        failed_items.append(
                            _annotate_failed_operation(
                                {
                                    "label": operation.get("label"),
                                    "tool_name": tool_name,
                                    "reason": "missing_required_arguments",
                                    "missing": list(missing_required),
                                }
                            )
                        )
                        continue

                    try:
                        raw_arguments = operation.get("arguments") if isinstance(operation.get("arguments"), dict) else {}
                        resolved_arguments = _resolve_created_result_refs(raw_arguments, created_map)
                        output = await tool_executor.call_tool(
                            name=tool_name,
                            arguments=resolved_arguments,
                            ctx=tool_ctx,
                        )
                        any_tool_executed = True
                        created_map[semantic_key] = {
                            "label": operation.get("label"),
                            "tool_name": tool_name,
                            "operation_type": operation.get("operation_type"),
                            "arguments": resolved_arguments,
                            "result": output if isinstance(output, dict) else {"value": str(output)},
                        }
                    except (KeyError, ValueError) as exc:
                        failed_items.append(
                            _annotate_failed_operation(
                                {
                                    "label": operation.get("label"),
                                    "tool_name": tool_name,
                                    "reason": "dependency_resolution_failed",
                                    "error": str(exc),
                                }
                            )
                        )
                    except Exception as exc:
                        failed_items.append(
                            _annotate_failed_operation(
                                {
                                    "label": operation.get("label"),
                                    "tool_name": tool_name,
                                    "reason": "tool_error",
                                    "error": str(exc),
                                }
                            )
                        )

                if not any_tool_executed and not created_map and "delegate_to_agent" in tool_names:
                    fallback_agent = _onboarding_target_agent(selected_scope)
                    try:
                        delegated = await tool_executor.call_tool(
                            name="delegate_to_agent",
                            arguments={
                                "request": _onboarding_creation_request(selected_scope, onboarding_data),
                                "agent_name": fallback_agent,
                            },
                            ctx=tool_ctx,
                        )
                    except Exception as exc:
                        failed_items.append(
                            _annotate_failed_operation(
                                {
                                    "label": "delegated onboarding submission",
                                    "reason": "tool_error",
                                    "error": str(exc),
                                }
                            )
                        )
                    else:
                        delegated_response = _coerce_delegated_response(delegated, fallback_agent_name=fallback_agent)
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
                            if delegated_response["delegated_final_state"] == TaskState.input_required:
                                await _save_workflow_state(
                                    context_id=task.context_id,
                                    metadata=metadata,
                                    workflow_state={
                                        "workflow": "inventory_onboarding",
                                        "status": "awaiting_input",
                                        "stage": "delegated_follow_up",
                                        "scope": selected_scope,
                                        "summary": onboarding_summary,
                                        "onboarding_data": onboarding_data,
                                        "pending_interaction": delegated_response["response_parts"][0].data
                                        if delegated_response["response_parts"]
                                        and isinstance(delegated_response["response_parts"][0], DataPart)
                                        else None,
                                        "created_operations": created_map,
                                    },
                                )
                                return
                            await _save_workflow_state(context_id=task.context_id, metadata=metadata, workflow_state=None)
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

                if failed_items and "create_multiple_choice" in tool_names:
                    try:
                        interaction_output = await tool_executor.call_tool(
                            name="create_multiple_choice",
                            arguments=_onboarding_retry_picker_arguments(
                                summary=onboarding_summary,
                                created_operations=created_map,
                                failed_operations=failed_items,
                            ),
                            ctx=tool_ctx,
                        )
                    except Exception:
                        interaction_output = None

                    if isinstance(interaction_output, dict):
                        interaction_output = _with_interaction_metadata(
                            interaction_output,
                            workflow="inventory_onboarding",
                            workflow_stage="retry",
                            onboarding_scope=selected_scope,
                            onboarding_data=onboarding_data,
                            onboarding_summary=onboarding_summary,
                            created_operations=created_map,
                            failed_operations=failed_items,
                        )
                        if company_context:
                            interaction_output["company_context"] = company_context
                        workflow_state = {
                            "workflow": "inventory_onboarding",
                            "status": "partial_failure",
                            "stage": "retry",
                            "scope": selected_scope,
                            "summary": onboarding_summary,
                            "onboarding_data": onboarding_data,
                            "pending_interaction": interaction_output,
                            "created_operations": created_map,
                            "failed_operations": failed_items,
                        }
                        if company_context:
                            workflow_state["company_context"] = company_context
                        await _save_workflow_state(
                            context_id=task.context_id,
                            metadata=metadata,
                            workflow_state=workflow_state,
                        )
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

                response_text = _onboarding_completed_text(created_map)
                response_parts = [TextPart(text=response_text)]
                yield Artifact(
                    name="onboarding.created_operations",
                    parts=[DataPart(data={"operations": created_map})],
                )
                yield Artifact(name="result", parts=response_parts)
                yield TaskStatus(
                    state=TaskState.completed,
                    message=Message(
                        role=Role.agent,
                        parts=response_parts,
                        context_id=task.context_id,
                    ),
                )
                await _save_workflow_state(context_id=task.context_id, metadata=metadata, workflow_state=None)
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

            if "create_multiple_choice" in tool_names:
                if saved_workflow_state and user_text_for_memory:
                    normalized_text = _normalize_user_text(user_text_for_memory)
                    if not any(phrase in normalized_text for phrase in ("start over", "restart", "new onboarding")):
                        try:
                            interaction_output = await tool_executor.call_tool(
                                name="create_multiple_choice",
                                arguments=_onboarding_resume_picker_arguments(saved_workflow_state),
                                ctx=tool_ctx,
                            )
                        except Exception:
                            interaction_output = None
                        if isinstance(interaction_output, dict):
                            interaction_output = _with_interaction_metadata(
                                interaction_output,
                                workflow="inventory_onboarding",
                                workflow_stage="resume_prompt",
                            )
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

                direct_scope = _infer_onboarding_scope_from_text(user_text_for_memory or "")
                direct_prefill = _parse_onboarding_prefill_from_text(
                    direct_scope or "full_setup",
                    user_text_for_memory or "",
                )
                if direct_scope and direct_prefill and "create_wizard_flow" in tool_names:
                    company_context = await _maybe_active_company_context()
                    try:
                        interaction_output = await tool_executor.call_tool(
                            name="create_wizard_flow",
                            arguments=_onboarding_wizard_arguments(direct_scope),
                            ctx=tool_ctx,
                        )
                    except Exception:
                        interaction_output = None

                    if isinstance(interaction_output, dict):
                        interaction_output = await _rewrite_relation_interaction_dict(
                            interaction_output,
                            tool_specs=tool_specs,
                            tool_executor=tool_executor,
                            tool_ctx=tool_ctx,
                        )
                        existing_responses = _build_onboarding_existing_responses(
                            direct_scope,
                            wizard_payload=interaction_output,
                            prefill_data=direct_prefill,
                        )
                        if existing_responses:
                            interaction_output["existing_responses"] = existing_responses
                        interaction_output = _with_interaction_metadata(
                            interaction_output,
                            workflow="inventory_onboarding",
                            workflow_stage="wizard",
                            onboarding_scope=direct_scope,
                        )
                        interaction_output["description"] = (
                            "I prefilled this setup from your message. Review it, correct anything that is off, "
                            "and complete any remaining fields before I create anything."
                        )
                        workflow_state = {
                            "workflow": "inventory_onboarding",
                            "status": "collecting",
                            "stage": "wizard",
                            "scope": direct_scope,
                            "pending_interaction": interaction_output,
                            "existing_responses": existing_responses,
                        }
                        if company_context:
                            workflow_state["company_context"] = company_context
                        await _save_workflow_state(
                            context_id=task.context_id,
                            metadata=metadata,
                            workflow_state=workflow_state,
                        )
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

                company_context = await _maybe_active_company_context()
                description = "Choose the setup area you want to complete first. I will guide you step by step."
                if isinstance(company_context, dict):
                    company_name = str(company_context.get("name") or "").strip()
                    if company_name:
                        description = f"Current company: {company_name}\n\n{description}"
                try:
                    interaction_output = await tool_executor.call_tool(
                        name="create_multiple_choice",
                        arguments=_onboarding_scope_picker_arguments(description=description),
                        ctx=tool_ctx,
                    )
                except Exception:
                    interaction_output = None

                if isinstance(interaction_output, dict):
                    interaction_output = _with_interaction_metadata(
                        interaction_output,
                        workflow="inventory_onboarding",
                        workflow_stage="scope_picker",
                    )
                    workflow_state = {
                        "workflow": "inventory_onboarding",
                        "status": "awaiting_scope",
                        "stage": "scope_picker",
                        "pending_interaction": interaction_output,
                    }
                    if company_context:
                        workflow_state["company_context"] = company_context
                    await _save_workflow_state(
                        context_id=task.context_id,
                        metadata=metadata,
                        workflow_state=workflow_state,
                    )
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

        if agent_name in {"inventory_setup", "product_catalog_admin", "inventory_fulfillment", "inventory_procurement", "product_merchandising", "product_pricing"} and tool_executor is not None:
            interaction_payload_matches = False
            if agent_name == "inventory_setup":
                interaction_payload_matches = _is_inventory_setup_payload(last_interaction_payload, stage="form")
            elif isinstance(last_interaction_payload, dict):
                interaction_payload_matches = (
                    str(last_interaction_payload.get("workflow") or "").strip().lower() == f"{agent_name}_mutation"
                    and str(last_interaction_payload.get("workflow_stage") or "").strip().lower() == "form"
                )

            if interaction_response is not None and interaction_payload_matches:
                action = str(last_interaction_payload.get("mutation_action") or "").strip()
                form_data = _inventory_setup_form_response_data(interaction_response)
                if agent_name == "inventory_setup":
                    tool_name, arguments, missing_required = _inventory_setup_execute_action(
                        action=action,
                        form_data=form_data,
                        tool_specs=tool_specs,
                    )
                elif agent_name == "product_catalog_admin":
                    tool_name, arguments, missing_required = _product_catalog_admin_execute_action(
                        action=action,
                        form_data=form_data,
                        tool_specs=tool_specs,
                    )
                elif agent_name == "inventory_fulfillment":
                    tool_name, arguments, missing_required = _inventory_fulfillment_execute_action(
                        action=action,
                        form_data=form_data,
                        tool_specs=tool_specs,
                    )
                elif agent_name == "inventory_procurement":
                    tool_name, arguments, missing_required = _inventory_procurement_execute_action(
                        action=action,
                        form_data=form_data,
                        tool_specs=tool_specs,
                    )
                elif agent_name == "product_merchandising":
                    tool_name, arguments, missing_required = _product_merchandising_execute_action(
                        action=action,
                        form_data=form_data,
                        tool_specs=tool_specs,
                    )
                else:
                    tool_name, arguments, missing_required = _product_pricing_execute_action(
                        action=action,
                        form_data=form_data,
                        tool_specs=tool_specs,
                    )
                if missing_required:
                    response_text = (
                        "I still need a few required fields before I can continue: "
                        + ", ".join(missing_required)
                    )
                    response_parts = [TextPart(text=response_text)]
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

                try:
                    output = await tool_executor.call_tool(
                        name=tool_name,
                        arguments=arguments,
                        ctx=tool_ctx,
                    )
                except Exception as exc:
                    recovered = await _recover_relation_error_as_interaction(
                        tool_name=tool_name,
                        error_text=str(exc),
                        tool_specs=tool_specs,
                        tool_executor=tool_executor,
                        tool_ctx=tool_ctx,
                    )
                    if isinstance(recovered, dict):
                        recovered = _with_interaction_metadata(
                            recovered,
                            workflow="inventory_setup_mutation",
                            workflow_stage="form",
                            mutation_action=action,
                        )
                        response_parts = [DataPart(data=recovered)]
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
                    response_text = str(exc).strip() or "The inventory setup operation failed."
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

                success_messages = {
                    "create_inventory_item": "Inventory item created successfully.",
                    "create_stock_location": "Stock location created successfully.",
                    "update_stock_location_parent": "Stock location parent updated successfully.",
                    "create_product": "Product created successfully.",
                    "update_product": "Product updated successfully.",
                    "transfer_location_stock": "Stock transfer completed successfully.",
                    "adjust_inventory_item_stock": "Inventory adjustment completed successfully.",
                    "add_purchase_order_line_item": "Purchase-order line item added successfully.",
                    "update_product_merchandising": "Product merchandising updated successfully.",
                    "create_pricing_strategy": "Pricing strategy created successfully.",
                    "create_pricing_rule": "Pricing rule created successfully.",
                }
                response_text = success_messages.get(action, "Requested operation completed successfully.")
                response_parts = [TextPart(text=response_text)]
                yield Artifact(name=f"{agent_name}.operation", parts=[DataPart(data=output if isinstance(output, dict) else {"value": output})])
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

            if interaction_response is None and user_text_for_memory:
                action = None
                if agent_name == "inventory_setup":
                    action = _inventory_setup_action_from_text(user_text_for_memory)
                elif agent_name == "product_catalog_admin":
                    action = _product_catalog_admin_action_from_text(user_text_for_memory)
                elif agent_name == "inventory_fulfillment":
                    action = _inventory_fulfillment_action_from_text(user_text_for_memory)
                elif agent_name == "inventory_procurement":
                    action = _inventory_procurement_action_from_text(user_text_for_memory)
                elif agent_name == "product_merchandising":
                    action = _product_merchandising_action_from_text(user_text_for_memory)
                else:
                    action = _product_pricing_action_from_text(user_text_for_memory)
                if action:
                    if agent_name == "inventory_setup":
                        interaction_output = await _inventory_setup_dynamic_form_payload(
                            action=action,
                            text=user_text_for_memory,
                            tool_names=tool_names,
                            tool_specs=tool_specs,
                            tool_executor=tool_executor,
                            tool_ctx=tool_ctx,
                        )
                    elif agent_name == "product_catalog_admin":
                        interaction_output = await _product_catalog_admin_dynamic_form_payload(
                            action=action,
                            text=user_text_for_memory,
                            tool_specs=tool_specs,
                            tool_executor=tool_executor,
                            tool_ctx=tool_ctx,
                        )
                    elif agent_name == "inventory_fulfillment":
                        interaction_output = await _inventory_fulfillment_dynamic_form_payload(
                            action=action,
                            text=user_text_for_memory,
                            tool_names=tool_names,
                            tool_specs=tool_specs,
                            tool_executor=tool_executor,
                            tool_ctx=tool_ctx,
                        )
                    elif agent_name == "inventory_procurement":
                        interaction_output = await _inventory_procurement_dynamic_form_payload(
                            action=action,
                            text=user_text_for_memory,
                            tool_names=tool_names,
                            tool_specs=tool_specs,
                            tool_executor=tool_executor,
                            tool_ctx=tool_ctx,
                        )
                    elif agent_name == "product_merchandising":
                        interaction_output = await _product_merchandising_dynamic_form_payload(
                            action=action,
                            text=user_text_for_memory,
                            tool_specs=tool_specs,
                            tool_executor=tool_executor,
                            tool_ctx=tool_ctx,
                        )
                    else:
                        interaction_output = await _product_pricing_dynamic_form_payload(
                            action=action,
                            text=user_text_for_memory,
                            tool_specs=tool_specs,
                            tool_executor=tool_executor,
                            tool_ctx=tool_ctx,
                        )
                    if isinstance(interaction_output, dict):
                        if "create_dynamic_form" in tool_names:
                            try:
                                rendered_output = await tool_executor.call_tool(
                                    name="create_dynamic_form",
                                    arguments={
                                        "title": interaction_output.get("title"),
                                        "description": interaction_output.get("description"),
                                        "fields": interaction_output.get("fields"),
                                    },
                                    ctx=tool_ctx,
                                )
                            except Exception:
                                rendered_output = interaction_output
                            else:
                                if isinstance(rendered_output, dict):
                                    rendered_output.update(
                                        {
                                            "current_values": interaction_output.get("current_values", {}),
                                            "workflow": interaction_output.get("workflow"),
                                            "workflow_stage": interaction_output.get("workflow_stage"),
                                            "mutation_action": interaction_output.get("mutation_action"),
                                        }
                                    )
                                    interaction_output = rendered_output
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
            agent_name in ROUTER_AGENT_NAMES
            and tool_executor is not None
            and "delegate_to_agent" in tool_names
            and "list_available_agents" in tool_names
            and user_text_for_memory
            and interaction_response is None
            and not _is_host_introspection_query(user_text_for_memory)
        ):
            try:
                router_listing_raw = await tool_executor.call_tool(
                    name="list_available_agents",
                    arguments={},
                    ctx=tool_ctx,
                )
            except Exception:
                router_listing_raw = None
            router_listing = _coerce_agent_listing(router_listing_raw)
            selected_agent = _select_router_delegation_agent(user_text_for_memory, router_listing.get("agents") or [])

            if selected_agent:
                yield TaskStatus(
                    state=TaskState.working,
                    message=Message(
                        role=Role.agent,
                        parts=[TextPart(text=f"Delegating this request to the {selected_agent} specialist agent.")],
                        context_id=task.context_id,
                    ),
                )

                try:
                    delegated = await tool_executor.call_tool(
                        name="delegate_to_agent",
                        arguments={
                            "request": user_text_for_memory,
                            "agent_name": selected_agent,
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

                orchestration_output = await _update_host_orchestration_state_after_delegation(
                    delegated_response=delegated_response,
                    original_request=user_text_for_memory,
                    orchestration_plan=None,
                )
                if isinstance(orchestration_output, dict):
                    response_parts = [DataPart(data=orchestration_output)]
                    response_text = json.dumps(orchestration_output, ensure_ascii=False)
                    yield Artifact(name="result", parts=response_parts)
                    yield TaskStatus(
                        state=TaskState.input_required,
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
            and "delegate_to_agent" in tool_names
            and user_text_for_memory
            and not _is_host_introspection_query(user_text_for_memory)
        ):
            agent_listing = await _load_host_agent_listing()
            agent_summaries = agent_listing.get("agents")
            inferred_agent = _infer_domain_agent_name(user_text_for_memory)
            available_names = _available_agent_names(agent_summaries)
            registered_names = _agent_listing_names(agent_listing, "registered_agents")
            if inferred_agent and inferred_agent not in available_names and (available_names or registered_names):
                if _is_host_availability_query(user_text_for_memory):
                    response_text = _host_unavailable_agent_text(
                        agent_name=inferred_agent,
                        available_names=available_names,
                        registered_names=registered_names,
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

                orchestration_output = await _update_host_orchestration_state_after_delegation(
                    delegated_response=delegated_response,
                    original_request=user_text_for_memory,
                    orchestration_plan=_host_orchestration_plan(user_text_for_memory, agent_summaries),
                )
                if isinstance(orchestration_output, dict):
                    response_parts = [DataPart(data=orchestration_output)]
                    response_text = json.dumps(orchestration_output, ensure_ascii=False)
                    yield Artifact(name="result", parts=response_parts)
                    yield TaskStatus(
                        state=TaskState.input_required,
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
                interaction_output = next(
                    (
                        result.output
                        for result in tool_results
                        if not result.is_error
                        and isinstance(result.output, dict)
                        and str(result.output.get("interaction_type") or result.output.get("type") or "").strip()
                    ),
                    None,
                )
                if isinstance(interaction_output, dict):
                    response_parts = [DataPart(data=interaction_output)]
                    response_text = json.dumps(interaction_output, ensure_ascii=False)
                    yield Artifact(name="result", parts=response_parts)
                    yield TaskStatus(
                        state=TaskState.input_required,
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

                relation_error_interaction: dict[str, Any] | None = None
                for result in tool_results:
                    if not result.is_error or not isinstance(result.output, dict):
                        continue
                    failed_tool_name = tool_call_names.get(result.tool_call_id)
                    if not failed_tool_name:
                        continue
                    relation_error_interaction = await _recover_relation_error_as_interaction(
                        tool_name=failed_tool_name,
                        error_text=str(result.output.get("error") or "").strip() or None,
                        tool_specs=tool_specs,
                        tool_executor=tool_executor,
                        tool_ctx=tool_ctx,
                    )
                    if relation_error_interaction is not None:
                        break
                if isinstance(relation_error_interaction, dict):
                    response_text = json.dumps(relation_error_interaction, ensure_ascii=False)
                    response_parts = [DataPart(data=relation_error_interaction)]
                    break
                messages2.append(
                    HumanMessage(
                        content=[p.model_dump(by_alias=True, exclude_none=True) for p in tool_results]
                    )
                )

            if not response_parts:
                response_parts = [TextPart(text="Tool execution limit reached.")]
                response_text = "Tool execution limit reached."

        if tool_executor is not None and tool_specs and response_parts:
            response_parts = await _rewrite_relation_interaction_parts(
                response_parts,
                tool_specs=tool_specs,
                tool_executor=tool_executor,
                tool_ctx=tool_ctx,
            )
            interaction_payload = _interaction_payload_from_parts(response_parts)
            if interaction_payload is not None:
                rewritten_text = _text_from_parts(response_parts)
                response_text = rewritten_text or json.dumps(interaction_payload, ensure_ascii=False)

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
