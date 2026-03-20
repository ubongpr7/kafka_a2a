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
                        "name": "primary_location_name",
                        "type": "text",
                        "label": "Primary Location Name",
                        "required": True,
                        "placeholder": "Main Warehouse",
                    },
                    {
                        "name": "primary_location_type",
                        "type": "select",
                        "label": "Primary Location Type",
                        "required": True,
                        "options": _select_options(
                            [
                                ("warehouse", "Warehouse"),
                                ("store", "Store"),
                                ("backroom", "Back Room"),
                                ("fulfillment", "Fulfillment Center"),
                                ("other", "Other"),
                            ]
                        ),
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
                "description": "Define the first inventory ledger you want to create.",
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
                        "placeholder": "Primary sellable stock ledger for the business.",
                    },
                    {
                        "name": "related_location_name",
                        "type": "text",
                        "label": "Primary Location for This Inventory",
                        "required": False,
                        "placeholder": "Main Warehouse",
                    },
                    {
                        "name": "category_name",
                        "type": "text",
                        "label": "Default Category",
                        "required": False,
                        "placeholder": "General Merchandise",
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
                        "name": "product_category",
                        "type": "text",
                        "label": "Default Product Category",
                        "required": False,
                        "placeholder": "Beverages",
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
                    "name": "primary_location_name",
                    "type": "text",
                    "label": "Primary Location Name",
                    "required": True,
                    "placeholder": "Main Warehouse",
                },
                {
                    "name": "primary_location_type",
                    "type": "select",
                    "label": "Primary Location Type",
                    "required": True,
                    "options": _select_options(
                        [
                            ("warehouse", "Warehouse"),
                            ("store", "Store"),
                            ("backroom", "Back Room"),
                            ("fulfillment", "Fulfillment Center"),
                            ("other", "Other"),
                        ]
                    ),
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
            "title": "Inventory Ledger",
            "description": "Define the first inventory ledger to create.",
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
                    "placeholder": "Primary sellable stock ledger for the business.",
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


def _split_multiline_values(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    entries: list[str] = []
    for raw in re.split(r"[\n,]+", value):
        cleaned = raw.strip().strip("-").strip()
        if cleaned:
            entries.append(cleaned)
    return entries


def _normalize_onboarding_wizard_data(scope: str, response: dict[str, Any]) -> dict[str, Any]:
    responses = response.get("all_responses") if isinstance(response.get("all_responses"), dict) else {}
    steps = _onboarding_wizard_steps(scope)
    step_values: dict[str, dict[str, Any]] = {}
    flat: dict[str, Any] = {}

    for index, step in enumerate(steps):
        raw_step = responses.get(f"step_{index}")
        if not isinstance(raw_step, dict):
            continue
        step_id = str(step.get("id") or f"step_{index}")
        step_values[step_id] = raw_step
        for key, value in raw_step.items():
            if value in ("", None, [], {}):
                continue
            flat[key] = value

    return {
        "scope": scope,
        "steps": step_values,
        "flat": flat,
        "raw_response": response,
    }


def _onboarding_summary_text(scope: str, data: dict[str, Any]) -> str:
    flat = data.get("flat") if isinstance(data.get("flat"), dict) else {}
    lines = [f"Scope: {ONBOARDING_SCOPE_LABELS.get(scope, scope.replace('_', ' ').title())}"]

    primary_location = str(flat.get("primary_location_name") or "").strip()
    primary_location_type = str(flat.get("primary_location_type") or "").strip()
    if primary_location:
        label = primary_location
        if primary_location_type:
            label = f"{label} ({primary_location_type})"
        lines.append(f"Primary location: {label}")

    additional_locations = _split_multiline_values(flat.get("additional_locations"))
    if additional_locations:
        lines.append("Additional locations: " + ", ".join(additional_locations))

    categories = _split_multiline_values(flat.get("category_names"))
    if categories:
        lines.append("Categories: " + ", ".join(categories))

    inventory_name = str(flat.get("default_inventory_name") or "").strip()
    if inventory_name:
        lines.append(f"Inventory ledger: {inventory_name}")

    inventory_description = str(flat.get("inventory_description") or "").strip()
    if inventory_description:
        lines.append(f"Inventory note: {inventory_description}")

    related_location_name = str(flat.get("related_location_name") or "").strip()
    if related_location_name:
        lines.append(f"Ledger location: {related_location_name}")

    category_name = str(flat.get("category_name") or "").strip()
    if category_name:
        lines.append(f"Default category: {category_name}")

    product_names = _split_multiline_values(flat.get("product_names") or flat.get("initial_product_names"))
    if product_names:
        lines.append("Products: " + ", ".join(product_names))

    product_category = str(flat.get("product_category") or "").strip()
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
        "Create stock locations, inventory categories, and inventory ledgers as applicable to the collected data, "
        "rather than only describing them. If any required detail is missing, ask one concise follow-up question.\n"
        f"Collected onboarding data JSON:\n{serialized}"
    )


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
        parts.append(f"{counts['inventory']} inventory ledger" + ("s" if counts["inventory"] != 1 else ""))
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


def _build_stock_location_operation(
    *,
    tool_specs: list[ToolSpec],
    company_context: dict[str, Any] | None,
    location_name: str,
    location_type: str | None,
    primary: bool,
) -> dict[str, Any]:
    tool_name = "inventory.create_stock_location"
    spec = _tool_spec_by_name(tool_specs, tool_name)
    arguments = _company_context_arguments(spec, company_context)
    _set_schema_arg(arguments, spec, ["name", "location_name", "locationName", "stock_location_name", "stockLocationName"], location_name)
    _set_schema_arg(arguments, spec, ["type", "location_type", "locationType", "stock_location_type", "stockLocationType"], location_type)
    _set_schema_arg(arguments, spec, ["is_primary", "isPrimary", "primary"], primary)
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
) -> dict[str, Any]:
    tool_name = "inventory.create_inventory_category"
    spec = _tool_spec_by_name(tool_specs, tool_name)
    arguments = _company_context_arguments(spec, company_context)
    _set_schema_arg(arguments, spec, ["name", "category_name", "categoryName", "title"], category_name)
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
    related_location_name: str | None,
    category_name: str | None,
) -> dict[str, Any]:
    tool_name = "inventory.create_inventory"
    spec = _tool_spec_by_name(tool_specs, tool_name)
    arguments = _company_context_arguments(spec, company_context)
    _set_schema_arg(arguments, spec, ["name", "inventory_name", "inventoryName", "title"], inventory_name)
    _set_schema_arg(arguments, spec, ["description", "inventory_description", "inventoryDescription", "notes"], inventory_description)
    _set_schema_arg(arguments, spec, ["location_name", "locationName", "stock_location_name", "stockLocationName", "default_location_name", "defaultLocationName"], related_location_name)
    _set_schema_arg(arguments, spec, ["category_name", "categoryName", "inventory_category_name", "inventoryCategoryName", "default_category_name", "defaultCategoryName"], category_name)
    return {
        "tool_name": tool_name,
        "label": f"inventory ledger '{inventory_name}'",
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
    product_category: str | None,
    pos_ready: bool | None,
) -> dict[str, Any]:
    tool_name = "product.create_product"
    spec = _tool_spec_by_name(tool_specs, tool_name)
    arguments = _company_context_arguments(spec, company_context)
    base_argument_keys = set(arguments)
    _set_schema_arg(arguments, spec, ["name", "product_name", "productName", "title"], product_name)
    _set_schema_arg(arguments, spec, ["category_name", "categoryName", "product_category", "productCategory", "category"], product_category)
    _set_schema_arg(arguments, spec, ["pos_ready", "posReady", "pos_visible", "posVisible", "quick_sale", "quickSale"], pos_ready)
    payload_spec = _nested_object_tool_spec(spec, "payload")
    payload_required = "payload" in _tool_schema_required(spec)
    top_level_product_args_added = bool(set(arguments) - base_argument_keys)
    if payload_spec is not None and (payload_required or not top_level_product_args_added):
        payload_arguments: dict[str, Any] = {}
        _set_schema_arg(payload_arguments, payload_spec, ["name", "product_name", "productName", "title"], product_name)
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

    if scope in {"stock_locations", "full_setup"}:
        primary_location_name = str(flat.get("primary_location_name") or "").strip()
        primary_location_type = str(flat.get("primary_location_type") or "").strip() or None
        if primary_location_name:
            operations.append(
                _build_stock_location_operation(
                    tool_specs=tool_specs,
                    company_context=company_context,
                    location_name=primary_location_name,
                    location_type=primary_location_type,
                    primary=True,
                )
            )
        for location_name in _split_multiline_values(flat.get("additional_locations")):
            operations.append(
                _build_stock_location_operation(
                    tool_specs=tool_specs,
                    company_context=company_context,
                    location_name=location_name,
                    location_type=primary_location_type or "store",
                    primary=False,
                )
            )

    categories = _split_multiline_values(flat.get("category_names"))
    if scope in {"inventory_categories", "full_setup"}:
        for category_name in categories:
            operations.append(
                _build_inventory_category_operation(
                    tool_specs=tool_specs,
                    company_context=company_context,
                    category_name=category_name,
                )
            )

    if scope in {"inventory_setup", "full_setup"}:
        inventory_name = str(flat.get("default_inventory_name") or "").strip()
        if inventory_name:
            operations.append(
                _build_inventory_operation(
                    tool_specs=tool_specs,
                    company_context=company_context,
                    inventory_name=inventory_name,
                    inventory_description=str(flat.get("inventory_description") or "").strip() or None,
                    related_location_name=(
                        str(flat.get("related_location_name") or "").strip()
                        or str(flat.get("primary_location_name") or "").strip()
                        or None
                    ),
                    category_name=(
                        str(flat.get("category_name") or "").strip()
                        or (categories[0] if categories else None)
                    ),
                )
            )

    should_create_products = scope == "product_onboarding" or (
        scope == "full_setup" and flat.get("continue_to_product_onboarding") is True
    )
    if should_create_products:
        product_names = _split_multiline_values(flat.get("product_names") or flat.get("initial_product_names"))
        product_category = str(flat.get("product_category") or "").strip() or None
        pos_ready = flat.get("pos_ready")
        pos_ready_value = pos_ready if isinstance(pos_ready, bool) else None
        for product_name in product_names:
            operations.append(
                _build_product_operation(
                    tool_specs=tool_specs,
                    company_context=company_context,
                    product_name=product_name,
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
                try:
                    obj = obj(agent_name=agent_name)
                except TypeError:
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
            if registered_names:
                labels = ", ".join(_friendly_agent_label(name) for name in registered_names)
                if available_names != registered_names:
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

                onboarding_data = _normalize_onboarding_wizard_data(selected_scope, interaction_response)
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
                        output = await tool_executor.call_tool(
                            name=tool_name,
                            arguments=operation.get("arguments") if isinstance(operation.get("arguments"), dict) else {},
                            ctx=tool_ctx,
                        )
                        any_tool_executed = True
                        created_map[semantic_key] = {
                            "label": operation.get("label"),
                            "tool_name": tool_name,
                            "operation_type": operation.get("operation_type"),
                            "arguments": operation.get("arguments"),
                            "result": output if isinstance(output, dict) else {"value": str(output)},
                        }
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
