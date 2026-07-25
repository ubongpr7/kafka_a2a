from __future__ import annotations

import asyncio
import ast
import json
import importlib
import logging
import os
import re
from calendar import monthrange
from collections.abc import AsyncIterator, Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any, TypedDict
from urllib.parse import quote

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

logger = logging.getLogger(__name__)


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
        + "- For read-only analytics, summaries, timelines, dashboards, risks, or comparisons, prefer render_* widget tools plus create_insight_response instead of long prose.\n"
        + "- Use interaction/formatting tools only when the frontend needs structured UI such as a form, selection, confirmation, wizard, table, or insight widget.\n"
        + "- Keep insight responses widget-first and prose-light. The summary should be one sentence or less.\n"
        + "- Never perform a mutation without an explicit confirmation tool step first.\n"
        + "- If you need a tool, respond with STRICT JSON only (no markdown).\n"
        + '- Output MUST be either a single object or a list of objects shaped like: {"kind":"tool-call","name":"...","arguments":{...}}.\n'
        + '- Never output bare tool names or pseudo-tool JSON such as {"kind":"list_available_agents"} or {"kind":"create_dynamic_form"}.\n'
        + '- Never output legacy wrappers such as {"tool_code":"..."} or print(create_multiple_choice(...)) or print(delegate_to_agent(...)).\n'
        + "- You may call multiple tools in one response.\n"
        + "- After tool results are provided, respond normally with your final answer unless the tool result is already a deliberate frontend interaction payload or insight_response.\n"
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


def _text_matches_all_terms(text: str, *patterns: str) -> bool:
    if not text:
        return False
    return all(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


class InsightTimeWindow(TypedDict):
    start_date: str
    end_date: str
    anchor_date: str
    days: int
    label: str
    period: str


_NUMBER_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

_MONTH_NAME_TO_NUMBER: dict[str, int] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_QUARTER_WORD_TO_NUMBER: dict[str, int] = {
    "q1": 1,
    "first": 1,
    "1st": 1,
    "q2": 2,
    "second": 2,
    "2nd": 2,
    "q3": 3,
    "third": 3,
    "3rd": 3,
    "q4": 4,
    "fourth": 4,
    "4th": 4,
}


def _parse_relative_count(raw_value: str | None) -> int | None:
    value = str(raw_value or "").strip().lower()
    if not value:
        return None
    if value.isdigit():
        return max(1, int(value))
    return _NUMBER_WORDS.get(value)


def _parse_iso_date(raw_value: str | None) -> date | None:
    value = str(raw_value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _month_label(month_number: int) -> str:
    return date(2000, month_number, 1).strftime("%B")


def _calendar_window_for_named_month(*, month_name: str, year_value: str) -> tuple[date, date, str] | None:
    month_number = _MONTH_NAME_TO_NUMBER.get(str(month_name or "").strip().lower())
    if month_number is None:
        return None
    try:
        year = int(str(year_value or "").strip())
    except (TypeError, ValueError):
        return None
    start = date(year, month_number, 1)
    end = date(year, month_number, monthrange(year, month_number)[1])
    return start, end, f"{_month_label(month_number)} {year}"


def _calendar_window_for_named_quarter(*, quarter_token: str, year_value: str) -> tuple[date, date, str] | None:
    normalized_token = str(quarter_token or "").strip().lower().replace(" quarter", "")
    quarter_number = _QUARTER_WORD_TO_NUMBER.get(normalized_token)
    if quarter_number is None:
        return None
    try:
        year = int(str(year_value or "").strip())
    except (TypeError, ValueError):
        return None
    start_month = ((quarter_number - 1) * 3) + 1
    start = date(year, start_month, 1)
    end_month = start_month + 2
    end = date(year, end_month, monthrange(year, end_month)[1])
    return start, end, f"Q{quarter_number} {year}"


def _days_for_time_unit(count: int, unit: str) -> int:
    normalized_unit = str(unit or "").strip().lower()
    if normalized_unit == "day":
        return max(1, count)
    if normalized_unit == "week":
        return max(1, count * 7)
    if normalized_unit == "month":
        return max(1, count * 30)
    if normalized_unit == "year":
        return max(1, count * 365)
    if normalized_unit == "quarter":
        return max(1, count * 90)
    return max(1, count)


def _start_of_month(target_date: date) -> date:
    return target_date.replace(day=1)


def _end_of_month(target_date: date) -> date:
    return target_date.replace(day=monthrange(target_date.year, target_date.month)[1])


def _start_of_year(target_date: date) -> date:
    return target_date.replace(month=1, day=1)


def _end_of_year(target_date: date) -> date:
    return target_date.replace(month=12, day=31)


def _start_of_quarter(target_date: date) -> date:
    quarter_month = ((target_date.month - 1) // 3) * 3 + 1
    return target_date.replace(month=quarter_month, day=1)


def _end_of_quarter(target_date: date) -> date:
    start = _start_of_quarter(target_date)
    end_month = start.month + 2
    return date(start.year, end_month, monthrange(start.year, end_month)[1])


def _shift_calendar_months(base_date: date, months: int) -> date:
    month_index = (base_date.month - 1) + months
    year = base_date.year + (month_index // 12)
    month = (month_index % 12) + 1
    day = min(base_date.day, monthrange(year, month)[1])
    return date(year, month, day)


def _calendar_window_for_recent_period(today: date, count: int, unit: str) -> tuple[date, date] | None:
    normalized_unit = str(unit or "").strip().lower()
    if normalized_unit == "month":
        start = _start_of_month(_shift_calendar_months(today, -(count - 1)))
        return start, today
    if normalized_unit == "quarter":
        start = _start_of_quarter(_shift_calendar_months(_start_of_quarter(today), -((count - 1) * 3)))
        return start, today
    if normalized_unit == "year":
        start = date(today.year - (count - 1), 1, 1)
        return start, today
    return None


def _calendar_window_for_previous_period(today: date, unit: str) -> tuple[date, date] | None:
    normalized_unit = str(unit or "").strip().lower()
    if normalized_unit == "month":
        anchor = _shift_calendar_months(today, -1)
        return _start_of_month(anchor), _end_of_month(anchor)
    if normalized_unit == "quarter":
        anchor = _shift_calendar_months(_start_of_quarter(today), -3)
        return _start_of_quarter(anchor), _end_of_quarter(anchor)
    if normalized_unit == "year":
        year = today.year - 1
        return date(year, 1, 1), date(year, 12, 31)
    return None


def _calendar_window_for_ago_period(today: date, count: int, unit: str) -> tuple[date, date] | None:
    normalized_unit = str(unit or "").strip().lower()
    if normalized_unit == "month":
        anchor = _shift_calendar_months(today, -count)
        return _start_of_month(anchor), _end_of_month(anchor)
    if normalized_unit == "quarter":
        anchor = _shift_calendar_months(_start_of_quarter(today), -(count * 3))
        return _start_of_quarter(anchor), _end_of_quarter(anchor)
    if normalized_unit == "year":
        year = today.year - count
        return date(year, 1, 1), date(year, 12, 31)
    return None


def _build_time_window(*, start: date, end: date, label: str) -> InsightTimeWindow:
    if end < start:
        start, end = end, start
    days = max(1, (end - start).days + 1)
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "anchor_date": end.isoformat(),
        "days": days,
        "label": label,
        "period": f"{days}d",
    }


def _resolve_insight_time_window(
    text: str,
    *,
    default_days: int,
    default_label: str,
) -> InsightTimeWindow:
    normalized = _normalize_user_text(text)
    today = datetime.now(timezone.utc).date()
    if not normalized:
        return _build_time_window(
            start=today - timedelta(days=max(0, default_days - 1)),
            end=today,
            label=default_label,
        )

    if "today" in normalized:
        return _build_time_window(start=today, end=today, label="today")
    if "yesterday" in normalized:
        target_date = today - timedelta(days=1)
        return _build_time_window(start=target_date, end=target_date, label="yesterday")

    if "this week" in normalized:
        start = today - timedelta(days=today.weekday())
        return _build_time_window(start=start, end=today, label="this week")
    if "this month" in normalized:
        start = today.replace(day=1)
        return _build_time_window(start=start, end=today, label="this month")
    if "this quarter" in normalized:
        start = _start_of_quarter(today)
        return _build_time_window(start=start, end=today, label="this quarter")
    if "this year" in normalized:
        start = _start_of_year(today)
        return _build_time_window(start=start, end=today, label="this year")
    if any(token in normalized for token in ("all time", "of all time", "ever", "historical", "historically", "lifetime")):
        return _build_time_window(start=today - timedelta(days=3650), end=today, label="all time")

    explicit_range_match = re.search(
        r"\b(?:from|between)\s+(\d{4}-\d{2}-\d{2})\s+(?:to|and)\s+(\d{4}-\d{2}-\d{2})\b",
        normalized,
    )
    if explicit_range_match:
        start = _parse_iso_date(explicit_range_match.group(1))
        end = _parse_iso_date(explicit_range_match.group(2))
        if start is not None and end is not None:
            return _build_time_window(start=start, end=end, label=f"{start.isoformat()} to {end.isoformat()}")

    named_month_match = re.search(
        r"\b(?:in|for|during)\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{4})\b",
        normalized,
    )
    if named_month_match:
        month_window = _calendar_window_for_named_month(
            month_name=named_month_match.group(1),
            year_value=named_month_match.group(2),
        )
        if month_window is not None:
            return _build_time_window(start=month_window[0], end=month_window[1], label=month_window[2])

    named_quarter_match = re.search(
        r"\b(?:in|for|during)\s+(q[1-4]|first(?:\s+quarter)?|1st(?:\s+quarter)?|second(?:\s+quarter)?|2nd(?:\s+quarter)?|third(?:\s+quarter)?|3rd(?:\s+quarter)?|fourth(?:\s+quarter)?|4th(?:\s+quarter)?)\s+(\d{4})\b",
        normalized,
    )
    if named_quarter_match:
        quarter_window = _calendar_window_for_named_quarter(
            quarter_token=named_quarter_match.group(1),
            year_value=named_quarter_match.group(2),
        )
        if quarter_window is not None:
            return _build_time_window(start=quarter_window[0], end=quarter_window[1], label=quarter_window[2])

    count_match = re.search(
        r"\b(last|previous|past|over the past|for the past|in the past)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+(day|week|month|quarter|year)s?\b",
        normalized,
    )
    if count_match:
        prefix = str(count_match.group(1) or "last").strip().lower()
        count = _parse_relative_count(count_match.group(2)) or 1
        unit = str(count_match.group(3) or "day")
        if prefix in {"last", "previous"} and unit in {"month", "quarter", "year"}:
            calendar_window = _calendar_window_for_recent_period(today, count, unit)
            if calendar_window is not None:
                return _build_time_window(
                    start=calendar_window[0],
                    end=calendar_window[1],
                    label=f"last {count} {unit}{'' if count == 1 else 's'}",
                )
        days = _days_for_time_unit(count, unit)
        return _build_time_window(
            start=today - timedelta(days=max(0, days - 1)),
            end=today,
            label=f"last {count} {unit}{'' if count == 1 else 's'}",
        )

    simple_match = re.search(r"\b(past|last|previous)\s+(day|week|month|quarter|year)\b", normalized)
    if simple_match:
        prefix = str(simple_match.group(1) or "last").strip().lower()
        unit = str(simple_match.group(2) or "day")
        if prefix in {"last", "previous"} and unit in {"month", "quarter", "year"}:
            calendar_window = _calendar_window_for_previous_period(today, unit)
            if calendar_window is not None:
                label = "last quarter" if unit == "quarter" else f"last {unit}"
                return _build_time_window(
                    start=calendar_window[0],
                    end=calendar_window[1],
                    label=label,
                )
        days = _days_for_time_unit(1, unit)
        label = "last quarter" if unit == "quarter" else f"last {unit}"
        return _build_time_window(
            start=today - timedelta(days=max(0, days - 1)),
            end=today,
            label=label,
        )

    ago_match = re.search(
        r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+(day|week|month|quarter|year)s?\s+ago\b",
        normalized,
    )
    if ago_match:
        count = _parse_relative_count(ago_match.group(1)) or 1
        unit = str(ago_match.group(2) or "day")
        if unit in {"month", "quarter", "year"}:
            calendar_window = _calendar_window_for_ago_period(today, count, unit)
            if calendar_window is not None:
                return _build_time_window(
                    start=calendar_window[0],
                    end=calendar_window[1],
                    label=f"{count} {unit}{'' if count == 1 else 's'} ago",
                )
        anchor = today - timedelta(days=_days_for_time_unit(count, unit))
        days = 1 if unit == "day" else _days_for_time_unit(1, unit)
        return _build_time_window(
            start=anchor - timedelta(days=max(0, days - 1)),
            end=anchor,
            label=f"{count} {unit}{'' if count == 1 else 's'} ago",
        )

    seven_day_match = re.search(r"\b(seven|7)\s+days?\b", normalized)
    if seven_day_match:
        return _build_time_window(
            start=today - timedelta(days=6),
            end=today,
            label="last 7 days",
        )

    return _build_time_window(
        start=today - timedelta(days=max(0, default_days - 1)),
        end=today,
        label=default_label,
    )


def _strong_domain_agent_override(query: str) -> str | None:
    text = _normalize_user_text(query)
    if not text:
        return None
    if any(token in text for token in ("sales", "revenue", "order count", "orders made", "gross sales", "avg basket")):
        return "pos"
    if _text_matches_all_terms(text, r"\bsales?\b", r"\blocation\b"):
        return "pos"
    if _text_matches_all_terms(text, r"\b(top|best)\s+sellers?\b"):
        return "pos"
    if _text_matches_all_terms(text, r"\bout[\s-]*of[\s-]*stock\b", r"\bproducts?\b"):
        return "inventory"
    if _text_matches_all_terms(text, r"\bstaff\b", r"\bactivity\b"):
        return "users"
    if _text_matches_all_terms(text, r"\bsupport\b", r"\baccess\b", r"\baudit\b"):
        return "users"
    if _text_matches_all_terms(text, r"\bsubscription\b", r"\b(usage|limit|limits)\b"):
        return "users"
    if _text_matches_all_terms(text, r"\b(global|catalog)\b", r"\bimport\b"):
        return "product"
    if _text_matches_all_terms(text, r"\b(purchase order|po)\b", r"\b(receiving|lifecycle|timeline)\b"):
        return "inventory"
    return None


def _latest_history_insight_payload(history: Any) -> dict[str, Any] | None:
    payloads = _history_insight_payloads(history)
    return payloads[-1] if payloads else None


def _history_insight_payloads(history: Any) -> list[dict[str, Any]]:
    if not isinstance(history, list):
        return []
    payloads: list[dict[str, Any]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in {"assistant", "agent", "ai"}:
            continue
        sources = [item]
        message = item.get("message")
        if isinstance(message, dict):
            sources.append(message)
        for source in sources:
            for key in ("structured_payload", "structuredPayload", "payload", "data"):
                payload_candidate = source.get(key)
                if isinstance(payload_candidate, dict):
                    if str(payload_candidate.get("kind") or "").strip() == "insight_response":
                        payloads.append(payload_candidate)
                        break
                    nested = payload_candidate.get("structured_payload") or payload_candidate.get("structuredPayload")
                    if isinstance(nested, dict) and str(nested.get("kind") or "").strip() == "insight_response":
                        payloads.append(nested)
                        break
                elif isinstance(payload_candidate, str):
                    try:
                        decoded = json.loads(payload_candidate)
                    except Exception:
                        decoded = None
                    if isinstance(decoded, dict) and str(decoded.get("kind") or "").strip() == "insight_response":
                        payloads.append(decoded)
                        break
            else:
                content = source.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue
                try:
                    payload = json.loads(content)
                except Exception:
                    continue
                if isinstance(payload, dict) and str(payload.get("kind") or "").strip() == "insight_response":
                    payloads.append(payload)
                    continue
                if isinstance(payload, dict):
                    nested = payload.get("structured_payload") or payload.get("structuredPayload") or payload.get("payload") or payload.get("data")
                    if isinstance(nested, dict) and str(nested.get("kind") or "").strip() == "insight_response":
                        payloads.append(nested)
    return payloads


def _insight_row_contains_keys(row: dict[str, Any], *keywords: str) -> bool:
    keys = " ".join(str(key).lower() for key in row.keys())
    return any(keyword.lower() in keys for keyword in keywords)


def _insight_location_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    widget = _insight_widget_by_title(payload, "location")
    rows = widget.get("rows") if isinstance(widget, dict) else []
    if not rows and isinstance(widget, dict):
        rows = widget.get("data") if isinstance(widget.get("data"), list) else []
    if isinstance(rows, list):
        location_rows = [row for row in rows if isinstance(row, dict)]
        if location_rows:
            return location_rows

    candidate_widgets = payload.get("widgets")
    if not isinstance(candidate_widgets, list):
        candidate_widgets = []
    for candidate in candidate_widgets:
        if not isinstance(candidate, dict):
            continue
        widget_type = str(candidate.get("type") or "").strip().lower()
        if widget_type not in {"comparison_table", "table", "ranked_list"}:
            continue
        for key in ("rows", "data", "items"):
            value = candidate.get(key)
            if not isinstance(value, list):
                continue
            rows = [row for row in value if isinstance(row, dict)]
            if not rows:
                continue
            if any(
                _insight_row_contains_keys(row, "location", "store", "branch", "outlet", "warehouse", "site")
                and _insight_row_contains_keys(row, "sales", "revenue", "value", "amount", "total")
                for row in rows
            ):
                return rows

    for key in ("location_rows", "locations", "rows", "groups"):
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        rows = [row for row in value if isinstance(row, dict)]
        if rows and any(
            _insight_row_contains_keys(row, "location", "store", "branch", "outlet", "warehouse", "site")
            and _insight_row_contains_keys(row, "sales", "revenue", "value", "amount", "total")
            for row in rows
        ):
            return rows
    return []


def _payload_search_text(payload: dict[str, Any]) -> str:
    try:
        return json.dumps(payload, default=str).lower()
    except Exception:
        return str(payload).lower()


def _select_history_insight_payload(user_text: str, history: Any) -> dict[str, Any] | None:
    payloads = _history_insight_payloads(history)
    if not payloads:
        return None
    text = _normalize_user_text(user_text)
    wants_business = any(term in text for term in ("business", "analyst", "analysis", "analyze", "analyse", "entire system", "whole system", "owner review", "first analysis", "first review"))
    wants_location = any(term in text for term in ("location", "branch", "store", "warehouse", "outlet", "site"))
    wants_comparison = any(term in text for term in ("comparison", "compare", "product comparison", "variant comparison")) or (
        any(term in text for term in ("product", "variant"))
        and any(term in text for term in ("revenue", "sales", "units", "quantity", "orders", "generated", "sold", "leader", "led", "best"))
    )
    wants_procurement = any(term in text for term in ("purchase order", "procurement", "po ", "receiving", "supplier"))
    wants_staff = any(term in text for term in ("staff", "audit", "activity", "user"))

    if wants_business:
        terms = ("business analyst review", "recommended owner actions", "revenue posture", "entire system")
    elif wants_location:
        terms = ("location contribution", "location revenue", "store revenue", "branch revenue", "location ranking")
    elif wants_comparison:
        terms = ("product comparison table", "product revenue ranking", "product units trend", "variant comparison", "top products", "top sellers")
    elif wants_procurement:
        terms = ("receiving progress", "receiving lifecycle", "receiving activity", "purchase-order receiving")
    elif wants_staff:
        terms = ("staff audit activity", "most frequent staff actions", "audit events for", "staff activity")
    else:
        return payloads[-1]

    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, payload in enumerate(payloads):
        haystack = _payload_search_text(payload)
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, index, payload))
    if scored:
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return scored[0][2]
    if wants_business or wants_location or wants_comparison or wants_procurement or wants_staff:
        return None
    return payloads[-1]


def _is_new_scoped_insight_request(text: str) -> bool:
    if not text or text.startswith(("going back", "based on", "from that")):
        return False
    starts_like_request = bool(
        re.search(r"^(show|give|get|analyse|analyze|review|summari[sz]e|compare|list|find|tell me|what are|which are)\b", text)
    )
    if not starts_like_request:
        return False
    has_scope = bool(re.search(r"\b(today|yesterday|last|past|this month|this year|date|between|from)\b", text))
    has_domain = bool(
        re.search(
            r"\b(sales?|products?|variants?|purchase orders?|po|procurement|receiving|staff|audit|activity|stock|inventory|system|business analyst)\b",
            text,
        )
    )
    return has_scope and has_domain


def _latest_repeated_question_response_parts(user_text: str, history: Any) -> list[Any] | None:
    current = _normalize_user_text(user_text)
    if not current or not isinstance(history, list):
        return None
    scoped_insight_request = _is_new_scoped_insight_request(current)

    prior_user_index: int | None = None
    for index in range(len(history) - 1, -1, -1):
        item = history[index]
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = item.get("content")
        if role not in {"user", "human"} or not isinstance(content, str):
            continue
        if _normalize_user_text(content) == current:
            prior_user_index = index
            # The last entry is usually the current user message in direct stream history.
            if index < len(history) - 1:
                break

    if prior_user_index is None:
        return None

    for item in history[prior_user_index + 1 :]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in {"assistant", "agent", "ai"}:
            continue
        structured_payload = item.get("structured_payload") or item.get("structuredPayload")
        if isinstance(structured_payload, dict) and structured_payload:
            return [DataPart(data=structured_payload)]
        if scoped_insight_request:
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            return [TextPart(text=content.strip())]
    return None


def _insight_widget_by_title(payload: dict[str, Any], *title_terms: str) -> dict[str, Any] | None:
    widgets = payload.get("widgets")
    if not isinstance(widgets, list):
        return None
    normalized_terms = [term.strip().lower() for term in title_terms if term.strip()]
    for widget in widgets:
        if not isinstance(widget, dict):
            continue
        title = str(widget.get("title") or "").strip().lower()
        if all(term in title for term in normalized_terms):
            return widget
    return None


def _insight_metric_value(payload: dict[str, Any], label: str) -> Any:
    label_lower = label.strip().lower()
    for widget in payload.get("widgets") or []:
        if not isinstance(widget, dict) or str(widget.get("type") or "") != "metric_grid":
            continue
        for item in widget.get("data") or []:
            if isinstance(item, dict) and str(item.get("label") or "").strip().lower() == label_lower:
                return item.get("value")
    return None


def _format_plain_money(value: Any, payload: dict[str, Any]) -> str:
    currency_code = "NGN"
    for widget in payload.get("widgets") or []:
        if isinstance(widget, dict):
            currency_code = str(widget.get("currency_code") or widget.get("currency") or currency_code).upper()
    symbol = {
        "NGN": "₦",
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
        "JPY": "¥",
        "CAD": "C$",
        "AUD": "A$",
        "GHS": "₵",
        "KES": "KSh",
        "ZAR": "R",
    }.get(currency_code, f"{currency_code} ")
    try:
        return f"{symbol}{float(value or 0):,.2f}"
    except Exception:
        return str(value or "")


def _format_plain_number(value: Any) -> str:
    try:
        numeric = float(value or 0)
    except Exception:
        return str(value or "")
    return f"{numeric:,.0f}" if numeric.is_integer() else f"{numeric:,.2f}"


def _insight_procurement_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    widget = (
        _insight_widget_by_title(payload, "receiving", "progress")
        or _insight_widget_by_title(payload, "purchase", "progress")
        or _insight_widget_by_title(payload, "po", "progress")
        or _insight_widget_by_title(payload, "receiving", "lifecycle")
    )
    if not isinstance(widget, dict):
        return []
    items = widget.get("items") if isinstance(widget.get("items"), list) else []
    rows = widget.get("rows") if isinstance(widget.get("rows"), list) else []
    steps = widget.get("steps") if isinstance(widget.get("steps"), list) else []
    source = items or rows or steps
    return [item for item in source if isinstance(item, dict)]


def _insight_item_label(item: dict[str, Any]) -> str:
    for key in ("label", "title", "reference", "name", "id"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return "item"


def _insight_item_status(item: dict[str, Any]) -> str:
    for key in ("status", "state", "badge", "stage"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _insight_ranked_items(payload: dict[str, Any], *title_terms: str) -> list[dict[str, Any]]:
    widget = _insight_widget_by_title(payload, *title_terms)
    if not isinstance(widget, dict):
        return []
    items = widget.get("items") if isinstance(widget.get("items"), list) else []
    return [item for item in items if isinstance(item, dict)]


def _insight_timeline_events(payload: dict[str, Any], *title_terms: str) -> list[dict[str, Any]]:
    widget = _insight_widget_by_title(payload, *title_terms)
    if not isinstance(widget, dict):
        return []
    events = widget.get("events") if isinstance(widget.get("events"), list) else []
    return [event for event in events if isinstance(event, dict)]


def _insight_comparison_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    table = _insight_widget_by_title(payload, "product", "comparison", "table") or _insight_widget_by_title(
        payload,
        "comparison",
        "table",
    )
    rows = table.get("rows") if isinstance(table, dict) else []
    if isinstance(rows, list) and len(rows) >= 2:
        return [row for row in rows if isinstance(row, dict)]
    ranked = _insight_widget_by_title(payload, "product", "revenue", "ranking") or _insight_widget_by_title(
        payload,
        "revenue",
        "ranking",
    )
    items = ranked.get("items") if isinstance(ranked, dict) else []
    return [item for item in items if isinstance(item, dict)]


def _insight_comparison_name(item: dict[str, Any]) -> str:
    for key in ("product", "product_name", "label", "title", "name"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return "product"


def _insight_comparison_value(item: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            try:
                return float(str(value).replace(",", ""))
            except Exception:
                return 0.0
    return 0.0


def _latest_insight_follow_up_answer(
    user_text: str,
    history: Any,
    *,
    memory: ContextMemory | None = None,
) -> str | None:
    text = _normalize_user_text(user_text)
    if not text:
        return None
    if _is_simple_greeting_query(text):
        return None
    if _is_new_scoped_insight_request(text):
        return None
    payload = _select_history_insight_payload(user_text, history)
    if not payload and memory is not None and isinstance(memory.analysis, dict):
        payload = memory.analysis
    if not payload:
        return None

    # New scoped requests should run tools instead of answering from stale history.
    if any(token in text for token in ("last month", "this month", "today", "yesterday", "past 7 days", "barcode", "compare ", "new analysis")):
        return None

    timeframe = payload.get("timeframe") if isinstance(payload.get("timeframe"), dict) else {}
    timeframe_label = str(timeframe.get("label") or "").strip()
    timeframe_suffix = f" for {timeframe_label}" if timeframe_label else ""

    if re.search(r"\b(procurement|purchasing|purchase order|po|receiving|supplier)\b", text):
        procurement_items = _insight_procurement_items(payload)
        if procurement_items:
            open_items = [
                item
                for item in procurement_items
                if _insight_item_status(item)
                and not re.search(r"(received|complete|completed|closed|done)", _insight_item_status(item).lower())
            ]
            attention_items = open_items or procurement_items[:3]
            status_counts: dict[str, int] = {}
            for item in procurement_items:
                status = _insight_item_status(item) or "unknown"
                status_counts[status] = status_counts.get(status, 0) + 1
            status_summary = ", ".join(f"{status}: {_format_plain_number(count)}" for status, count in status_counts.items())
            priority = ", ".join(
                f"{_insight_item_label(item)}{f' ({_insight_item_status(item)})' if _insight_item_status(item) else ''}"
                for item in attention_items[:5]
            )

            if any(phrase in text for phrase in ("bottleneck", "block", "delay", "issue", "risk")):
                return (
                    f"From the receiving lifecycle{timeframe_suffix}, the main bottleneck is unfinished receiving work: "
                    f"{_format_plain_number(len(open_items))} of {_format_plain_number(len(procurement_items))} tracked POs are not fully received. "
                    f"Status mix: {status_summary}."
                )
            if any(phrase in text for phrase in ("status", "attention", "needs attention")):
                return f"From the receiving lifecycle{timeframe_suffix}, these statuses need attention: {status_summary}. Priority POs: {priority}."
            if any(phrase in text for phrase in ("what should", "next", "action", "do next", "recommend", "team")):
                return (
                    f"From the receiving lifecycle{timeframe_suffix}, the purchasing team should first follow up on {priority}. "
                    "Then confirm supplier ETAs, close partial receipts, and split large supplier deliveries where receiving spikes are recurring."
                )

    comparison_rows = _insight_comparison_rows(payload)
    if len(comparison_rows) >= 2:
        value_keys = (
            "sales_total",
            "total_sales",
            "total_revenue",
            "gross_sales",
            "revenue",
            "sales",
            "amount",
            "value",
        )
        ordered = sorted(comparison_rows, key=lambda item: _insight_comparison_value(item, *value_keys), reverse=True)
        leader, runner = ordered[0], ordered[1]
        leader_value = _insight_comparison_value(leader, *value_keys)
        runner_value = _insight_comparison_value(runner, *value_keys)
        if any(phrase in text for phrase in ("revenue", "sales total", "generated more", "made more", "led", "highest", "best", "top")):
            return (
                f"From the comparison{timeframe_suffix}, {_insight_comparison_name(leader)} led revenue with "
                f"{_format_plain_money(leader_value, payload)}. It was ahead of {_insight_comparison_name(runner)} by "
                f"{_format_plain_money(leader_value - runner_value, payload)}."
            )
        if any(phrase in text for phrase in ("far behind", "lagging", "lowest revenue", "least revenue", "underperforming", "worst", "behind")):
            laggard = min(comparison_rows, key=lambda item: _insight_comparison_value(item, *value_keys), default=runner)
            laggard_value = _insight_comparison_value(laggard, *value_keys)
            return (
                f"From the comparison{timeframe_suffix}, {_insight_comparison_name(laggard)} was farthest behind with "
                f"{_format_plain_money(laggard_value, payload)}, trailing {_insight_comparison_name(leader)} by "
                f"{_format_plain_money(leader_value - laggard_value, payload)}."
            )
        if any(phrase in text for phrase in ("difference", "gap", "spread", "compare", "comparison", "versus", "vs")):
            return (
                f"From the comparison{timeframe_suffix}, {_insight_comparison_name(leader)} led with "
                f"{_format_plain_money(leader_value, payload)} and {_insight_comparison_name(runner)} followed with "
                f"{_format_plain_money(runner_value, payload)}. The gap was {_format_plain_money(leader_value - runner_value, payload)}."
            )
        if any(phrase in text for phrase in ("trend", "change", "moving", "movement", "progress")):
            return (
                f"From the comparison{timeframe_suffix}, {_insight_comparison_name(leader)} and {_insight_comparison_name(runner)} show the clearest split in the saved result. "
                "Ask for revenue, units, gap, or laggard if you want the comparison narrowed further."
            )
        if any(phrase in text for phrase in ("unit", "quantity", "sold more", "volume")):
            ranked = sorted(
                comparison_rows,
                key=lambda item: _insight_comparison_value(item, "quantity_sold", "units_sold", "items_sold", "quantity", "units"),
                reverse=True,
            )
            leader, runner = ranked[0], ranked[1]
            leader_value = _insight_comparison_value(leader, "quantity_sold", "units_sold", "items_sold", "quantity", "units")
            runner_value = _insight_comparison_value(runner, "quantity_sold", "units_sold", "items_sold", "quantity", "units")
            return (
                f"From the comparison{timeframe_suffix}, {_insight_comparison_name(leader)} sold more units: "
                f"{_format_plain_number(leader_value)} units vs {_format_plain_number(runner_value)} for {_insight_comparison_name(runner)}."
            )

    if re.search(r"\b(staff|audit|activity|action|event|risk)\b", text):
        action_items = _insight_ranked_items(payload, "staff", "actions") or _insight_ranked_items(payload, "frequent", "actions")
        timeline_events = _insight_timeline_events(payload, "audit", "events")
        if any(phrase in text for phrase in ("most active staff", "active staff", "staff member", "which staff", "who was")):
            return (
                f"The saved staff activity response{timeframe_suffix} does not include a staff-member ranking. "
                "It includes action frequency and recent audit events. Ask for staff activity by user if you want the top staff member."
            )
        if action_items and any(phrase in text for phrase in ("activity type", "action type", "happened the most", "most frequent", "most common")):
            top = action_items[0]
            return (
                f"From the staff audit activity{timeframe_suffix}, the most frequent activity type was "
                f"{_insight_item_label(top)} with {_format_plain_number(top.get('value') or top.get('count'))} events."
            )
        if any(phrase in text for phrase in ("risk", "risks", "attention", "warning", "high")):
            risky = [
                event
                for event in timeline_events
                if re.search(r"\b(warning|high|critical|error)\b", str(event.get("severity") or "").lower())
            ]
            if risky:
                preview = "; ".join(str(event.get("title") or "Audit event") for event in risky[:3])
                return f"From the staff audit activity{timeframe_suffix}, {len(risky)} recent audit events need attention: {preview}."
            if timeline_events:
                return f"From the staff audit activity{timeframe_suffix}, no high-severity staff activity risk is visible in the recent audit events shown."

    if any(phrase in text for phrase in ("best day", "highest day", "strongest day", "peak day")):
        for insight in payload.get("insights") or []:
            if isinstance(insight, dict) and "best day" in str(insight.get("title") or "").lower():
                detail = str(insight.get("detail") or "").strip()
                if detail:
                    return f"From the last analysis{timeframe_suffix}: {detail}"
        trend = _insight_widget_by_title(payload, "revenue", "trend")
        rows = trend.get("data") if isinstance(trend, dict) else []
        if isinstance(rows, list) and rows:
            best = max((row for row in rows if isinstance(row, dict)), key=lambda row: float(row.get("sales") or row.get("value") or 0), default=None)
            if best:
                return f"From the last analysis{timeframe_suffix}, the strongest day was {best.get('label')} at {_format_plain_money(best.get('sales') or best.get('value'), payload)}."

    if any(
        phrase in text
        for phrase in (
            "which location",
            "top location",
            "best location",
            "leading location",
            "location led",
            "far behind",
            "lagging",
            "lowest revenue",
            "least revenue",
            "underperforming",
            "branch",
            "store",
            "warehouse",
            "outlet",
        )
    ):
        rows = _insight_location_rows(payload)
        if isinstance(rows, list) and rows:
            valid_rows = [row for row in rows if isinstance(row, dict)]
            value_keys = ("sales", "value", "total_sales", "revenue", "amount", "total_revenue")
            order_keys = ("orders", "count", "order_count", "orderCount", "transaction_count")
            best = max(valid_rows, key=lambda row: _insight_comparison_value(row, *value_keys), default=None)
            worst = min(valid_rows, key=lambda row: _insight_comparison_value(row, *value_keys), default=None)
            if best and worst:
                best_label = best.get("location") or best.get("label")
                best_sales = best.get("sales") or best.get("value") or best.get("total_sales") or best.get("revenue") or best.get("amount") or best.get("total_revenue")
                best_orders = best.get("orders") or best.get("count") or best.get("order_count") or best.get("orderCount") or best.get("transaction_count")
                worst_label = worst.get("location") or worst.get("label")
                worst_sales = worst.get("sales") or worst.get("value") or worst.get("total_sales") or worst.get("revenue") or worst.get("amount") or worst.get("total_revenue")
                total_sales = sum(_insight_comparison_value(row, *value_keys) for row in rows if isinstance(row, dict))
                order_leader = max(
                    valid_rows,
                    key=lambda row: _insight_comparison_value(row, *order_keys),
                    default=None,
                )
                share = ""
                try:
                    if total_sales > 0:
                        share = f", representing {(float(best_sales or 0) / total_sales) * 100:.1f}% of location revenue"
                except Exception:
                    share = ""
                also_led_orders = bool(order_leader and (order_leader.get("location") or order_leader.get("label")) == best_label)
                reason = ""
                if any(phrase in text for phrase in ("why", "reason", "because")):
                    reason = f" It led because it generated the highest revenue{share}{' and also had the highest order count' if also_led_orders else ''}."
                if any(phrase in text for phrase in ("far behind", "lagging", "lowest revenue", "least revenue", "underperforming")):
                    gap = float(best_sales or 0) - float(worst_sales or 0)
                    return (
                        f"From the last analysis{timeframe_suffix}, {worst_label} was farthest behind with "
                        f"{_format_plain_money(worst_sales, payload)}, trailing {best_label} by {_format_plain_money(gap, payload)}."
                    )
                return f"From the last analysis{timeframe_suffix}, {best_label} led with {_format_plain_money(best_sales, payload)} across {_format_plain_number(best_orders)} orders.{reason}"

    if any(phrase in text for phrase in ("top product", "top products", "which product", "what product", "products drove", "best seller")):
        ranked = _insight_widget_by_title(payload, "top", "product") or _insight_widget_by_title(payload, "top", "seller")
        items = ranked.get("items") if isinstance(ranked, dict) else []
        if not (isinstance(items, list) and items):
            items = _insight_comparison_rows(payload) or list(payload.get("products") or [])
        if isinstance(items, list) and items:
            ranked_items = [
                item for item in items if isinstance(item, dict)
            ]
            if ranked_items:
                ranked_items.sort(
                    key=lambda item: _insight_comparison_value(
                        item,
                        "sales_total",
                        "total_sales",
                        "total_revenue",
                        "gross_sales",
                        "revenue",
                        "sales",
                        "amount",
                        "value",
                    ),
                    reverse=True,
                )
            lines = []
            for index, item in enumerate(ranked_items[:5], start=1):
                label = str(
                    item.get("label")
                    or item.get("title")
                    or item.get("product_name")
                    or item.get("variant_name")
                    or item.get("name")
                    or "Product"
                ).strip()
                value = _format_plain_money(
                    item.get("value")
                    or item.get("count")
                    or item.get("sales_total")
                    or item.get("total_sales")
                    or item.get("revenue")
                    or item.get("amount"),
                    payload,
                )
                detail = str(item.get("detail") or item.get("variant_name") or item.get("barcode") or item.get("barcode_snapshot") or "").strip()
                lines.append(f"{index}. {label}: {value}{f' ({detail})' if detail else ''}")
            if lines:
                return "From the last analysis, the top products were:\n" + "\n".join(lines)

    if any(phrase in text for phrase in ("what should i do", "what do i do", "first action", "next action", "recommend", "priority")):
        ranked = _insight_widget_by_title(payload, "recommended", "actions") or _insight_widget_by_title(payload, "next", "actions")
        items = ranked.get("items") if isinstance(ranked, dict) else []
        if isinstance(items, list) and items:
            lines = []
            for index, item in enumerate([row for row in items if isinstance(row, dict)][:3], start=1):
                label = str(item.get("label") or item.get("title") or "Action").strip()
                detail = str(item.get("detail") or item.get("description") or "").strip()
                lines.append(f"{index}. {label}{f': {detail}' if detail else ''}")
            return "From the last analysis, prioritize:\n" + "\n".join(lines)

    if any(phrase in text for phrase in ("risk", "risks", "problem", "problems", "attention", "concern", "weak")):
        panel = _insight_widget_by_title(payload, "attention") or _insight_widget_by_title(payload, "risk")
        items = panel.get("items") if isinstance(panel, dict) else []
        if isinstance(items, list) and items:
            lines = []
            for index, item in enumerate([row for row in items if isinstance(row, dict)][:5], start=1):
                label = str(item.get("label") or item.get("title") or "Risk").strip()
                detail = str(item.get("description") or item.get("detail") or "").strip()
                lines.append(f"{index}. {label}{f': {detail}' if detail else ''}")
            return "From the last analysis, these need attention:\n" + "\n".join(lines)

    if any(phrase in text for phrase in ("total revenue", "how much", "total sales", "how many orders", "order count", "revenue")) and not any(
        phrase in text for phrase in ("which product", "which products", "top product", "top products", "products drove", "best seller", "top seller")
    ):
        revenue = _insight_metric_value(payload, "Revenue")
        orders = _insight_metric_value(payload, "Orders")
        if revenue is not None or orders is not None:
            parts = []
            if revenue is not None:
                parts.append(f"revenue was {_format_plain_money(revenue, payload)}")
            if orders is not None:
                parts.append(f"orders were {_format_plain_number(orders)}")
            return f"From the last analysis{timeframe_suffix}, " + " and ".join(parts) + "."

    if any(token in text for token in ("explain", "summary", "summarize", "what does this mean", "insight")):
        explanation = str(payload.get("explanation") or "").strip()
        insights = [
            str(item.get("detail") or "").strip()
            for item in payload.get("insights") or []
            if isinstance(item, dict) and str(item.get("detail") or "").strip()
        ]
        if explanation or insights:
            return "\n".join([part for part in [explanation, *insights[:3]] if part])

    return (
        f"I have the previous analysis{timeframe_suffix}, but that follow-up is not mapped yet. "
        "Ask about the leader, laggard, gap, trend, risk, or next action, and I will answer from the saved result."
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
    "onboarding": "Product Import",
    "product": "Product Management",
    "inventory": "Inventory Management",
    "pos": "Point of Sale (POS)",
    "users": "User and Workspace Management",
}


ROUTER_AGENT_NAMES: set[str] = {"product", "inventory", "pos"}


def _canonical_host_domain_agent(name: str) -> str:
    normalized = str(name or "").strip().lower().replace("_", "-")
    if not normalized:
        return ""
    if normalized == "host" or normalized.endswith("-host") or "-host-" in normalized:
        return "host"
    if normalized in HOST_AGENT_LABELS:
        return normalized
    if "onboarding" in normalized or normalized == "onboard":
        return "onboarding"
    if "inventory" in normalized:
        return "inventory"
    if "product" in normalized or "catalog" in normalized or "pricing" in normalized or "merchandising" in normalized:
        return "product"
    if normalized.startswith("pos") or "-pos" in normalized or "point-of-sale" in normalized:
        return "pos"
    if "user" in normalized or "workspace" in normalized:
        return "users"
    return normalized

SIMPLE_GREETING_QUERIES: set[str] = {
    "hello",
    "hello again",
    "hello are you there",
    "hello there",
    "hey",
    "hey are you there",
    "hey there",
    "hi",
    "hi are you there",
    "hi there",
    "good morning",
    "good afternoon",
    "good evening",
}

SIMPLE_STATUS_CHECK_QUERIES: set[str] = {
    "are you listening",
    "are you still there",
    "are you there",
    "can you hear me",
    "can you hear me now",
    "did you get my message",
    "is my message delivered",
    "is this working",
    "still there",
    "what is going on",
    "whats going on",
    "you there",
}

CONVERSATIONAL_SHORT_CIRCUIT_BLOCKERS: tuple[str, ...] = (
    "add",
    "analyse",
    "analysis",
    "analyze",
    "audit",
    "barcode",
    "business",
    "cashier",
    "compare",
    "create",
    "delete",
    "import",
    "inventory",
    "location",
    "order",
    "orders",
    "po",
    "pos",
    "product",
    "products",
    "purchase",
    "report",
    "revenue",
    "sales",
    "staff",
    "stock",
    "subscription",
    "supplier",
    "update",
    "variant",
    "variants",
)


def _is_simple_greeting_query(value: str) -> bool:
    text = _normalize_user_text(value)
    if text in SIMPLE_GREETING_QUERIES or text in SIMPLE_STATUS_CHECK_QUERIES:
        return True
    words = text.split()
    if len(words) > 8:
        return False
    if any(re.search(rf"\b{re.escape(term)}\b", text) for term in CONVERSATIONAL_SHORT_CIRCUIT_BLOCKERS):
        return False
    if words and words[0] in {"hello", "hey", "hi"}:
        return True
    return "are you there" in text or "can you hear me" in text


def _agent_intro_text(agent_name: str | None) -> str:
    normalized = str(agent_name or "").strip().lower()
    if _canonical_host_domain_agent(normalized) == "host":
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
            "The user selected Product Import from the Product Management menu. "
            "Help them discover curated products, select items, and import them into the workspace. "
            "Start with a short structured choice or the next required import step. "
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
        "product import",
        "import products",
        "catalog import",
        "global catalog",
        "curated catalog",
        "product onboarding",
        "onboarding",
        "onboard",
        "select all products",
        "barcode selection",
        "brand selection",
        "category selection",
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
        "inventory category",
        "inventory categories",
        "categorize inventory",
        "categorise inventory",
        "categorize inventories",
        "categorise inventories",
        "uncategorized",
        "uncategorised",
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
        "user group",
        "user groups",
        "staff group",
        "staff groups",
        "permission group",
        "permission groups",
        "workspace group",
        "workspace groups",
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
    if _is_domain_action_request(text):
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
        "available to you",
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
    if _is_domain_action_request(text):
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


def _is_domain_action_request(value: str) -> bool:
    text = _normalize_user_text(value)
    if not text:
        return False
    action_terms = (
        "add",
        "assign",
        "attach",
        "categorise",
        "categorize",
        "category",
        "categories",
        "check my",
        "classify",
        "create",
        "link",
        "look at",
        "map",
        "move",
        "set",
        "update",
    )
    domain_terms = (
        "inventory",
        "inventories",
        "item",
        "items",
        "order",
        "orders",
        "pos",
        "product",
        "products",
        "stock",
    )
    return any(term in text for term in action_terms) and any(term in text for term in domain_terms)


def _should_offer_host_unavailable_domain_picker(value: str) -> bool:
    text = _normalize_user_text(value)
    if not text:
        return False
    if "multiple_choice_response" in text or '"selected"' in text or "'selected'" in text:
        return True
    if _is_host_introspection_query(text) or _is_host_capability_picker_query(text) or _is_host_availability_query(text):
        return True
    if _is_domain_action_request(text):
        return True
    generic_help_phrases = (
        "help me with",
        "i need help with",
        "i want help with",
        "which area",
        "what area",
        "pick an area",
        "choose an area",
    )
    return any(phrase in text for phrase in generic_help_phrases)


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
            "Start a guided product import flow. Ask the user which product categories or brands they want to import first, "
            "then browse global catalog products in pages, keep already-imported products filtered out, and collect selection step by step using structured interactions."
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
    inferred_agent = _strong_domain_agent_override(query)
    available_names = _available_agent_names(agent_summaries)
    if inferred_agent and inferred_agent != "onboarding" and (not available_names or inferred_agent in available_names):
        return [inferred_agent]
    candidates: list[tuple[str, int]] = []
    for agent_name, keywords in HOST_DOMAIN_KEYWORDS.items():
        if _canonical_host_domain_agent(agent_name) == "host":
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


_DELEGATED_CONFIRMATION_TEXT_PATTERNS = (
    re.compile(r"\bonce you confirm\b", flags=re.IGNORECASE),
    re.compile(r"\bplease confirm\b", flags=re.IGNORECASE),
    re.compile(r"\breview and confirm\b", flags=re.IGNORECASE),
    re.compile(r"\bconfirm and (?:i[' ]?ll|i will)\b", flags=re.IGNORECASE),
    re.compile(r"\bi need (?:your )?(?:confirmation|approval)\b", flags=re.IGNORECASE),
    re.compile(r"\blet me know if you(?:'d| would) like me to proceed\b", flags=re.IGNORECASE),
    re.compile(r"\bwhat i need from you\b", flags=re.IGNORECASE),
    re.compile(r"\b(?:approve|approval)\b.*\b(?:proceed|execute|apply|create|assign|update)\b", flags=re.IGNORECASE),
)


def _plain_text_delegated_response_requires_confirmation(text: str) -> bool:
    candidate = str(text or "").strip()
    if not candidate:
        return False
    return any(pattern.search(candidate) for pattern in _DELEGATED_CONFIRMATION_TEXT_PATTERNS)


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
    "full_setup": "Product Import",
    "stock_locations": "Catalog Discovery",
    "inventory_categories": "Category Matching",
    "inventory_setup": "Import Review",
    "product_onboarding": "Product Import",
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
    workflow = str(payload.get("workflow") or "").strip().lower()
    return (
        workflow in {"inventory_onboarding", "product_import"}
        and str(payload.get("workflow_stage") or "").strip().lower() == stage
    )


def _onboarding_scope_picker_arguments(
    *,
    description: str = "Choose the product import flow you want to start. I will guide you step by step.",
) -> dict[str, Any]:
    return {
        "title": "Start Product Import",
        "description": description,
        "options": [
            {"value": "product_onboarding", "label": "Product Import"},
        ],
        "multiple": False,
        "allow_input": True,
    }


def _select_options(options: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"value": value, "label": label} for value, label in options]


def _onboarding_wizard_steps(scope: str) -> list[dict[str, Any]]:
    if scope == "product_onboarding":
        return [
            {
                "id": "filters",
                "title": "Choose Catalog Filters",
                "description": "First choose whether you want to browse by category, brand, or both. Then list the categories and brands you want.",
                "fields": [
                    {
                        "name": "catalog_scope",
                        "type": "select",
                        "label": "Browse By",
                        "required": True,
                        "options": _select_options(
                            [
                                ("category", "Product Category"),
                                ("brand", "Brand"),
                                ("both", "Both Category and Brand"),
                            ]
                        ),
                        "placeholder": "Choose how you want to filter products",
                    },
                    {
                        "name": "selected_category_names",
                        "type": "textarea",
                        "label": "Selected Categories",
                        "required": False,
                        "placeholder": "Beverages\nSnacks\nHousehold Care",
                    },
                    {
                        "name": "selected_brand_names",
                        "type": "textarea",
                        "label": "Selected Brands",
                        "required": False,
                        "placeholder": "Nivea\nCoca-Cola\nCadbury",
                    },
                    {
                        "name": "browse_all_categories",
                        "type": "boolean",
                        "label": "Browse all global categories",
                        "required": False,
                    },
                    {
                        "name": "browse_all_brands",
                        "type": "boolean",
                        "label": "Browse all global brands",
                        "required": False,
                    },
                ],
            },
            {
                "id": "products",
                "title": "Review Catalog Page",
                "description": "I will browse products 30 at a time. Pick the ones you want from this page and choose whether to import now or continue.",
                "fields": [
                    {
                        "name": "current_page",
                        "type": "text",
                        "label": "Current Page",
                        "required": False,
                        "placeholder": "1",
                    },
                    {
                        "name": "page_size",
                        "type": "select",
                        "label": "Products Per Page",
                        "required": False,
                        "options": _select_options([("30", "30 products")]),
                        "placeholder": "30",
                    },
                    {
                        "name": "selected_product_barcodes",
                        "type": "textarea",
                        "label": "Selected Product Barcodes",
                        "required": False,
                        "placeholder": "8800000002501\n8800000002502",
                    },
                    {
                        "name": "page_action",
                        "type": "select",
                        "label": "Page Action",
                        "required": True,
                        "options": _select_options(
                            [
                                ("import_current_page", "Import Current Page"),
                                ("select_more", "Select More"),
                                ("end", "End For Now"),
                            ]
                        ),
                        "placeholder": "Choose what to do next",
                    },
                ],
            },
            {
                "id": "review",
                "title": "Confirm Import",
                "description": "Review the selected products and confirm that I should import them into your workspace.",
                "fields": [
                    {
                        "name": "confirm_import",
                        "type": "boolean",
                        "label": "Confirm Import",
                        "required": True,
                    },
                    {
                        "name": "send_in_app_notification",
                        "type": "boolean",
                        "label": "Notify me in app when import processing completes",
                        "required": False,
                    },
                ],
            },
        ]

    # Any non-product import setup request now falls back to the same product-import workflow.
    return [
        {
            "id": "categories",
            "title": "Choose Product Categories",
            "description": "Start by selecting the product categories you want to browse from the global catalog.",
            "fields": [
                {
                    "name": "selected_category_names",
                    "type": "textarea",
                    "label": "Selected Categories",
                    "required": True,
                    "placeholder": "Beverages\nSnacks\nHousehold Care",
                },
                {
                    "name": "browse_all_categories",
                    "type": "boolean",
                    "label": "Browse all global categories",
                    "required": False,
                },
            ],
        },
        {
            "id": "products",
            "title": "Review Catalog Page",
            "description": "I will browse products 30 at a time. Pick the ones you want from this page and choose whether to import now or continue.",
            "fields": [
                {
                    "name": "current_page",
                    "type": "text",
                    "label": "Current Page",
                    "required": False,
                    "placeholder": "1",
                },
                {
                    "name": "page_size",
                    "type": "select",
                    "label": "Products Per Page",
                    "required": False,
                    "options": _select_options([("30", "30 products")]),
                    "placeholder": "30",
                },
                {
                    "name": "selected_product_barcodes",
                    "type": "textarea",
                    "label": "Selected Product Barcodes",
                    "required": False,
                    "placeholder": "8800000002501\n8800000002502",
                },
                {
                    "name": "page_action",
                    "type": "select",
                    "label": "Page Action",
                    "required": True,
                    "options": _select_options(
                        [
                            ("import_current_page", "Import Current Page"),
                            ("select_more", "Select More"),
                            ("end", "End For Now"),
                        ]
                    ),
                    "placeholder": "Choose what to do next",
                },
            ],
        },
        {
            "id": "review",
            "title": "Confirm Import",
            "description": "Review the selected products and confirm that I should import them into your workspace.",
            "fields": [
                {
                    "name": "confirm_import",
                    "type": "boolean",
                    "label": "Confirm Import",
                    "required": True,
                },
                {
                    "name": "send_in_app_notification",
                    "type": "boolean",
                    "label": "Notify me in app when import processing completes",
                    "required": False,
                },
            ],
        },
    ]


def _onboarding_wizard_arguments(scope: str) -> dict[str, Any]:
    label = ONBOARDING_SCOPE_LABELS.get(scope, "Product Import")
    return {
        "title": f"{label} Wizard",
        "description": "Fill in the import details and I will prepare the product import plan.",
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
    has_product = "product" in normalized or "sku" in normalized
    has_import_intent = any(token in normalized for token in ("import", "catalog", "barcode", "variant", "variants", "select all", "global products"))
    mentions_product_import = "product import" in normalized or "catalog import" in normalized
    mentions_onboarding = "onboarding" in normalized or "onboard" in normalized
    if has_import_intent or has_product or mentions_product_import or mentions_onboarding:
        return "product_onboarding"
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

    brand_names = _extract_named_text_list(
        stripped,
        (
            r"(?:^|\n)\s*(?:product\s+)?brands?\s*[:=-]\s*(?P<value>[^\n]+(?:\n(?!\s*[A-Za-z][A-Za-z /'-]{0,40}\s*[:=-]).+)*)",
            r"\bbrands?\s+(?:are|like|such as)\s+(?P<value>[^.]+)",
        ),
    )
    if brand_names:
        parsed["brand_names"] = "\n".join(_dedupe_preserving_order(brand_names))

    if "both" in normalized and ("brand" in normalized or "category" in normalized):
        parsed["catalog_scope"] = "both"
    elif "brand" in normalized and "category" not in normalized:
        parsed["catalog_scope"] = "brand"
    elif "category" in normalized and "brand" not in normalized:
        parsed["catalog_scope"] = "category"

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
            if key in {"initial_product_names", "product_category_name", "brand_names", "catalog_scope", "pos_ready"}
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
    elif field_name == "catalog_scope":
        desired_text = str(prefill_data.get("catalog_scope") or "").strip()
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


def _normalize_onboarding_existing_responses(
    scope: str,
    *,
    wizard_payload: dict[str, Any],
    existing_responses: dict[str, Any],
) -> dict[str, Any]:
    steps = wizard_payload.get("steps")
    if not isinstance(steps, list):
        steps = _onboarding_wizard_steps(scope)

    normalized: dict[str, Any] = {}
    for index, step in enumerate(steps):
        raw_step = existing_responses.get(f"step_{index}")
        if not isinstance(raw_step, dict):
            continue
        step_response = dict(raw_step)
        field_names = {
            str(field.get("name") or "").strip()
            for field in step.get("fields", [])
            if isinstance(field, dict) and str(field.get("name") or "").strip()
        }

        if "primary_location_mode" in field_names:
            mode = str(step_response.get("primary_location_mode") or "").strip().lower()
            has_existing_primary = bool(str(step_response.get("primary_location_id") or "").strip())
            has_new_primary = bool(
                str(step_response.get("primary_location_name") or "").strip()
                or str(step_response.get("primary_location_type") or "").strip()
            )
            if not mode:
                if has_existing_primary:
                    step_response["primary_location_mode"] = "existing"
                elif has_new_primary:
                    step_response["primary_location_mode"] = "new"
            elif mode == "existing" and not has_existing_primary and has_new_primary:
                step_response["primary_location_mode"] = "new"

        if "continue_to_product_onboarding" in field_names and "continue_to_product_onboarding" not in step_response:
            has_product_follow_up = bool(
                str(step_response.get("initial_product_names") or "").strip()
                or str(step_response.get("product_category_id") or "").strip()
                or step_response.get("pos_ready") is True
            )
            if has_product_follow_up:
                step_response["continue_to_product_onboarding"] = True

        if step_response:
            normalized[f"step_{index}"] = step_response

    return normalized


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

    catalog_scope = str(flat.get("catalog_scope") or "").strip()
    if catalog_scope:
        lines.append(f"Browse by: {catalog_scope}")

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

    brand_names = _split_multiline_values(flat.get("brand_names"))
    if brand_names:
        lines.append("Brands: " + ", ".join(brand_names))

    product_category = (
        str(flat.get("product_category_label") or "").strip()
        or str(flat.get("product_category") or "").strip()
    )
    if product_category:
        lines.append(f"Product category: {product_category}")

    if isinstance(flat.get("pos_ready"), bool):
        lines.append(f"POS ready: {'Yes' if flat['pos_ready'] else 'No'}")

    if isinstance(flat.get("continue_to_product_onboarding"), bool):
        lines.append("Continue to product import: " + ("Yes" if flat["continue_to_product_onboarding"] else "No"))

    return "\n".join(lines)


def _onboarding_review_picker_arguments(summary: str) -> dict[str, Any]:
    return {
        "title": "Review Product Import Plan",
        "description": summary + "\n\nChoose what you want me to do next.",
        "options": [
            {"value": "create_now", "label": "Import These Products"},
            {"value": "cancel_onboarding", "label": "Cancel For Now"},
        ],
        "multiple": False,
        "allow_input": True,
    }


def _onboarding_target_agent(scope: str) -> str:
    return "product" if scope == "product_onboarding" else "product"


def _onboarding_creation_request(scope: str, data: dict[str, Any]) -> str:
    serialized = json.dumps(data, ensure_ascii=False)
    if scope == "product_onboarding":
        return (
            "Import the selected global catalog products using the available product import tools if possible. "
            "Use category-first discovery, allow brand selection, keep the catalog paginated, and filter out products that are already in this workspace. "
            "Perform the requested product import work rather than only describing it. "
            "If any required detail is missing, ask one concise follow-up question.\n"
            f"Collected onboarding data JSON:\n{serialized}"
        )
    return (
        "Import the requested product selection using the available product import tools if possible. "
        "Do not create inventory items or inventory categories as part of this flow. If any required detail is missing, ask one concise follow-up question.\n"
        f"Collected onboarding data JSON:\n{serialized}"
    )


def _extract_onboarding_creation_payload_from_text(text: str) -> tuple[str, dict[str, Any]] | None:
    raw = str(text or "")
    marker = "Collected onboarding data JSON:"
    if marker not in raw:
        return None
    payload = _extract_json_object_from_text(raw.split(marker, 1)[1].strip())
    if not isinstance(payload, dict):
        return None
    scope = str(payload.get("scope") or "").strip() or "full_setup"
    flat = payload.get("flat")
    if not isinstance(flat, dict):
        return None
    return scope, payload


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
    read_only_prefixes = (
        "list ",
        "show ",
        "search ",
        "find ",
        "what ",
        "which ",
        "get ",
        "display ",
        "summarize ",
        "summarise ",
    )
    if normalized.startswith(read_only_prefixes):
        return None
    if any(token in normalized for token in ("parent of", "child of", "as parent of", "assign as parent", "set parent of")):
        return "update_stock_location_parent"
    explicit_stock_location_creation = (
        "create stock location" in normalized
        or "create a stock location" in normalized
        or "create new stock location" in normalized
        or "add stock location" in normalized
        or "add a stock location" in normalized
        or "new stock location" in normalized
        or "set up stock location" in normalized
        or "setup stock location" in normalized
        or "create location called" in normalized
        or "create a location called" in normalized
        or "add location called" in normalized
        or "new location called" in normalized
    )
    if explicit_stock_location_creation:
        return "create_stock_location"
    explicit_inventory_creation = any(token in normalized for token in ("create", "add", "new"))
    explicit_inventory_target = any(
        token in normalized
        for token in ("inventory item", "inventory ledger", "stock ledger", "inventory called", "inventory named")
    )
    if "inventory" in normalized and explicit_inventory_creation and explicit_inventory_target:
        return "create_inventory_item"
    return None


def _inventory_setup_lookup_from_text(text: str) -> tuple[str, dict[str, Any]] | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    read_only_prefixes = (
        "list ",
        "show ",
        "search ",
        "find ",
        "what ",
        "which ",
        "get ",
        "display ",
        "summarize ",
        "summarise ",
        "inspect ",
        "check ",
    )
    if not normalized.startswith(read_only_prefixes):
        return None
    if any(token in normalized for token in ("inventory category", "inventory categories", "category tree")):
        return "inventory.list_inventory_categories", {}
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
        inventory_name = _extract_first_named_value(
            text,
            (
                r"(?:^|\n)\s*(?:inventory|inventory\s+item)\s+name\s*[:=-]\s*(?P<value>[^\n]+)",
                r"\bcreate\s+(?:an?\s+)?inventory\s+item\s+(?:called|named)\s+(?P<value>.+?)(?:\s+in\s+category\b|\s+at\b|\s+with\s+description\b|[.,]|$)",
                r"\bnew\s+inventory\s+item\s+(?:called|named)\s+(?P<value>.+?)(?:\s+in\s+category\b|\s+at\b|\s+with\s+description\b|[.,]|$)",
            ),
        )
        if inventory_name:
            prefill["default_inventory_name"] = inventory_name
        direct_category = _extract_first_named_value(
            text,
            (
                r"(?:^|\n)\s*(?:inventory\s+)?category\s*[:=-]\s*(?P<value>[^\n]+)",
                r"\bcategory\s+(?:is|should be|as)\s+(?P<value>[^.]+)",
                r"\bin\s+category\s+(?P<value>.+?)(?:\s+at\b|\s+with\b|[.,]|$)",
            ),
        )
        if direct_category:
            prefill["inventory_category_name"] = direct_category
        related_location = _extract_first_named_value(
            text,
            (
                r"(?:^|\n)\s*(?:inventory|default|ledger)\s+location\s*[:=-]\s*(?P<value>[^\n]+)",
                r"\bat\s+(?P<value>[^,\n.]+)",
            ),
        )
        if related_location:
            prefill["related_stock_location_name"] = related_location
        direct_description = _extract_first_named_value(
            text,
            (
                r"(?:^|\n)\s*(?:inventory\s+)?description\s*[:=-]\s*(?P<value>[^\n]+)",
                r"\bwith\s+description\s+(?P<value>[^.]+)",
                r"\bdescribed\s+as\s+(?P<value>[^.]+)",
            ),
        )
        if direct_description:
            prefill["inventory_description"] = direct_description
        return prefill

    if action == "create_stock_location":
        name = _extract_first_named_value(
            text,
            (
                r"(?:^|\n)\s*(?:stock\s+)?location\s+name\s*[:=-]\s*(?P<value>[^\n]+)",
                r"\b(?:create|add|new)\s+(?:a\s+)?(?:stock\s+)?location\s+(?:called|named)\s+(?P<value>.+?)(?:\s+with\s+location\s+type\b|\s+under\b|[.,]|$)",
            ),
        )
        if name:
            prefill["location_name"] = name
        location_type = _extract_first_named_value(
            text,
            (
                r"(?:^|\n)\s*location\s+type\s*[:=-]\s*(?P<value>[^\n]+)",
                r"\btype\s+(?:is|should be|as)\s+(?P<value>[^,\n.]+)",
                r"\bwith\s+location\s+type\s+(?P<value>.+?)(?:\s+under\b|[.,]|$)",
                r"\blocation\s+type\s+(?P<value>.+?)(?:\s+under\b|[.,]|$)",
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
    preferred_query: str | None = None,
) -> list[dict[str, Any]]:
    relation_spec = next(
        (item for item in _relation_lookup_specs(tool_specs) if item.get("lookup_tool") == lookup_tool),
        None,
    )
    if relation_spec is None:
        return []
    desired = str(preferred_query or "").strip()
    if desired and ".search_" in lookup_tool:
        try:
            output = await tool_executor.call_tool(
                name=lookup_tool,
                arguments={"query": desired, "limit": 10},
                ctx=tool_ctx,
            )
        except Exception:
            pass
        else:
            targeted_options: list[dict[str, Any]] = []
            for item in _relation_items_from_lookup_output(lookup_tool, output):
                if not isinstance(item, dict):
                    continue
                option = _relation_option_from_item(lookup_tool, item)
                if option is not None:
                    targeted_options.append(option)
            if targeted_options:
                return targeted_options
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
    exact_matches: list[dict[str, Any]] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        label = re.sub(r"\s+", " ", str(option.get("label") or "").strip().lower())
        value = re.sub(r"\s+", " ", str(option.get("value") or "").strip().lower())
        if desired == label or desired == value:
            exact_matches.append(option)
            continue
        if desired in label:
            return option.get("value")
    if exact_matches:
        def _quantity_score(option: dict[str, Any]) -> float:
            description = str(option.get("description") or "")
            match = re.search(r"(?:available|qty|quantity)\s*:\s*(-?\d+(?:\.\d+)?)", description, flags=re.IGNORECASE)
            if not match:
                return float("-inf")
            try:
                return float(match.group(1))
            except ValueError:
                return float("-inf")
        best_match = max(exact_matches, key=_quantity_score)
        return best_match.get("value")
    return None


async def _ensure_lookup_option_for_name(
    lookup_tool: str,
    *,
    desired_name: str | None,
    options: list[dict[str, Any]],
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
) -> tuple[list[dict[str, Any]], Any]:
    matched_value = _inventory_setup_prefill_option_value(options, desired_name)
    if matched_value not in (None, "", [], {}):
        return options, matched_value
    desired = str(desired_name or "").strip()
    if not desired:
        return options, None
    try:
        output = await tool_executor.call_tool(
            name=lookup_tool,
            arguments={"query": desired, "limit": 10},
            ctx=tool_ctx,
        )
    except Exception:
        return options, None
    items = _relation_items_from_lookup_output(lookup_tool, output)
    merged = list(options)
    seen_values = {str(option.get("value") or "").strip() for option in merged if isinstance(option, dict)}
    for item in items:
        if not isinstance(item, dict):
            continue
        option = _relation_option_from_item(lookup_tool, item)
        if not isinstance(option, dict):
            continue
        option_value = str(option.get("value") or "").strip()
        if not option_value or option_value in seen_values:
            continue
        merged.append(option)
        seen_values.add(option_value)
    return merged, _inventory_setup_prefill_option_value(merged, desired)


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
            preferred_query=prefill.get("parent_location_name"),
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
            "location_type_name": _inventory_setup_prefill_option_value(
                location_type_options,
                str(prefill.get("location_type_name") or "").strip() or None,
            ),
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
    if any(token in normalized for token in ("reservation", "reserve stock", "reserve inventory", "hold stock")):
        return "create_stock_reservation"
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
        "create_stock_reservation": "Create Stock Reservation",
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
            r"\bupdate\s+the\s+product\s+(?P<value>.+?)(?:\s+description\s+to\b|\s+base\s+price\b|\s+price\b|\s+category\b|[.,]|$)",
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
            r"\bdescription\s+to\s+(?P<value>[^.\n]+)",
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
            r"\b(?:stock\s+reservation\s+for|reserve(?:\s+stock)?\s+for)\s+\d+(?:\.\d+)?\s+units?\s+of\s+(?P<value>.+?)\s+at\b",
            r"\b\d+(?:\.\d+)?\s+units?\s+of\s+(?P<value>.+?)\s+at\b",
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
    if action == "create_stock_reservation":
        reservation_location = _extract_first_named_value(
            text,
            (
                r"\bat\s+(?P<value>.+?)(?:\s+for\b|[.,]|$)",
            ),
        )
        if reservation_location:
            prefill["from_location_name"] = reservation_location
    quantity = _extract_decimal_text(text)
    if quantity:
        prefill["quantity"] = quantity
    order_reference = _extract_first_named_value(
        text,
        (
            r"(?:^|\n)\s*(?:reference|order\s+reference)\s*[:=-]\s*(?P<value>[^\n]+)",
        ),
    )
    if order_reference:
        prefill["external_order_id"] = order_reference
    normalized = _normalize_user_text(text)
    if action == "adjust_inventory_item_stock":
        if any(token in normalized for token in ("remove", "decrease", "reduce", "deduct")):
            prefill["adjustment_type"] = "remove"
        elif any(token in normalized for token in ("add", "increase", "restock")):
            prefill["adjustment_type"] = "add"
    if action == "create_stock_reservation":
        prefill["external_order_type"] = "sales_order"
        if "showroom transfer" in normalized:
            prefill["external_order_id"] = "showroom-transfer"
            prefill["notes"] = "Showroom transfer reservation"
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
        product_options, matched_product_id = await _ensure_lookup_option_for_name(
            "product.search_products",
            desired_name=prefill.get("name"),
            options=product_options,
            tool_executor=tool_executor,
            tool_ctx=tool_ctx,
        )
        fields = [
            {"name": "product_id", "type": "select", "label": "Product to Update", "required": True, "options": product_options, "placeholder": "Select a product"},
            *common_fields,
        ]
        current_values["product_id"] = matched_product_id
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
    if prefill.get("from_location_name") and "inventory.search_stock_locations" in tool_names:
        stock_location_lookup = "inventory.search_stock_locations"
    if prefill.get("to_location_name") and "inventory.search_stock_locations" in tool_names:
        stock_location_lookup = "inventory.search_stock_locations"
    if prefill.get("inventory_item_name") and "inventory.search_inventory_items" in tool_names:
        inventory_item_lookup = "inventory.search_inventory_items"
    location_options = await _load_lookup_options_by_tool_name(
        stock_location_lookup,
        tool_specs=tool_specs,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
        preferred_query=prefill.get("from_location_name") or prefill.get("to_location_name"),
    )
    inventory_item_options = await _load_lookup_options_by_tool_name(
        inventory_item_lookup,
        tool_specs=tool_specs,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
        preferred_query=prefill.get("inventory_item_name"),
    )
    inventory_item_options, matched_inventory_item_id = await _ensure_lookup_option_for_name(
        inventory_item_lookup,
        desired_name=prefill.get("inventory_item_name"),
        options=inventory_item_options,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
    )
    location_options, matched_from_location_id = await _ensure_lookup_option_for_name(
        stock_location_lookup,
        desired_name=prefill.get("from_location_name"),
        options=location_options,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
    )
    location_options, matched_to_location_id = await _ensure_lookup_option_for_name(
        stock_location_lookup,
        desired_name=prefill.get("to_location_name"),
        options=location_options,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
    )
    if action == "create_stock_reservation":
        fields = [
            {"name": "inventory_item_id", "type": "select", "label": "Inventory Item", "required": True, "options": inventory_item_options, "placeholder": "Select an inventory item"},
            {"name": "stock_location_id", "type": "select", "label": "Stock Location", "required": True, "options": location_options, "placeholder": "Select a stock location"},
            {"name": "quantity", "type": "text", "label": "Reserved Quantity", "required": True, "placeholder": "2"},
            {"name": "external_order_type", "type": "text", "label": "Reference Type", "required": True, "placeholder": "sales_order"},
            {"name": "external_order_id", "type": "text", "label": "Reference ID", "required": True, "placeholder": "showroom-transfer"},
            {"name": "external_order_line_id", "type": "text", "label": "Reference Line ID", "required": False, "placeholder": "Optional line reference"},
            {"name": "notes", "type": "textarea", "label": "Notes", "required": False, "placeholder": "Optional reservation notes"},
        ]
        current_values = {
            "inventory_item_id": matched_inventory_item_id,
            "stock_location_id": matched_from_location_id,
            "quantity": prefill.get("quantity"),
            "external_order_type": prefill.get("external_order_type"),
            "external_order_id": prefill.get("external_order_id"),
            "notes": prefill.get("notes"),
        }
        description = "Confirm the stock reservation details before I reserve anything."
    elif action == "transfer_location_stock":
        fields = [
            {"name": "inventory_item_id", "type": "select", "label": "Inventory Item", "required": True, "options": inventory_item_options, "placeholder": "Select an inventory item"},
            {"name": "from_location_id", "type": "select", "label": "Source Location", "required": True, "options": location_options, "placeholder": "Select the source location"},
            {"name": "to_location_id", "type": "select", "label": "Destination Location", "required": True, "options": location_options, "placeholder": "Select the destination location"},
            {"name": "quantity", "type": "text", "label": "Quantity", "required": True, "placeholder": "10"},
            {"name": "reason", "type": "text", "label": "Reason", "required": False, "placeholder": "Rebalancing stock"},
            {"name": "notes", "type": "textarea", "label": "Notes", "required": False, "placeholder": "Optional transfer notes"},
        ]
        current_values = {
            "inventory_item_id": matched_inventory_item_id,
            "from_location_id": matched_from_location_id,
            "to_location_id": matched_to_location_id,
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
            "inventory_item_id": matched_inventory_item_id,
            "stock_location_id": matched_from_location_id,
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
    interaction_payload: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], list[str]]:
    if action == "create_stock_reservation":
        spec = _tool_spec_by_name(tool_specs, "inventory.create_stock_reservation")
        payload_spec = _nested_object_tool_spec(spec, "payload")
        arguments: dict[str, Any] = {}
        payload_arguments: dict[str, Any] = {}
        stock_location_id = str(form_data.get("stock_location_id") or "").strip() or None
        structural_location_id = _selected_structural_location_id_from_form(
            interaction_payload,
            field_name="stock_location_id",
            selected_value=stock_location_id,
        )
        _set_schema_arg(payload_arguments, payload_spec, ["inventory_item_id", "inventoryItemId"], str(form_data.get("inventory_item_id") or "").strip() or None)
        _set_schema_arg(payload_arguments, payload_spec, ["stock_location_id", "stockLocationId"], stock_location_id)
        _set_schema_arg(payload_arguments, payload_spec, ["structural_location_id", "structuralLocationId"], structural_location_id)
        _set_schema_arg(payload_arguments, payload_spec, ["reserved_quantity", "reservedQuantity"], str(form_data.get("quantity") or "").strip() or None)
        _set_schema_arg(payload_arguments, payload_spec, ["external_order_type", "externalOrderType"], str(form_data.get("external_order_type") or "").strip() or None)
        _set_schema_arg(payload_arguments, payload_spec, ["external_order_id", "externalOrderId"], str(form_data.get("external_order_id") or "").strip() or None)
        _set_schema_arg(payload_arguments, payload_spec, ["external_order_line_id", "externalOrderLineId"], str(form_data.get("external_order_line_id") or "").strip() or None)
        _set_schema_arg(payload_arguments, payload_spec, ["notes"], str(form_data.get("notes") or "").strip() or None)
        if payload_arguments:
            arguments["payload"] = payload_arguments
        filtered = _filtered_tool_arguments(spec, arguments)
        return "inventory.create_stock_reservation", filtered, _missing_required_arguments(spec, filtered)

    if action == "transfer_location_stock":
        spec = _tool_spec_by_name(tool_specs, "inventory.transfer_location_stock")
        payload_spec = _nested_object_tool_spec(spec, "payload")
        arguments: dict[str, Any] = {}
        from_location_id = str(form_data.get("from_location_id") or "").strip() or None
        from_structural_location_id = _selected_structural_location_id_from_form(
            interaction_payload,
            field_name="from_location_id",
            selected_value=from_location_id,
        )
        _set_schema_arg(arguments, spec, ["location_id", "locationId"], from_location_id)
        payload_arguments: dict[str, Any] = {}
        transfer_line = {
            "inventory_item_id": str(form_data.get("inventory_item_id") or "").strip() or None,
            "from_location_id": from_location_id,
            "to_location_id": str(form_data.get("to_location_id") or "").strip() or None,
            "quantity": str(form_data.get("quantity") or "").strip() or None,
            "notes": str(form_data.get("notes") or "").strip() or None,
        }
        if from_structural_location_id:
            transfer_line["structural_location_id"] = from_structural_location_id
        _set_schema_arg(payload_arguments, payload_spec, ["transfers"], [transfer_line])
        _set_schema_arg(payload_arguments, payload_spec, ["structural_location_id", "structuralLocationId"], from_structural_location_id)
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
        stock_location_id = str(form_data.get("stock_location_id") or "").strip() or None
        structural_location_id = _selected_structural_location_id_from_form(
            interaction_payload,
            field_name="stock_location_id",
            selected_value=stock_location_id,
        )
        adjustment_line = {
            "inventory_item_id": str(form_data.get("inventory_item_id") or "").strip() or None,
            "stock_location_id": stock_location_id,
            "quantity": str(form_data.get("quantity") or "").strip() or None,
            "adjustment_type": str(form_data.get("adjustment_type") or "").strip() or None,
            "notes": str(form_data.get("notes") or "").strip() or None,
        }
        if structural_location_id:
            adjustment_line["structural_location_id"] = structural_location_id
        _set_schema_arg(payload_arguments, payload_spec, ["adjustments"], [adjustment_line])
        _set_schema_arg(payload_arguments, payload_spec, ["structural_location_id", "structuralLocationId"], structural_location_id)
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
    read_only_prefixes = (
        "list ",
        "show ",
        "search ",
        "find ",
        "what ",
        "which ",
        "get ",
        "display ",
        "summarize ",
        "summarise ",
        "inspect ",
        "check ",
    )
    if normalized.startswith(read_only_prefixes):
        return None
    if "pricing strategy" in normalized or "margin strategy" in normalized or "price strategy" in normalized:
        return "create_pricing_strategy"
    if "pricing rule" in normalized or "discount rule" in normalized or "promo rule" in normalized:
        return "create_pricing_rule"
    return None


def _product_pricing_lookup_from_text(text: str) -> tuple[str, dict[str, Any]] | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    read_only_prefixes = (
        "list ",
        "show ",
        "search ",
        "find ",
        "what ",
        "which ",
        "get ",
        "display ",
        "summarize ",
        "summarise ",
        "inspect ",
        "check ",
    )
    if not normalized.startswith(read_only_prefixes):
        return None
    if "pricing rule" in normalized or "pricing rules" in normalized or "discount rule" in normalized:
        prefill = _parse_product_pricing_prefill_from_text("create_pricing_rule", text)
        arguments: dict[str, Any] = {"active_only": False, "limit": 20}
        product_name = str(prefill.get("product_name") or "").strip()
        if product_name:
            arguments["product_name"] = product_name
        return "product.get_product_pricing_rules", arguments
    return None


def _default_pricing_rule_window() -> tuple[str, str]:
    start_at = datetime.now(timezone.utc)
    end_at = start_at + timedelta(days=30)
    return start_at.isoformat(), end_at.isoformat()


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
            r"\bcreate\s+(?:a\s+)?pricing\s+strategy\s+for\s+.+?\s+(?:called|named)\s+(?P<value>[^,\n.]+)",
            r"\bcreate\s+(?:a\s+)?pricing\s+rule\s+for\s+.+?\s+(?:called|named)\s+(?P<value>[^,\n.]+)",
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
            r"\bfor\s+(?P<value>.+?)(?:\s+called\b|\s+named\b|[.,]|$)",
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
        preferred_query=prefill.get("purchase_order_name"),
    )
    inventory_item_lookup = "inventory.list_inventory_items" if "inventory.list_inventory_items" in tool_names else "inventory.search_inventory_items"
    inventory_item_options = await _load_lookup_options_by_tool_name(
        inventory_item_lookup,
        tool_specs=tool_specs,
        tool_executor=tool_executor,
        tool_ctx=tool_ctx,
        preferred_query=prefill.get("inventory_item_name"),
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
    default_start_at, default_end_at = _default_pricing_rule_window()
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
    product_options, matched_product_id = await _ensure_lookup_option_for_name(
        "product.search_products",
        desired_name=prefill.get("product_name"),
        options=product_options,
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
                        "product_id": matched_product_id,
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
                    {"name": "start_date", "type": "text", "label": "Start Date", "required": False, "placeholder": default_start_at},
                    {"name": "end_date", "type": "text", "label": "End Date", "required": False, "placeholder": default_end_at},
                    {"name": "description", "type": "textarea", "label": "Description", "required": False, "placeholder": "Optional rule description"},
                ],
                "current_values": {
                    key: value
                    for key, value in {
                        "name": prefill.get("name"),
                        "rule_type": prefill.get("rule_type"),
                        "product_id": matched_product_id,
                        "category_ref_id": _inventory_setup_prefill_option_value(category_options, prefill.get("category_name")),
                        "value": prefill.get("value"),
                        "discount_type": "PERCENTAGE",
                        "start_date": default_start_at,
                        "end_date": default_end_at,
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
        start_date = str(form_data.get("start_date") or "").strip() or None
        end_date = str(form_data.get("end_date") or "").strip() or None
        rule_type = str(form_data.get("rule_type") or "").strip().upper() or None
        if rule_type == "PROMO" and not (start_date and end_date):
            start_date, end_date = _default_pricing_rule_window()
        for key in ("name", "product_id", "category_ref_id", "discount_type", "value", "description"):
            _set_schema_arg(payload_arguments, payload_spec, [key], str(form_data.get(key) or "").strip() or None)
        _set_schema_arg(payload_arguments, payload_spec, ["rule_type"], rule_type)
        _set_schema_arg(payload_arguments, payload_spec, ["start_date", "startDate"], start_date)
        _set_schema_arg(payload_arguments, payload_spec, ["end_date", "endDate"], end_date)
    arguments = {"payload": payload_arguments} if payload_arguments else {}
    filtered = _filtered_tool_arguments(spec, arguments)
    return tool_name, filtered, _missing_required_arguments(spec, filtered)


def _pos_admin_action_from_text(text: str) -> str | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    read_only_prefixes = (
        "list ",
        "show ",
        "search ",
        "find ",
        "what ",
        "which ",
        "get ",
        "display ",
        "summarize ",
        "summarise ",
        "inspect ",
        "check ",
    )
    if normalized.startswith(read_only_prefixes):
        return None
    if any(
        token in normalized
        for token in (
            "create pos terminal",
            "add pos terminal",
            "new pos terminal",
            "create a pos terminal",
            "create terminal called",
            "add terminal called",
        )
    ):
        return "create_pos_terminal"
    if any(
        token in normalized
        for token in (
            "create pos discount",
            "add pos discount",
            "new pos discount",
            "create a pos discount",
            "create discount called",
            "add discount called",
        )
    ):
        return "create_pos_discount"
    return None


def _pos_admin_named_insight_from_text(text: str) -> str | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    comparison_terms = ("compare", "comparison", "versus", "vs", "side by side", "side-by-side", "performance")
    comparison_queries = _extract_product_comparison_queries_from_text(text)
    if (
        any(token in normalized for token in ("variant", "variants"))
        and any(token in normalized for token in comparison_terms)
    ):
        return "variant_comparison"
    if (
        any(token in normalized for token in comparison_terms)
        and len(comparison_queries) >= 2
        and (
            any(token in normalized for token in ("product", "products", "item", "items", "barcode", "sku"))
            or any(re.fullmatch(r"\d{8,18}", query.strip()) for query in comparison_queries)
            or not any(
                token in normalized
                for token in (
                    "location",
                    "locations",
                    "store",
                    "stores",
                    "staff",
                    "cashier",
                    "cashiers",
                    "supplier",
                    "suppliers",
                    "purchase order",
                    "audit",
                    "permission",
                    "security",
                )
            )
        )
    ):
        return "product_comparison"
    if (
        any(token in normalized for token in ("trend", "performance", "analyse", "analyze", "analysis", "compare", "comparison", "history"))
        and any(token in normalized for token in ("product", "variant", "item", "barcode", "sku"))
        and any(token in normalized for token in ("sales", "sold", "revenue", "units", "location", "locations"))
    ):
        return "product_sales_trend"
    if (
        any(token in normalized for token in ("highest sales", "best sales day", "peak sales day", "strongest sales day", "most sales in a day", "highest revenue day"))
        and any(token in normalized for token in ("day", "daily", "ever", "all time", "historical", "history"))
    ):
        return "best_sales_day"
    if (
        any(token in normalized for token in ("sales", "revenue", "orders", "order count", "avg basket", "average basket"))
        and any(
            token in normalized
            for token in (
                "how many",
                "total",
                "overview",
                "analysis",
                "analyse",
                "analyze",
                "analytics",
                "summary",
                "gross",
                "made",
                "recorded",
                "generated",
                "did we make",
                "did we do",
                "data",
            )
        )
        and not any(
            token in normalized
            for token in (
                "location",
                "locations",
                "branch",
                "branches",
                "top seller",
                "top sellers",
                "best seller",
                "best sellers",
                "payment",
                "terminal",
                "cashier",
            )
        )
    ):
        return "sales_overview"
    if _text_matches_all_terms(
        normalized,
        r"\b(sales?|revenue|orders?|basket)\b",
        r"\b(location|locations|branch|branches)\b",
    ):
        return "sales_by_location_today"
    if (
        _text_matches_all_terms(normalized, r"\b(top|best)\s+sellers?\b")
        or (
            any(
                token in normalized
                for token in (
                    "selling the most",
                    "sold the most",
                    "highest revenue",
                    "generated the highest revenue",
                    "strongest sellers",
                    "top-selling variants",
                    "product leaders",
                    "carrying sales",
                    "quantity sold",
                    "weekly top sellers",
                    "top sellers over seven days",
                    "skus are my top sellers",
                )
            )
        )
    ):
        return "top_sellers_seven_days"
    if (
        any(token in normalized for token in ("sku", "skus", "product", "products", "item", "items", "seller", "sellers", "revenue"))
        and any(token in normalized for token in ("top", "best", "highest", "strongest", "leaders", "sold the most", "selling the most"))
    ):
        return "top_sellers_seven_days"
    if any(
        token in normalized
        for token in (
            "payment mix",
            "payment method",
            "payment methods",
            "payment channel",
            "payment channels",
            "payment usage split",
            "payment usage",
            "cash versus transfer",
            "failed versus successful payment activity",
            "failed versus successful payment",
            "tender mix",
            "tender",
        )
    ):
        return "payment_mix"
    if any(
        token in normalized
        for token in (
            "pos activity from audit events",
            "audited activity",
            "timeline of pos actions",
            "pos settings recently",
            "pos configuration changes",
            "cashier actions are most frequent",
            "order-related pos events",
            "pos activity patterns look unusual",
            "pos audit activity",
            "pos operational events",
            "cashier performance",
            "sales by cashier",
            "terminal activity",
            "busiest",
            "terminal usage",
            "performance across terminals and cashiers",
            "processed the most orders",
            "revenue contribution",
            "average basket by cashier",
            "refund activity",
        )
    ):
        if any(
            token in normalized
            for token in (
                "audit",
                "audited",
                "timeline of pos actions",
                "pos settings recently",
                "pos configuration changes",
                "order-related pos events",
                "pos activity patterns look unusual",
                "pos operational events",
            )
        ):
            return "pos_audit_activity"
        return "terminal_cashier_activity"
    if any(
        token in normalized
        for token in (
            "open versus closed pos sessions",
            "sessions were opened and closed",
            "hourly sales trend",
            "driving the most sales today",
            "order flow",
            "order count trend",
            "draft, held, and completed",
            "session throughput",
            "longest active pos session",
            "trend lines for orders and revenue",
        )
    ):
        return "sessions_orders"
    if any(
        token in normalized
        for token in (
            "pos risk signals",
            "refunds unusually high",
            "suspicious activity",
            "voided or cancelled",
            "abnormal discount activity",
            "weak conversion",
            "operational attention",
            "pos exceptions",
            "payment or session anomalies",
            "investigate first",
        )
    ):
        return "pos_exceptions"
    return None


def _inventory_procurement_named_insight_from_text(text: str) -> str | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    if _text_matches_all_terms(normalized, r"\b(purchase order|purchase orders|po)\b", r"\b(receiving|received|receipts?)\b"):
        return "receiving_lifecycle"
    if any(
        token in normalized
        for token in (
            "purchase-order lifecycle",
            "purchase order lifecycle",
            "purchase-order pipeline",
            "purchase order pipeline",
            "purchase orders are pending, approved, issued, or received",
            "open purchase orders by status",
            "stage is each active po",
            "po workflow progress",
            "closest to completion",
            "purchase-order status split",
            "timeline view of current po activity",
            "stalled in the pipeline",
        )
    ):
        return "po_lifecycle"
    if any(
        token in normalized
        for token in (
            "po receiving lifecycle",
            "purchase orders received",
            "purchase order receiving",
            "purchase order receipts",
            "what was received",
            "receiving progress for open purchase orders",
            "partially received",
            "receiving timeline",
            "receipts landed today",
            "receiving completion progress",
            "waiting to be received",
            "goods-receipt activity",
            "blocked at receiving",
            "receiving progress board",
        )
    ):
        return "receiving_lifecycle"
    if (
        _text_matches_all_terms(normalized, r"\b(purchase order|purchase orders|po)\b", r"\b(status|statuses|pipeline|lifecycle|open|pending|approved|issued|received)\b")
        or _text_matches_all_terms(normalized, r"\bhow many\b", r"\b(purchase order|purchase orders|po)\b")
    ):
        return "po_lifecycle"
    if (
        _text_matches_all_terms(normalized, r"\b(purchase order|purchase orders|po)\b", r"\b(analysis|analytics|analyze|analyse)\b")
        or (
            re.search(r"\b(purchase order|purchase orders|po)\b", normalized, flags=re.IGNORECASE)
            and any(
                token in normalized
                for token in (
                    "last month",
                    "last 3 months",
                    "last three months",
                    "past month",
                    "past 3 months",
                    "past three months",
                    "this month",
                    "this quarter",
                    "last quarter",
                    "last year",
                    "past year",
                )
            )
        )
    ):
        return "po_lifecycle"
    if any(
        token in normalized
        for token in (
            "supplier performance",
            "delivering on time",
            "fill rate and delay rate",
            "receiving reliability",
            "suppliers create the most receiving exceptions",
            "completion quality by supplier",
            "supplier responsiveness",
            "trust most for urgent restocks",
            "supplier scorecards",
            "performance summary for procurement",
        )
    ):
        return "supplier_performance"
    if any(
        token in normalized
        for token in (
            "delayed purchase orders",
            "need escalation right now",
            "receiving exceptions by severity",
            "overdue for receiving",
            "procurement risks across active suppliers",
            "missing or blocked receipt activity",
            "procurement exceptions from the audit trail",
            "procurement problems should i investigate first",
            "highest operational risk",
            "purchasing delays affecting stock availability",
        )
    ):
        return "delay_exceptions"
    if any(
        token in normalized
        for token in (
            "cost variance across recent purchase orders",
            "biggest price variance",
            "expected versus received procurement cost",
            "landed cost is drifting",
            "procurement lines deserve price review first",
        )
    ):
        return "cost_variance"
    return None


def _product_discovery_named_insight_from_text(text: str) -> str | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    if any(
        token in normalized
        for token in (
            "product activity from audit events",
            "products were edited most recently",
            "audit timeline for product changes",
            "recent catalog activity",
            "barcode or sku change activity",
            "product families are seeing the most edits",
            "recently modified products with audit evidence",
            "product records changed across multiple staff",
            "product audit activity by category",
            "product changes should i review first",
        )
    ):
        return "product_audit_activity"
    if any(
        token in normalized
        for token in (
            "global catalog import opportunities",
            "global products should i import next",
            "import opportunities",
            "global catalog products match my current assortment gaps",
            "catalog opportunities not yet imported",
            "brands have the biggest import potential",
            "likely import wins for the current workspace",
            "global products can expand my catalog fastest",
            "rank import opportunities",
            "global catalog opportunity board",
        )
    ):
        return "import_opportunities"
    if any(
        token in normalized
        for token in (
            "look up product variants by barcode",
            "matching variant for a scanned barcode",
            "product variant matches this sku or barcode",
            "best catalog match for a product code",
            "look up a product variant",
            "variant record behind a barcode scan",
            "variant is tied to this catalog code",
            "variant lookup result",
            "closest global catalog match for a code",
            "product family and variant that match a lookup",
        )
    ):
        return "variant_lookup"
    if any(
        token in normalized
        for token in (
            "catalog gaps in my current assortment",
            "categories look underrepresented",
            "product families are missing versus demand signals",
            "product assortment risks by category",
            "brands are thin in my catalog",
            "gaps between imported catalog and active sales mix",
            "products should exist here but do not yet",
            "assortment weaknesses",
            "catalog gaps likely hurting sales coverage",
            "catalog gaps should i close first",
        )
    ):
        return "catalog_gaps"
    if any(
        token in normalized
        for token in (
            "duplicate barcode risks",
            "skus or barcodes may conflict",
            "product code collisions",
            "duplicate identifiers",
            "barcode conflicts",
            "duplicate code pressure",
            "code conflicts could block imports",
            "duplicate variant records",
            "barcode or sku issues should i resolve first",
            "duplicate code risk",
        )
    ):
        return "duplicate_codes"
    if any(
        token in normalized
        for token in (
            "missing strong media coverage",
            "better curated product content",
            "weak image coverage",
            "merchandising cleanup first",
            "content quality opportunities",
        )
    ):
        return "media_category"
    return None


def _host_passthrough_named_insight_from_text(text: str) -> str | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None

    pos_insight = _pos_admin_named_insight_from_text(normalized)
    if pos_insight:
        return f"pos::{pos_insight}"

    inventory_visibility_insight = _inventory_visibility_named_insight_from_text(normalized)
    if inventory_visibility_insight:
        return f"inventory_visibility::{inventory_visibility_insight}"

    inventory_procurement_insight = _inventory_procurement_named_insight_from_text(normalized)
    if inventory_procurement_insight:
        return f"inventory_procurement::{inventory_procurement_insight}"

    users_insight = _users_named_insight_from_text(normalized)
    if users_insight:
        return f"users::{users_insight}"

    product_discovery_insight = _product_discovery_named_insight_from_text(normalized)
    if product_discovery_insight:
        return f"product_discovery::{product_discovery_insight}"

    return None


def _host_named_insight_from_text(text: str) -> str | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    if any(
        token in normalized
        for token in (
            "business analyst",
            "data analyst",
            "act as an analyst",
            "act like an analyst",
            "analyze the entire data",
            "analyse the entire data",
            "analyze my whole business",
            "analyse my whole business",
            "analyze the whole workspace",
            "analyse the whole workspace",
            "analyze my entire system",
            "analyse my entire system",
            "analyze the entire system",
            "analyse the entire system",
            "analyze my whole system",
            "analyse my whole system",
            "analyze the whole system",
            "analyse the whole system",
            "what am i not seeing",
            "what are we missing",
            "hidden insight",
            "hidden insights",
            "strategic business review",
            "management review",
            "owner review",
        )
    ) or (
        _text_matches_all_terms(normalized, r"\b(business|data)\b", r"\banalyst\b")
        or _text_matches_all_terms(normalized, r"\b(entire|whole|overall)\b", r"\b(data|business|workspace|system|operation|operations)\b", r"\b(analy[sz]e|review|insight|insights)\b")
        or _text_matches_all_terms(normalized, r"\bwhat\b", r"\b(missing|not seeing|overlooked)\b")
    ):
        return "business_analyst_review"
    if any(
        token in normalized
        for token in (
            "compare locations",
            "compare branches",
            "location performance",
            "branch is winning on sales",
            "branch has strong sales but poor replenishment posture",
            "location needs the most intervention",
            "branch scorecard",
            "overall operational readiness",
            "side-by-side location performance",
            "revenue, orders, and stock risk",
            "location performance gaps",
            "top sellers and stockouts",
            "rank locations",
        )
    ) or (
        _text_matches_all_terms(normalized, r"\b(compare|rank)\b", r"\b(location|locations|branch|branches)\b")
        or _text_matches_all_terms(normalized, r"\b(location|branch)\b", r"\b(scorecard|performance)\b")
    ):
        return "location_comparison"
    if any(
        token in normalized
        for token in (
            "top three actions",
            "urgent operator recommendations",
            "improve sales and reduce stock risk fastest",
            "prioritized action plan",
            "what should i do first",
        )
    ) or (
        _text_matches_all_terms(normalized, r"\b(action|actions|recommendation|recommendations)\b", r"\b(next|urgent|prioritized|first)\b")
        or _text_matches_all_terms(normalized, r"\bwhat\b", r"\bshould i do first\b")
    ):
        return "recommendations"
    if any(
        token in normalized
        for token in (
            "operational summary for today",
            "one-screen operational summary",
            "business signals across sales, stock, and purchasing",
            "whole workspace",
            "revenue, stock risk, and po status together",
            "executive operational snapshot",
            "strong and weak across inventory and pos",
            "cross-service health overview",
            "business posture across sales, stock, staff, and procurement",
            "operational changes since yesterday",
            "workspace summary with the key metrics and risks",
            "pay attention to first across the whole workspace",
        )
    ) or (
        _text_matches_all_terms(normalized, r"\boperational\b", r"\bsummary\b")
        or _text_matches_all_terms(normalized, r"\bbusiness\b", r"\bsignals?\b")
        or _text_matches_all_terms(normalized, r"\bwhole\b", r"\bworkspace\b")
        or _text_matches_all_terms(normalized, r"\brevenue\b", r"\bstock\b", r"\bpo\b")
        or _text_matches_all_terms(normalized, r"\bexecutive\b", r"\bsnapshot\b")
        or _text_matches_all_terms(normalized, r"\bstrong\b", r"\bweak\b", r"\b(inventory|stock)\b", r"\b(pos|sales)\b")
        or _text_matches_all_terms(normalized, r"\bcross[\s-]*service\b", r"\bhealth\b")
        or _text_matches_all_terms(normalized, r"\bbusiness\b", r"\bposture\b")
        or _text_matches_all_terms(normalized, r"\boperational\b", r"\bchanges?\b", r"\byesterday\b")
        or _text_matches_all_terms(normalized, r"\bworkspace\b", r"\bsummary\b", r"\b(metrics|risks?)\b")
    ):
        return "cross_domain_ops"
    passthrough_insight = _host_passthrough_named_insight_from_text(normalized)
    if passthrough_insight:
        return passthrough_insight
    return None


def _inventory_visibility_named_insight_from_text(text: str) -> str | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    if any(
        token in normalized
        for token in (
            "out-of-stock",
            "out of stock",
            "stockout",
            "stockouts",
            "zero-balance",
            "zero balance",
            "missing from the shelf",
            "missing from shelf",
        )
    ):
        return "stock_risk_out_of_stock"
    if _text_matches_all_terms(normalized, r"\blow\b", r"\bstock\b"):
        return "stock_risk_low_stock"
    if any(token in normalized for token in ("run out soon", "below reorder level", "closest to stockout")):
        return "stock_risk_low_stock"
    if (
        any(token in normalized for token in ("supplier-linked", "supplier linked", "supplier defaults"))
        and "replenishment first" in normalized
    ):
        return "reorder_candidates"
    if any(
        token in normalized
        for token in (
            "stock value",
            "inventory value",
            "stock value change",
            "stock value changes",
            "inventory value change",
            "inventory value changes",
            "value variance",
            "stock variance",
            "inventory variance",
            "value trend",
            "stock value trend",
            "inventory value trend",
        )
    ):
        return "stock_value_changes"
    if any(
        token in normalized
        for token in (
            "realtime dashboard snapshot",
            "realtime snapshot",
            "real-time dashboard snapshot",
            "live dashboard snapshot",
            "current dashboard snapshot",
            "dashboard snapshot right now",
            "inventory health across locations",
            "location has the biggest stock risk",
            "biggest stock risk right now",
            "stock posture by branch",
            "stock by location",
            "replenishment first",
            "category concentration by location",
            "overstocked versus understocked",
            "inventory readiness",
            "healthiest from an inventory standpoint",
            "inventory value and stock pressure",
        )
    ):
        if any(
            token in normalized
            for token in (
                "realtime dashboard snapshot",
                "realtime snapshot",
                "real-time dashboard snapshot",
                "live dashboard snapshot",
                "current dashboard snapshot",
                "dashboard snapshot right now",
            )
        ):
            return "realtime_snapshot"
        return "location_health"
    if (
        _text_matches_all_terms(normalized, r"\breorder\b", r"\b(candidate|candidates|need|needs|priority|priorities|now)\b")
        or any(
            token in normalized
            for token in (
                "purchasing prioritize",
                "safety stock",
                "supplier defaults",
                "reorder pressure",
                "reorder short list",
            )
        )
    ):
        return "reorder_candidates"
    if _text_matches_all_terms(normalized, r"\b(stock|inventory)\b", r"\b(risk|risks|alerts)\b"):
        return "stock_risk"
    if (
        _text_matches_all_terms(normalized, r"\b(stock|inventory)\b", r"\bmovement\b")
        or any(
            token in normalized
            for token in (
                "recent stock movements",
                "inventory movements happened today",
                "stock in versus stock out",
                "movement timeline",
                "receiving, transfers, and issues",
                "movement trends",
                "movement history",
                "movement dashboard",
                "stock depletion",
            )
        )
    ):
        return "stock_movements"
    if any(
        token in normalized
        for token in (
            "adjustment risk",
            "stock adjustments",
            "adjustment-heavy",
            "adjustment activity by staff and item",
            "manual corrections",
            "negative adjustment trend",
            "correction patterns",
            "process issues",
            "adjustment anomalies",
        )
    ):
        return "adjustment_risk"
    return None


def _inventory_risk_rows(payload: dict[str, Any], *, focus: str | None = None) -> list[dict[str, Any]]:
    risk_items = payload.get("risk_items") if isinstance(payload.get("risk_items"), dict) else {}
    if focus and isinstance(risk_items.get(focus), list):
        rows = risk_items.get(focus)
    else:
        rows = []
        for key in ("out_of_stock", "needs_reorder", "low_stock", "expiring_soon"):
            value = risk_items.get(key)
            if isinstance(value, list):
                rows.extend(value)
    return [row for row in rows if isinstance(row, dict)]


def _build_inventory_stock_risk_insight(payload: dict[str, Any], *, focus: str | None = None) -> dict[str, Any]:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    rows = _inventory_risk_rows(payload, focus=focus)
    ranked_items = []
    risk_panel_items = []
    for row in rows[:12]:
        label = _first_string(row, ["name", "inventory_item_name", "product_variant", "sku", "barcode"]) or "Inventory item"
        quantity = float(row.get("quantity_available") or row.get("quantity") or 0)
        location_name = _first_string(row, ["location_name"]) or "Unassigned location"
        ranked_items.append(
            {
                "label": label,
                "value": quantity,
                "secondary_value": location_name,
                "meta": {
                    "sku": str(row.get("sku") or ""),
                    "barcode": str(row.get("barcode") or ""),
                    "location_name": location_name,
                    "reorder_point": float(row.get("reorder_point") or 0),
                    "minimum_stock_level": float(row.get("minimum_stock_level") or 0),
                },
            }
        )
        risk_panel_items.append(
            {
                "label": label,
                "severity": "high" if focus == "out_of_stock" else "medium",
                "detail": f"{location_name} · available {quantity}",
                "next_action": "Review replenishment or transfer options.",
            }
        )
    if focus == "out_of_stock":
        summary_text = "Out-of-stock products are ready."
    elif focus == "low_stock":
        summary_text = "Low-stock products are ready."
    elif focus == "needs_reorder":
        summary_text = "Reorder candidates are ready."
    else:
        summary_text = "Inventory stock risk is ready."
    if not rows:
        if focus == "out_of_stock":
            summary_text = "No out-of-stock products are active right now."
        elif focus == "low_stock":
            summary_text = "No low-stock products are active right now."
        elif focus == "needs_reorder":
            summary_text = "No reorder candidates are active right now."
        else:
            summary_text = "No inventory stock risks are active right now."
    return {
        "kind": "insight_response",
        "summary": summary_text,
        "widgets": [
            {
                "type": "metric_grid",
                "title": "Inventory risk posture",
                "data": [
                    {"label": "Out of Stock", "value": int(summary.get("out_of_stock_count") or 0)},
                    {"label": "Needs Reorder", "value": int(summary.get("reorder_count") or 0)},
                    {"label": "Low Stock", "value": int(summary.get("low_stock_count") or 0)},
                    {"label": "Expiring Soon", "value": int(summary.get("expiring_count") or 0)},
                ],
            },
            {
                "type": "risk_panel",
                "title": "Highest priority stock risks",
                "items": risk_panel_items,
            },
            {
                "type": "ranked_list",
                "title": "Items needing attention",
                "items": ranked_items,
                "ordered_by": "quantity_available",
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "inventory", "endpoint_or_topic": "get_stock_risk", "freshness": "live"}],
        "permissions_checked": ["read_inventory"],
        "confidence": "high",
        "warnings": [] if rows else ["No inventory items matched the requested risk posture."],
    }


def _build_inventory_movements_insight(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results") if isinstance(payload, dict) else []
    results = results if isinstance(results, list) else []
    timeline_items = []
    type_counts: dict[str, int] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        movement_type = str(item.get("movement_type") or "movement")
        type_counts[movement_type] = type_counts.get(movement_type, 0) + 1
        timeline_items.append(
            {
                "timestamp": str(item.get("occurred_at") or ""),
                "title": f"{movement_type.replace('_', ' ').title()} {float(item.get('quantity') or 0):g}",
                "description": str(item.get("inventory_item_name") or ""),
                "severity": "info",
            }
        )
    chart_rows = [{"label": key.replace("_", " ").title(), "count": value} for key, value in type_counts.items()]
    return {
        "kind": "insight_response",
        "summary": "Recent stock movements are ready." if timeline_items else "No recent stock movements were found.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": "Movement snapshot",
                "data": [
                    {"label": "Events", "value": len(timeline_items)},
                    {"label": "Movement Types", "value": len(chart_rows)},
                ],
            },
            {
                "type": "timeline",
                "title": "Recent stock movements",
                "events": timeline_items,
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "inventory", "endpoint_or_topic": "get_stock_movements", "freshness": "live"}],
        "permissions_checked": ["read_inventory"],
        "confidence": "high",
        "warnings": [] if timeline_items else ["No recent stock movements matched the requested window."],
    }


def _build_inventory_location_health_insight(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results") if isinstance(payload, dict) else []
    results = results if isinstance(results, list) else []
    rows = []
    risk_items = []
    for item in results:
        if not isinstance(item, dict):
            continue
        row = {
            "location": str(item.get("name") or "Location"),
            "items": int(item.get("total_items") or 0),
            "quantity": float(item.get("total_quantity") or 0),
            "value": float(item.get("total_value") or 0),
            "expiring_soon": int(item.get("expiring_soon_count") or 0),
        }
        rows.append(row)
        if row["expiring_soon"] > 0:
            risk_items.append(
                {
                    "label": row["location"],
                    "severity": "medium",
                    "description": f"{row['expiring_soon']} items expiring soon.",
                }
            )
    return {
        "kind": "insight_response",
        "summary": "Location inventory health is ready." if rows else "No location inventory summary is available.",
        "widgets": [
            {
                "type": "comparison_table",
                "title": "Inventory health by location",
                "columns": ["location", "items", "quantity", "value", "expiring_soon"],
                "rows": rows,
            },
            {
                "type": "risk_panel",
                "title": "Location pressure",
                "items": risk_items or [{"label": "All locations", "severity": "low", "description": "No immediate expiring-stock pressure detected."}],
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "inventory", "endpoint_or_topic": "search_stock_locations", "freshness": "live"}],
        "permissions_checked": ["read_inventory"],
        "confidence": "medium",
        "warnings": [] if rows else ["No stock-location summaries were returned."],
    }


def _build_realtime_dashboard_snapshot_insight(
    snapshot_payload: dict[str, Any],
    alerts_payload: dict[str, Any],
) -> dict[str, Any]:
    metrics = snapshot_payload.get("metrics") if isinstance(snapshot_payload.get("metrics"), dict) else {}
    charts = snapshot_payload.get("charts") if isinstance(snapshot_payload.get("charts"), dict) else {}
    leaderboards = snapshot_payload.get("leaderboards") if isinstance(snapshot_payload.get("leaderboards"), dict) else {}
    alerts = snapshot_payload.get("alerts") if isinstance(snapshot_payload.get("alerts"), dict) else {}
    feed = snapshot_payload.get("feed") if isinstance(snapshot_payload.get("feed"), list) else []

    metric_rows = [
        {"label": "Sales 24h", "value": float(metrics.get("sales_24h_amount") or 0)},
        {"label": "Orders 24h", "value": int(metrics.get("sales_24h_orders") or 0)},
        {"label": "Receiving Units 24h", "value": int(metrics.get("receiving_24h_units") or 0)},
        {"label": "Attention Items", "value": int(alerts.get("total_attention_items") or 0)},
        {"label": "Security Events 24h", "value": int(metrics.get("security_events_24h") or 0)},
        {"label": "Unread Alerts", "value": int(alerts_payload.get("unread_count") or 0)},
    ]
    sales_chart = charts.get("sales_amount_by_hour") if isinstance(charts.get("sales_amount_by_hour"), list) else []
    leaderboard_rows = leaderboards.get("top_products_24h") if isinstance(leaderboards.get("top_products_24h"), list) else []
    ranked_items = [
        {
            "label": str(item.get("title") or "Product"),
            "value": float(item.get("metric_value") or 0),
            "secondary_value": str(item.get("subtitle") or ""),
        }
        for item in leaderboard_rows[:8]
        if isinstance(item, dict)
    ]
    risk_items = [
        {
            "label": "High severity activity",
            "severity": "high" if int(alerts.get("high_severity_24h") or 0) > 0 else "low",
            "description": f"{int(alerts.get('high_severity_24h') or 0)} high-severity events in the last 24 hours.",
        },
        {
            "label": "Support access activity",
            "severity": "medium" if int(alerts.get("support_access_24h") or 0) > 0 else "low",
            "description": f"{int(alerts.get('support_access_24h') or 0)} support-access events in the last 24 hours.",
        },
        {
            "label": "Notification pressure",
            "severity": "medium" if int(alerts_payload.get("unread_count") or 0) > 0 else "low",
            "description": f"{int(alerts_payload.get('unread_count') or 0)} unread notifications for the active workspace.",
        },
    ]
    timeline_events = [
        {
            "title": str(item.get("title") or item.get("summary") or item.get("event_name") or "Audit event"),
            "subtitle": str(item.get("subtitle") or item.get("source_service") or ""),
            "timestamp": str(item.get("occurred_at") or ""),
            "severity": str(item.get("severity") or "info"),
            "meta": [
                {"label": "Actor", "value": str(item.get("actor_name") or "")},
                {"label": "Target", "value": str(item.get("target_label") or "")},
            ],
        }
        for item in feed[:8]
        if isinstance(item, dict)
    ]
    warnings = []
    if not sales_chart:
        warnings.append("No hourly sales series was returned in the realtime snapshot.")
    if not timeline_events:
        warnings.append("No recent audit feed items were returned in the realtime snapshot.")
    return {
        "kind": "insight_response",
        "summary": "Realtime operational snapshot is ready.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": "Realtime dashboard snapshot",
                "data": metric_rows,
            },
            {
                "type": "line_chart",
                "title": "Sales amount by hour",
                "data": sales_chart,
                "x_key": "bucket",
                "y_key": "value",
            },
            {
                "type": "ranked_list",
                "title": "Top products in the last 24 hours",
                "items": ranked_items,
                "ordered_by": "metric_value",
            },
            {
                "type": "risk_panel",
                "title": "Attention signals",
                "items": risk_items,
            },
            {
                "type": "timeline",
                "title": "Recent operational feed",
                "events": timeline_events,
            },
        ],
        "suggested_actions": [],
        "data_sources": [
            {"service": "audit", "endpoint_or_topic": "get_realtime_dashboard_snapshot", "freshness": "live"},
            {"service": "notifications", "endpoint_or_topic": "get_alert_summary", "freshness": "live"},
        ],
        "permissions_checked": ["view_audit_logs"],
        "confidence": "high",
        "warnings": warnings,
    }


def _build_stock_value_change_insight(
    analytics_payload: dict[str, Any],
    movements_payload: dict[str, Any],
    *,
    window_label: str,
) -> dict[str, Any]:
    analytics = analytics_payload.get("analytics") if isinstance(analytics_payload, dict) else {}
    analytics = analytics if isinstance(analytics, dict) else {}
    location_distribution = analytics.get("location_distribution") if isinstance(analytics.get("location_distribution"), list) else []
    aging_analysis = analytics.get("aging_analysis") if isinstance(analytics.get("aging_analysis"), dict) else {}
    movements = movements_payload.get("results") if isinstance(movements_payload, dict) else []
    movements = movements if isinstance(movements, list) else []

    def _movement_value(row: dict[str, Any]) -> float:
        movement_type = str(row.get("movement_type") or "").strip().lower()
        quantity = float(row.get("quantity") or 0)
        unit_cost = float(row.get("unit_cost") or 0)
        gross_value = quantity * unit_cost
        if row.get("from_location_id") and row.get("to_location_id"):
            return 0.0
        negative_tokens = ("issue", "ship", "dispatch", "sale", "write", "damage", "shrink", "consume", "return_out")
        positive_tokens = ("receive", "opening", "return_in", "count_surplus", "restock")
        if any(token in movement_type for token in negative_tokens):
            return -abs(gross_value)
        if any(token in movement_type for token in positive_tokens):
            return abs(gross_value)
        if row.get("from_location_id") and not row.get("to_location_id"):
            return -abs(gross_value)
        if row.get("to_location_id") and not row.get("from_location_id"):
            return abs(gross_value)
        return gross_value

    daily_totals: dict[str, float] = {}
    inflow_value = 0.0
    outflow_value = 0.0
    for movement in movements:
        if not isinstance(movement, dict):
            continue
        raw_timestamp = str(movement.get("occurred_at") or "").strip()
        if len(raw_timestamp) < 10:
            continue
        bucket = raw_timestamp[:10]
        delta_value = _movement_value(movement)
        daily_totals[bucket] = round(daily_totals.get(bucket, 0.0) + delta_value, 2)
        if delta_value >= 0:
            inflow_value += delta_value
        else:
            outflow_value += abs(delta_value)

    chart_rows = [{"bucket": bucket, "value": value} for bucket, value in sorted(daily_totals.items())]
    table_rows = [
        {
            "location": str(item.get("location_name") or "Location"),
            "items": int(item.get("item_count") or 0),
            "quantity": float(item.get("total_quantity") or 0),
            "value": float(item.get("total_value") or 0),
        }
        for item in location_distribution[:10]
        if isinstance(item, dict)
    ]
    risk_items = [
        {
            "label": "Aged stock",
            "severity": "medium" if int(aging_analysis.get("over_1_year") or 0) > 0 else "low",
            "description": f"{int(aging_analysis.get('over_1_year') or 0)} stock positions are older than one year.",
        },
        {
            "label": "Mid-age stock",
            "severity": "medium" if int(aging_analysis.get("91-365_days") or 0) > 0 else "low",
            "description": f"{int(aging_analysis.get('91-365_days') or 0)} stock positions have aged beyond 90 days.",
        },
        {
            "label": "Observed value swings",
            "severity": "medium" if abs(inflow_value - outflow_value) > 0 else "low",
            "description": f"Inflows were {round(inflow_value, 2)} and outflows were {round(outflow_value, 2)} for {window_label}.",
        },
    ]
    return {
        "kind": "insight_response",
        "summary": f"Stock value analysis is ready for {window_label}.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": f"Stock value for {window_label}",
                "data": [
                    {"label": "Current Stock Value", "value": float(analytics.get("total_stock_value") or 0)},
                    {"label": "Value Inflows", "value": round(inflow_value, 2)},
                    {"label": "Value Outflows", "value": round(outflow_value, 2)},
                    {"label": "Locations", "value": int(analytics.get("total_locations") or 0)},
                ],
            },
            {
                "type": "line_chart",
                "title": f"Stock value changes for {window_label}",
                "data": chart_rows,
                "x_key": "bucket",
                "y_key": "value",
            },
            {
                "type": "comparison_table",
                "title": "Stock value by location",
                "columns": ["location", "items", "quantity", "value"],
                "rows": table_rows,
            },
            {
                "type": "risk_panel",
                "title": "Value variance watchpoints",
                "items": risk_items,
            },
        ],
        "suggested_actions": [],
        "data_sources": [
            {"service": "inventory", "endpoint_or_topic": "get_stock_analytics", "freshness": "live"},
            {"service": "inventory", "endpoint_or_topic": "get_stock_movements", "freshness": "live"},
        ],
        "permissions_checked": ["read_inventory"],
        "confidence": "medium",
        "warnings": [] if chart_rows else [f"No stock movements were returned for {window_label}."],
    }


def _build_inventory_adjustment_risk_insight(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results") if isinstance(payload, dict) else []
    results = results if isinstance(results, list) else []
    risk_items = []
    timeline_items = []
    for item in results:
        if not isinstance(item, dict):
            continue
        qty = float(item.get("quantity") or 0)
        title = str(item.get("inventory_item_name") or "Inventory item")
        timeline_items.append(
            {
                "timestamp": str(item.get("occurred_at") or ""),
                "title": f"Adjustment {qty:g}",
                "description": title,
                "severity": "medium" if qty < 0 else "info",
            }
        )
        if qty < 0:
            risk_items.append(
                {
                    "label": title,
                    "severity": "high" if abs(qty) >= 5 else "medium",
                    "description": f"Negative adjustment of {abs(qty):g}.",
                }
            )
    return {
        "kind": "insight_response",
        "summary": "Inventory adjustment risk is ready." if timeline_items else "No recent adjustment activity was found.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": "Adjustment snapshot",
                "data": [
                    {"label": "Adjustment Events", "value": len(timeline_items)},
                    {"label": "Negative Adjustments", "value": len(risk_items)},
                ],
            },
            {
                "type": "risk_panel",
                "title": "Adjustment anomalies",
                "items": risk_items or [{"label": "Current adjustment posture", "severity": "low", "description": "No negative adjustment spikes detected."}],
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "inventory", "endpoint_or_topic": "get_stock_movements", "freshness": "live"}],
        "permissions_checked": ["read_inventory"],
        "confidence": "medium",
        "warnings": [] if timeline_items else ["No recent adjustment movements matched the requested window."],
    }


def _build_pos_sales_by_location_insight(payload: dict[str, Any]) -> dict[str, Any]:
    window_label = _payload_window_label(payload, fallback="today")
    groups = payload.get("groups") if isinstance(payload, dict) else []
    groups = groups if isinstance(groups, list) else []
    total_sales = float(payload.get("total_sales") or 0) if isinstance(payload, dict) else 0.0
    total_orders = sum(int(item.get("order_count") or 0) for item in groups if isinstance(item, dict))
    chart_rows = [
        {
            "label": str(item.get("label") or "Unassigned location"),
            "value": float(item.get("total_sales") or 0),
            "order_count": int(item.get("order_count") or 0),
            "average_basket": round(
                float(item.get("total_sales") or 0) / max(int(item.get("order_count") or 0), 1),
                2,
            ),
        }
        for item in groups
        if isinstance(item, dict)
    ]
    top_location = chart_rows[0]["label"] if chart_rows else "No sales"
    average_basket = round(total_sales / max(total_orders, 1), 2) if total_orders else 0.0
    summary = (
        f"{top_location} leads sales for {window_label}."
        if chart_rows
        else f"No completed POS sales were recorded for {window_label}."
    )
    explanation = (
        f"{top_location} generated the most revenue in {window_label}, while average basket size settled at {average_basket}."
        if chart_rows
        else "There is no completed sales activity to compare across locations for the selected window."
    )
    return {
        "kind": "insight_response",
        "summary": summary,
        "explanation": explanation,
        "insights": (
            [
                {
                    "title": "Leading location",
                    "detail": f"{top_location} produced the highest sales volume for {window_label}.",
                },
                {
                    "title": "Coverage",
                    "detail": f"{len(chart_rows)} locations contributed {total_orders} completed orders.",
                },
            ]
            if chart_rows
            else []
        ),
        "widgets": [
            {
                "type": "metric_grid",
                "title": f"{window_label.title()} across locations",
                "data": [
                    {"label": "Sales", "value": round(total_sales, 2)},
                    {"label": "Orders", "value": total_orders},
                    {"label": "Avg Basket", "value": average_basket},
                    {"label": "Locations", "value": len(chart_rows)},
                ],
            },
            {
                "type": "bar_chart",
                "title": f"Sales by location for {window_label}",
                "data": chart_rows,
                "x_key": "label",
                "y_key": "value",
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "pos", "endpoint_or_topic": "get_sales_summary", "freshness": "live"}],
        "permissions_checked": ["view_pos_reports"],
        "confidence": "high",
        "warnings": [] if chart_rows else ["No completed sales matched the requested window."],
    }


def _xml_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _product_image_url(item: dict[str, Any]) -> str:
    for key in ("image_url", "product_variant_image_url", "display_image", "image"):
        value = str(item.get(key) or "").strip()
        if value:
            return value

    label = str(item.get("variant_name") or item.get("product_name") or item.get("label") or "Product").strip()
    barcode = str(item.get("barcode") or item.get("barcode_snapshot") or "").strip()
    sku = str(item.get("sku") or item.get("sku_snapshot") or "").strip()
    seed = (barcode or sku or label or "product").lower()
    palette = [
        ("#0f766e", "#ecfeff", "#99f6e4"),
        ("#1d4ed8", "#eff6ff", "#bfdbfe"),
        ("#b45309", "#fffbeb", "#fde68a"),
        ("#be123c", "#fff1f2", "#fecdd3"),
        ("#6d28d9", "#f5f3ff", "#ddd6fe"),
        ("#047857", "#ecfdf5", "#a7f3d0"),
    ]
    bg, fg, accent = palette[sum(ord(char) for char in seed) % len(palette)]
    initials = "".join(part[0] for part in re.split(r"[^A-Za-z0-9]+", label) if part)[:3].upper() or "IMS"
    display_label = _xml_text(label[:34])
    display_code = _xml_text(barcode or sku or "No barcode")
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160" viewBox="0 0 160 160">
<rect width="160" height="160" rx="28" fill="{fg}"/>
<circle cx="126" cy="30" r="34" fill="{accent}"/>
<rect x="22" y="22" width="116" height="78" rx="22" fill="{bg}"/>
<text x="80" y="74" text-anchor="middle" font-family="Inter,Arial,sans-serif" font-size="34" font-weight="800" fill="white">{_xml_text(initials)}</text>
<text x="80" y="122" text-anchor="middle" font-family="Inter,Arial,sans-serif" font-size="11" font-weight="700" fill="#0f172a">{display_label}</text>
<text x="80" y="140" text-anchor="middle" font-family="ui-monospace,SFMono-Regular,Menlo,monospace" font-size="9" fill="#475569">{display_code}</text>
</svg>"""
    return "data:image/svg+xml;utf8," + quote(svg)


def _extract_product_query_from_text(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    quoted = re.search(r"['\"]([^'\"]{2,120})['\"]", normalized)
    if quoted:
        return quoted.group(1).strip()
    for pattern in (
        r"\bbarcode\s*(?:is|:|#)?\s*([A-Za-z0-9][A-Za-z0-9_-]{3,80})\b",
        r"\bsku\s*(?:is|:|#)?\s*([A-Za-z0-9][A-Za-z0-9_-]{2,80})\b",
        r"\bvariant\s*(?:id|code)?\s*(?:is|:|#)?\s*([A-Za-z0-9][A-Za-z0-9_-]{3,80})\b",
    ):
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    numeric_code = re.search(r"\b\d{8,18}\b", normalized)
    if numeric_code:
        return numeric_code.group(0)
    for pattern in (
        r"\b(?:for|of|on|about)\s+(.+?)(?:\s+(?:for|over|across|in|during|from|between)\b|[?.!]|$)",
        r"\b(?:trend|analysis|performance|compare)\s+(.+?)(?:\s+(?:for|over|across|in|during|from|between)\b|[?.!]|$)",
    ):
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            candidate = re.sub(r"\b(the|this|that|product|variant|item|sales|trend|analysis|performance)\b", " ", match.group(1), flags=re.IGNORECASE)
            candidate = re.sub(r"\s+", " ", candidate).strip(" .,:;")
            if len(candidate) >= 2:
                return candidate
    return ""


def _extract_product_comparison_queries_from_text(text: str) -> list[str]:
    normalized = str(text or "").strip()
    if not normalized:
        return []
    quoted = [item.strip() for item in re.findall(r"['\"]([^'\"]{2,120})['\"]", normalized) if item.strip()]
    if len(quoted) >= 2:
        return quoted[:5]

    candidate = re.sub(
        r"(?i)\b(compare|comparison|versus|vs|side by side|side-by-side|performance|sales|revenue|units|orders|order count|products?|items?)\b",
        " ",
        normalized,
    )
    candidate = re.split(r"(?i)\b(?:for|over|during|from|between|in the past|last|past)\b", candidate, maxsplit=1)[0]
    parts = re.split(r"(?i)\s+(?:vs\.?|versus|and|against|with)\s+|[,/]+", candidate)
    cleaned: list[str] = []
    for part in parts:
        value = re.sub(r"\s+", " ", part).strip(" .,:;?\"'")
        value = re.sub(r"(?i)\b(the|this|that|a|an|of|by|barcode|sku|code)\b", " ", value)
        value = re.sub(r"\s+", " ", value).strip(" .,:;?\"'")
        if len(value) >= 2 and value.lower() not in {"product", "products", "item", "items"}:
            cleaned.append(value)

    deduped: list[str] = []
    seen: set[str] = set()
    for value in cleaned:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped[:5]


def _build_pos_sales_overview_insight(
    payload: dict[str, Any],
    *,
    top_sellers_payload: dict[str, Any] | None = None,
    daily_sales_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    window_label = _payload_window_label(payload, fallback="the selected period")
    groups = payload.get("groups") if isinstance(payload, dict) else []
    groups = groups if isinstance(groups, list) else []
    total_sales = float(payload.get("total_sales") or 0) if isinstance(payload, dict) else 0.0
    total_orders = sum(int(item.get("order_count") or 0) for item in groups if isinstance(item, dict))
    average_basket = round(total_sales / max(total_orders, 1), 2) if total_orders else 0.0
    rows = [
        {
            "location": str(item.get("label") or "Unassigned location"),
            "orders": int(item.get("order_count") or 0),
            "sales": round(float(item.get("total_sales") or 0), 2),
        }
        for item in groups
        if isinstance(item, dict)
    ]
    sales_chart_rows = [
        {
            "label": row["location"],
            "value": row["sales"],
        }
        for row in rows
    ]
    order_share_rows = [
        {
            "label": row["location"],
            "value": row["orders"],
        }
        for row in rows
        if row["orders"] > 0
    ]
    daily_groups = daily_sales_payload.get("groups") if isinstance(daily_sales_payload, dict) else []
    daily_groups = daily_groups if isinstance(daily_groups, list) else []
    daily_sales_rows = [
        {
            "label": str(item.get("label") or ""),
            "value": round(float(item.get("total_sales") or 0), 2),
            "order_count": int(item.get("order_count") or 0),
        }
        for item in daily_groups
        if isinstance(item, dict)
    ]
    peak_day = max(daily_sales_rows, key=lambda row: (float(row.get("value") or 0), int(row.get("order_count") or 0)), default=None)
    top_seller_results = top_sellers_payload.get("results") if isinstance(top_sellers_payload, dict) else []
    top_seller_results = top_seller_results if isinstance(top_seller_results, list) else []
    product_revenue_rows = []
    product_unit_rows = []
    product_ranked_items = []
    for item in top_seller_results[:8]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("variant_name") or item.get("product_name") or "Unnamed item")
        units_sold = round(float(item.get("quantity_sold") or 0), 2)
        revenue_amount = round(float(item.get("sales_total") or 0), 2)
        order_count = int(item.get("order_count") or 0)
        product_revenue_rows.append({"label": label, "value": revenue_amount})
        product_unit_rows.append({"label": label, "value": units_sold})
        product_ranked_items.append(
            {
                "label": label,
                "value": revenue_amount,
                "format": "currency",
                "secondary_value": units_sold,
                "secondary_format": "number",
                "image_url": _product_image_url(item),
                "barcode": str(item.get("barcode") or item.get("barcode_snapshot") or ""),
                "detail": f"{order_count} orders" if order_count else "",
            }
        )
    top_location = rows[0]["location"] if rows else "No sales"
    top_product = product_ranked_items[0]["label"] if product_ranked_items else "No product leader"
    leading_location_share = round((rows[0]["sales"] / total_sales) * 100, 1) if rows and total_sales else 0.0
    currency_code = str(payload.get("currency_code") or payload.get("currency") or "NGN").upper()
    currency_symbol = {
        "NGN": "₦",
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
        "JPY": "¥",
        "CAD": "C$",
        "AUD": "A$",
        "GHS": "₵",
        "KES": "KSh",
        "ZAR": "R",
    }.get(currency_code, f"{currency_code} ")
    total_sales_label = f"{currency_symbol}{total_sales:,.2f}"
    summary = (
        f"{total_orders} sales were recorded for {window_label}, totaling {total_sales_label}."
        if total_orders
        else f"No completed sales were recorded for {window_label}."
    )
    explanation = (
        f"{top_location} contributed the most revenue for {window_label} at {leading_location_share}% of sales, and {top_product} led the product mix."
        if total_orders
        else "There is no completed sales activity to analyze for the selected window."
    )
    widgets: list[dict[str, Any]] = [
        {
            "type": "metric_grid",
            "title": f"Sales overview for {window_label}",
            "data": [
                {"label": "Sales Count", "value": total_orders},
                {"label": "Revenue", "value": round(total_sales, 2)},
                {"label": "Avg Basket", "value": average_basket},
                {"label": "Top Location", "value": top_location},
            ],
        },
    ]
    if daily_sales_rows:
        widgets.append(
            {
                "type": "line_chart",
                "title": f"Daily sales trend for {window_label}",
                "subtitle": "Use this to spot the strongest and weakest days across the period.",
                "x_key": "label",
                "y_key": "value",
                "value_format": "currency",
                "data": daily_sales_rows,
            }
        )
    widgets.extend(
        [
            {
                "type": "comparison_table",
                "title": f"Location contribution for {window_label}",
                "columns": ["location", "orders", "sales"],
                "rows": rows,
            },
            {
                "type": "bar_chart",
                "title": f"Sales by location for {window_label}",
                "subtitle": "Use this to compare location revenue at a glance.",
                "x_key": "label",
                "y_key": "value",
                "value_format": "currency",
                "data": sales_chart_rows,
            },
            {
                "type": "donut_chart",
                "title": f"Order share by location for {window_label}",
                "subtitle": "This shows how the order mix was split across locations.",
                "label_key": "label",
                "value_key": "value",
                "value_format": "number",
                "data": order_share_rows,
            },
            {
                "type": "bar_chart",
                "title": f"Top products by sales amount for {window_label}",
                "subtitle": "This compares product revenue contribution inside the selected sales window.",
                "x_key": "label",
                "y_key": "value",
                "value_format": "currency",
                "data": product_revenue_rows,
            },
            {
                "type": "histogram",
                "title": f"Units sold by product for {window_label}",
                "subtitle": "Use this to spot high-volume products even when revenue concentration differs.",
                "x_key": "label",
                "y_key": "value",
                "value_format": "number",
                "data": product_unit_rows,
            },
            {
                "type": "donut_chart",
                "title": f"Product revenue share for {window_label}",
                "subtitle": "This shows which products are carrying the period's revenue mix.",
                "label_key": "label",
                "value_key": "value",
                "value_format": "currency",
                "data": product_revenue_rows,
            },
            {
                "type": "ranked_list",
                "title": f"Top products for {window_label}",
                "items": product_ranked_items,
                "ordered_by": "sales_total",
            },
        ]
    )
    return {
        "kind": "insight_response",
        "summary": summary,
        "explanation": explanation,
        "timeframe": {
            "label": window_label,
            "start_date": str(payload.get("_window_start_date") or ""),
            "end_date": str(payload.get("_window_end_date") or ""),
            "period": str(payload.get("_window_period") or ""),
        },
        "insights": [
            {
                "title": "Location concentration",
                "detail": (
                    f"{top_location} accounted for {leading_location_share}% of revenue in {window_label}."
                    if total_orders and rows
                    else "No location concentration signal is available."
                ),
            },
            {
                "title": "Best trading day",
                "detail": (
                    f"{peak_day['label']} delivered the peak daily revenue at {peak_day['value']} across {peak_day['order_count']} orders."
                    if peak_day
                    else "A day-by-day revenue trend was not available for this window."
                ),
            },
            {
                "title": "Product leader",
                "detail": (
                    f"{top_product} generated the highest product revenue in the selected period."
                    if product_ranked_items
                    else "No product-level sales split was available."
                ),
            },
        ],
        "widgets": widgets,
        "suggested_actions": [],
        "data_sources": [
            {"service": "pos", "endpoint_or_topic": "get_sales_summary", "freshness": "live"},
            {"service": "pos", "endpoint_or_topic": "get_sales_summary(day)", "freshness": "live"},
            {"service": "pos", "endpoint_or_topic": "get_top_sellers", "freshness": "live"},
        ],
        "permissions_checked": ["view_pos_reports"],
        "confidence": "high",
        "warnings": (
            [] if rows else ["No completed sales matched the requested window."]
        ) + ([] if product_revenue_rows else ["No product-level sales breakdown was available for the requested window."]),
    }


def _build_pos_top_sellers_insight(payload: dict[str, Any]) -> dict[str, Any]:
    window_label = _payload_window_label(payload, fallback="last 7 days")
    results = payload.get("results") if isinstance(payload, dict) else []
    results = results if isinstance(results, list) else []
    ranked_items = []
    total_quantity = 0.0
    total_sales = 0.0
    for item in results:
        if not isinstance(item, dict):
            continue
        quantity_sold = float(item.get("quantity_sold") or 0)
        sales_total = float(item.get("sales_total") or 0)
        label = str(item.get("variant_name") or item.get("product_name") or "Unnamed item")
        ranked_items.append(
            {
                "label": label,
                "value": quantity_sold,
                "format": "number",
                "secondary_value": sales_total,
                "secondary_format": "currency",
                "image_url": _product_image_url(item),
                "barcode": str(item.get("barcode_snapshot") or item.get("barcode") or ""),
                "detail": f"{int(item.get('order_count') or 0)} orders" if int(item.get("order_count") or 0) else "",
                "meta": {
                    "product_name": str(item.get("product_name") or ""),
                    "sku": str(item.get("sku_snapshot") or ""),
                    "barcode": str(item.get("barcode_snapshot") or ""),
                    "order_count": int(item.get("order_count") or 0),
                },
            }
        )
        total_quantity += quantity_sold
        total_sales += sales_total
    lead_label = ranked_items[0]["label"] if ranked_items else "No sellers"
    summary = (
        f"{lead_label} is the top seller for {window_label}."
        if ranked_items
        else f"No completed POS sales were recorded for {window_label}."
    )
    explanation = (
        f"{lead_label} leads unit movement for {window_label}, and the ranked list shows whether revenue concentration matches volume concentration."
        if ranked_items
        else "There are no completed sales to rank for the selected period."
    )
    return {
        "kind": "insight_response",
        "summary": summary,
        "explanation": explanation,
        "insights": (
            [
                {
                    "title": "Lead product",
                    "detail": f"{lead_label} is currently the strongest-selling product in {window_label}.",
                },
                {
                    "title": "Volume tracked",
                    "detail": (
                        f"{_format_plain_number(total_quantity)} units contributed "
                        f"{_format_plain_money(total_sales, payload)} in sales across the ranked set."
                    ),
                },
            ]
            if ranked_items
            else []
        ),
        "widgets": [
            {
                "type": "metric_grid",
                "title": f"Sales for {window_label}",
                "data": [
                    {"label": "Units Sold", "value": round(total_quantity, 2)},
                    {"label": "Sales", "value": round(total_sales, 2)},
                    {"label": "Items Ranked", "value": len(ranked_items)},
                ],
            },
            {
                "type": "ranked_list",
                "title": f"Top sellers for {window_label}",
                "items": ranked_items,
                "ordered_by": "quantity_sold",
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "pos", "endpoint_or_topic": "get_top_sellers", "freshness": "live"}],
        "permissions_checked": ["view_pos_reports"],
        "confidence": "high",
        "warnings": [] if ranked_items else ["No completed sales matched the requested window."],
    }


def _build_pos_product_sales_trend_insight(payload: dict[str, Any], *, variant_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    window_label = _payload_window_label(payload, fallback="the selected period")
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    trend = payload.get("trend") if isinstance(payload.get("trend"), list) else []
    locations = payload.get("locations") if isinstance(payload.get("locations"), list) else []
    products = payload.get("products") if isinstance(payload.get("products"), list) else []
    recent_orders = payload.get("recent_orders") if isinstance(payload.get("recent_orders"), list) else []
    first_product = products[0] if products and isinstance(products[0], dict) else {}
    variant_results = variant_payload.get("results") if isinstance(variant_payload, dict) else []
    variant_match = variant_results[0] if isinstance(variant_results, list) and variant_results and isinstance(variant_results[0], dict) else {}
    product_name = str(
        first_product.get("variant_name")
        or first_product.get("product_name")
        or variant_match.get("name")
        or variant_match.get("product_name")
        or payload.get("query")
        or "Product"
    )
    product_context = {
        "variant_name": product_name,
        "product_name": str(first_product.get("product_name") or variant_match.get("product_name") or product_name),
        "barcode": str(first_product.get("barcode_snapshot") or variant_match.get("barcode") or ""),
        "sku": str(first_product.get("sku_snapshot") or variant_match.get("sku") or ""),
        "image_url": str(variant_match.get("image_url") or ""),
    }
    trend_rows = [
        {
            "label": str(item.get("label") or ""),
            "sales_total": round(float(item.get("sales_total") or 0), 2),
            "quantity_sold": round(float(item.get("quantity_sold") or 0), 2),
            "order_count": int(item.get("order_count") or 0),
        }
        for item in trend
        if isinstance(item, dict)
    ]
    location_rows = [
        {
            "location": str(item.get("location") or "Unassigned location"),
            "sales_total": round(float(item.get("sales_total") or 0), 2),
            "quantity_sold": round(float(item.get("quantity_sold") or 0), 2),
            "order_count": int(item.get("order_count") or 0),
        }
        for item in locations
        if isinstance(item, dict)
    ]
    recent_rows = [
        {
            "completed_at": str(item.get("completed_at") or ""),
            "location": str(item.get("location") or ""),
            "terminal": str(item.get("terminal_name") or ""),
            "quantity": round(float(item.get("quantity") or 0), 2),
            "unit_price": round(float(item.get("unit_price") or 0), 2),
            "line_total": round(float(item.get("line_total") or 0), 2),
        }
        for item in recent_orders[:10]
        if isinstance(item, dict)
    ]
    sales_total = round(float(totals.get("sales_total") or 0), 2)
    quantity_sold = round(float(totals.get("quantity_sold") or 0), 2)
    order_count = int(totals.get("order_count") or 0)
    average_unit_price = round(float(totals.get("average_unit_price") or 0), 2)
    peak_bucket = max(trend_rows, key=lambda row: (row["sales_total"], row["quantity_sold"]), default=None)
    top_location = location_rows[0] if location_rows else None
    summary = (
        f"{product_name} generated {sales_total} across {quantity_sold} units in {window_label}."
        if order_count
        else f"No completed sales were found for {product_name} in {window_label}."
    )
    explanation = (
        f"{top_location['location']} led location contribution, and {peak_bucket['label']} was the strongest trend bucket."
        if top_location and peak_bucket
        else "The product has insufficient completed POS history for deeper trend commentary in this window."
    )
    return {
        "kind": "insight_response",
        "summary": summary,
        "explanation": explanation,
        "timeframe": {
            "label": window_label,
            "start_date": str(payload.get("_window_start_date") or ""),
            "end_date": str(payload.get("_window_end_date") or ""),
            "period": str(payload.get("_window_period") or ""),
        },
        "insights": [
            {
                "title": "Peak period",
                "detail": (
                    f"{peak_bucket['label']} led with {peak_bucket['sales_total']} in sales and {peak_bucket['quantity_sold']} units."
                    if peak_bucket
                    else "No sales bucket was available for this product."
                ),
            },
            {
                "title": "Location leader",
                "detail": (
                    f"{top_location['location']} contributed {top_location['sales_total']} across {top_location['quantity_sold']} units."
                    if top_location
                    else "No location split was available for this product."
                ),
            },
        ],
        "widgets": [
            {
                "type": "entity_preview",
                "title": "Product analyzed",
                "entity": {
                    "kind": "Variant",
                    "title": product_name,
                    "subtitle": str(product_context.get("product_name") or ""),
                    "image_url": _product_image_url(product_context),
                    "meta": [
                        {"label": "Barcode", "value": str(product_context.get("barcode") or "")},
                        {"label": "SKU", "value": str(product_context.get("sku") or "")},
                    ],
                },
            },
            {
                "type": "metric_grid",
                "title": f"Product sales snapshot for {window_label}",
                "data": [
                    {"label": "Revenue", "value": sales_total, "format": "currency"},
                    {"label": "Units Sold", "value": quantity_sold, "format": "number"},
                    {"label": "Orders", "value": order_count, "format": "number"},
                    {"label": "Avg Unit Price", "value": average_unit_price, "format": "currency"},
                ],
            },
            {
                "type": "line_chart",
                "title": f"Sales trend for {product_name} in {window_label}",
                "subtitle": "Revenue trend across the selected period.",
                "x_key": "label",
                "y_key": "sales_total",
                "value_format": "currency",
                "data": trend_rows,
            },
            {
                "type": "bar_chart",
                "title": f"Units sold trend for {product_name} in {window_label}",
                "x_key": "label",
                "y_key": "quantity_sold",
                "value_format": "number",
                "data": trend_rows,
            },
            {
                "type": "bar_chart",
                "title": f"Location contribution for {product_name}",
                "x_key": "location",
                "y_key": "sales_total",
                "value_format": "currency",
                "data": location_rows,
            },
            {
                "type": "comparison_table",
                "title": f"Recent sales for {product_name}",
                "columns": ["completed_at", "location", "terminal", "quantity", "unit_price", "line_total"],
                "rows": recent_rows,
            },
        ],
        "suggested_actions": [],
        "data_sources": [
            {"service": "pos", "endpoint_or_topic": "get_product_sales_trend", "freshness": "live"},
            {"service": "products", "endpoint_or_topic": "get_variant_lookup", "freshness": "live"},
        ],
        "permissions_checked": ["view_pos_reports", "read_products"],
        "confidence": "high" if order_count else "medium",
        "warnings": [] if order_count else ["No completed sales matched this product query and date range."],
    }


def _build_pos_product_comparison_insight(payloads: list[dict[str, Any]], *, window: dict[str, Any]) -> dict[str, Any]:
    window_label = str(window.get("label") or "the selected period")
    first_payload = payloads[0] if payloads else {}
    currency_code = str(first_payload.get("currency_code") or first_payload.get("currency") or "NGN").upper()
    currency_symbol = {
        "NGN": "₦",
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
        "JPY": "¥",
        "CAD": "C$",
        "AUD": "A$",
        "GHS": "₵",
        "KES": "KSh",
        "ZAR": "R",
    }.get(currency_code, f"{currency_code} ")
    rows: list[dict[str, Any]] = []
    all_buckets: set[str] = set()
    series: list[dict[str, str]] = []
    revenue_by_bucket: dict[str, dict[str, Any]] = {}
    units_by_bucket: dict[str, dict[str, Any]] = {}
    orders_by_bucket: dict[str, dict[str, Any]] = {}

    def _money_label(amount: Any) -> str:
        return f"{currency_symbol}{float(amount or 0):,.2f}"

    def _count_label(value: Any) -> str:
        numeric = float(value or 0)
        return f"{numeric:,.0f}" if numeric.is_integer() else f"{numeric:,.2f}"

    def _series_key(value: str, index: int) -> str:
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
        return f"product_{normalized or index}"

    for payload_index, payload in enumerate(payloads, start=1):
        totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
        products = payload.get("products") if isinstance(payload.get("products"), list) else []
        first_product = products[0] if products and isinstance(products[0], dict) else {}
        product_name = str(
            first_product.get("variant_name")
            or first_product.get("product_name")
            or payload.get("query")
            or f"Product {payload_index}"
        )
        key = _series_key(product_name, payload_index)
        series.append({"key": key, "label": product_name})
        sales_total = round(float(totals.get("sales_total") or 0), 2)
        quantity_sold = round(float(totals.get("quantity_sold") or 0), 2)
        order_count = int(totals.get("order_count") or 0)
        rows.append(
            {
                "product": product_name,
                "barcode": str(first_product.get("barcode_snapshot") or ""),
                "sales_total": sales_total,
                "quantity_sold": quantity_sold,
                "order_count": order_count,
                "avg_unit_revenue": round(sales_total / quantity_sold, 2) if quantity_sold else 0,
                "image_url": _product_image_url(
                    {
                        "variant_name": product_name,
                        "product_name": str(first_product.get("product_name") or product_name),
                        "barcode": str(first_product.get("barcode_snapshot") or ""),
                        "sku": str(first_product.get("sku_snapshot") or ""),
                    }
                ),
            }
        )
        trend_rows = payload.get("trend") if isinstance(payload.get("trend"), list) else []
        for trend_item in trend_rows:
            if not isinstance(trend_item, dict):
                continue
            bucket = str(trend_item.get("label") or "")
            if not bucket:
                continue
            all_buckets.add(bucket)
            revenue_by_bucket.setdefault(bucket, {"label": bucket})[key] = round(float(trend_item.get("sales_total") or 0), 2)
            units_by_bucket.setdefault(bucket, {"label": bucket})[key] = round(float(trend_item.get("quantity_sold") or 0), 2)
            orders_by_bucket.setdefault(bucket, {"label": bucket})[key] = int(trend_item.get("order_count") or 0)

    for bucket in all_buckets:
        for series_item in series:
            revenue_by_bucket.setdefault(bucket, {"label": bucket}).setdefault(series_item["key"], 0)
            units_by_bucket.setdefault(bucket, {"label": bucket}).setdefault(series_item["key"], 0)
            orders_by_bucket.setdefault(bucket, {"label": bucket}).setdefault(series_item["key"], 0)

    rows.sort(key=lambda row: (float(row["sales_total"]), float(row["quantity_sold"])), reverse=True)
    total_sales = round(sum(float(row["sales_total"]) for row in rows), 2)
    total_units = round(sum(float(row["quantity_sold"]) for row in rows), 2)
    total_orders = sum(int(row["order_count"]) for row in rows)
    leader = rows[0] if rows else None
    ranked_items = [
        {
            "label": row["product"],
            "value": row["sales_total"],
            "format": "currency",
            "secondary_value": row["quantity_sold"],
            "secondary_format": "number",
            "detail": f"{row['order_count']} orders",
            "barcode": row["barcode"],
            "image_url": row["image_url"],
        }
        for row in rows[:10]
    ]
    table_rows = [
        {
            "product": row["product"],
            "barcode": row["barcode"],
            "sales_total": row["sales_total"],
            "quantity_sold": row["quantity_sold"],
            "order_count": row["order_count"],
            "avg_unit_revenue": row["avg_unit_revenue"],
        }
        for row in rows[:15]
    ]
    return {
        "kind": "insight_response",
        "summary": (
            f"{len(rows)} products were compared for {window_label}; {leader['product']} leads revenue."
            if leader
            else f"No product sales were found for {window_label}."
        ),
        "explanation": (
            f"The compared products generated {_money_label(total_sales)} across {_count_label(total_units)} units and {_count_label(total_orders)} orders. Use the trend lines to see when each product gained or lost momentum."
            if rows
            else "There is not enough completed POS history to compare these products."
        ),
        "timeframe": {
            "label": window_label,
            "start_date": str(window.get("start_date") or ""),
            "end_date": str(window.get("end_date") or ""),
            "period": str(window.get("period") or ""),
        },
        "insights": [
            {
                "title": "Revenue leader",
                "detail": (
                    f"{leader['product']} is ahead with {_money_label(leader['sales_total'])} and {_count_label(leader['quantity_sold'])} units."
                    if leader
                    else "No leading product could be identified."
                ),
            },
            {
                "title": "Trend view",
                "detail": "Revenue, units sold, and order count are shown separately so high-price products do not hide volume movement.",
            },
        ],
        "widgets": [
            {
                "type": "metric_grid",
                "title": f"Product comparison snapshot for {window_label}",
                "data": [
                    {"label": "Products Compared", "value": len(rows), "format": "number"},
                    {"label": "Revenue", "value": total_sales, "format": "currency"},
                    {"label": "Units Sold", "value": total_units, "format": "number"},
                    {"label": "Orders", "value": total_orders, "format": "number"},
                ],
            },
            {
                "type": "ranked_list",
                "title": f"Product revenue ranking for {window_label}",
                "items": ranked_items,
                "ordered_by": "sales_total",
            },
            {
                "type": "line_chart",
                "title": f"Product revenue trend for {window_label}",
                "subtitle": "Each line tracks one product across the selected period.",
                "x_key": "label",
                "series": series,
                "value_format": "currency",
                "data": [revenue_by_bucket[key] for key in sorted(revenue_by_bucket)],
            },
            {
                "type": "line_chart",
                "title": f"Product units trend for {window_label}",
                "subtitle": "Use this to compare quantity movement independent of price.",
                "x_key": "label",
                "series": series,
                "value_format": "number",
                "data": [units_by_bucket[key] for key in sorted(units_by_bucket)],
            },
            {
                "type": "line_chart",
                "title": f"Product order-count trend for {window_label}",
                "subtitle": "This shows purchase frequency for each product.",
                "x_key": "label",
                "series": series,
                "value_format": "number",
                "data": [orders_by_bucket[key] for key in sorted(orders_by_bucket)],
            },
            {
                "type": "bar_chart",
                "title": f"Product revenue comparison for {window_label}",
                "x_key": "product",
                "y_key": "sales_total",
                "value_format": "currency",
                "data": rows,
            },
            {
                "type": "comparison_table",
                "title": f"Product comparison table for {window_label}",
                "columns": ["product", "barcode", "sales_total", "quantity_sold", "order_count", "avg_unit_revenue"],
                "rows": table_rows,
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "pos", "endpoint_or_topic": "get_product_sales_trend", "freshness": "live"}],
        "permissions_checked": ["view_pos_reports"],
        "confidence": "high" if rows else "medium",
        "warnings": [] if rows else ["No completed sales matched these product queries and date range."],
    }


def _build_pos_variant_comparison_insight(payload: dict[str, Any], *, variant_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    window_label = _payload_window_label(payload, fallback="the selected period")
    products = payload.get("products") if isinstance(payload.get("products"), list) else []
    variant_results = variant_payload.get("results") if isinstance(variant_payload, dict) else []
    variant_results = variant_results if isinstance(variant_results, list) else []
    lookup_by_code: dict[str, dict[str, Any]] = {}
    for item in variant_results:
        if not isinstance(item, dict):
            continue
        for key in (str(item.get("barcode") or ""), str(item.get("sku") or "")):
            if key:
                lookup_by_code[key] = item
    rows = []
    for item in products:
        if not isinstance(item, dict):
            continue
        barcode = str(item.get("barcode_snapshot") or "")
        sku = str(item.get("sku_snapshot") or "")
        label = str(item.get("variant_name") or item.get("product_name") or barcode or sku or "Variant")
        match = lookup_by_code.get(barcode) or lookup_by_code.get(sku) or {}
        sales_total = round(float(item.get("sales_total") or 0), 2)
        quantity_sold = round(float(item.get("quantity_sold") or 0), 2)
        order_count = int(item.get("order_count") or 0)
        rows.append(
            {
                "variant": label,
                "product": str(item.get("product_name") or match.get("product_name") or ""),
                "sku": sku or str(match.get("sku") or ""),
                "barcode": barcode or str(match.get("barcode") or ""),
                "sales_total": sales_total,
                "quantity_sold": quantity_sold,
                "order_count": order_count,
                "avg_unit_revenue": round(sales_total / quantity_sold, 2) if quantity_sold else 0,
                "image_url": _product_image_url(
                    {
                        "variant_name": label,
                        "product_name": str(item.get("product_name") or match.get("product_name") or ""),
                        "barcode": barcode or str(match.get("barcode") or ""),
                        "sku": sku or str(match.get("sku") or ""),
                        "image_url": str(match.get("image_url") or ""),
                    }
                ),
            }
        )
    rows.sort(key=lambda row: (float(row["sales_total"]), float(row["quantity_sold"])), reverse=True)
    total_sales = round(sum(float(row["sales_total"]) for row in rows), 2)
    total_units = round(sum(float(row["quantity_sold"]) for row in rows), 2)
    total_orders = sum(int(row["order_count"]) for row in rows)
    leader = rows[0] if rows else None
    currency_code = str(payload.get("currency_code") or payload.get("currency") or "NGN").upper()
    currency_symbol = {
        "NGN": "₦",
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
        "JPY": "¥",
        "CAD": "C$",
        "AUD": "A$",
        "GHS": "₵",
        "KES": "KSh",
        "ZAR": "R",
    }.get(currency_code, f"{currency_code} ")
    money_label = lambda amount: f"{currency_symbol}{float(amount or 0):,.2f}"
    count_label = lambda value: f"{float(value or 0):,.0f}" if float(value or 0).is_integer() else f"{float(value or 0):,.2f}"
    label_by_identity: dict[str, str] = {}
    for row in rows:
        identity_parts = [
            str(row.get("barcode") or ""),
            str(row.get("sku") or ""),
            str(row.get("variant") or ""),
            str(row.get("product") or ""),
        ]
        identity_key = next((part for part in identity_parts if part), "")
        if identity_key:
            label_by_identity[identity_key] = str(row.get("variant") or row.get("product") or identity_key)

    def _series_identity(item: dict[str, Any]) -> str:
        return (
            str(item.get("barcode_snapshot") or "")
            or str(item.get("sku_snapshot") or "")
            or str(item.get("variant_name") or "")
            or str(item.get("product_name") or "")
            or "Series"
        )

    def _series_label(item: dict[str, Any]) -> str:
        identity = _series_identity(item)
        return (
            label_by_identity.get(identity)
            or str(item.get("variant_name") or item.get("product_name") or item.get("barcode_snapshot") or item.get("sku_snapshot") or identity)
        )

    def _series_key(identity: str, index: int) -> str:
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", identity).strip("_").lower()
        return f"series_{normalized or index}"

    def _build_series_chart(metric_key: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        trend_rows = payload.get("series_trend") if isinstance(payload.get("series_trend"), list) else []
        series_lookup: dict[str, dict[str, str]] = {}
        bucket_lookup: dict[str, dict[str, Any]] = {}
        for item in trend_rows:
            if not isinstance(item, dict):
                continue
            bucket = str(item.get("label") or "")
            identity = _series_identity(item)
            if not bucket or not identity:
                continue
            if identity not in series_lookup:
                series_lookup[identity] = {
                    "key": _series_key(identity, len(series_lookup) + 1),
                    "label": _series_label(item),
                }
            bucket_row = bucket_lookup.setdefault(bucket, {"label": bucket})
            bucket_row[series_lookup[identity]["key"]] = round(float(item.get(metric_key) or 0), 2)
        series = list(series_lookup.values())
        data = [bucket_lookup[key] for key in sorted(bucket_lookup)]
        for bucket_row in data:
            for series_item in series:
                bucket_row.setdefault(series_item["key"], 0)
        return data, series

    revenue_trend_data, trend_series = _build_series_chart("sales_total")
    units_trend_data, _ = _build_series_chart("quantity_sold")
    orders_trend_data, _ = _build_series_chart("order_count")
    ranked_items = [
        {
            "label": row["variant"],
            "value": row["sales_total"],
            "format": "currency",
            "secondary_value": row["quantity_sold"],
            "secondary_format": "number",
            "detail": f"{row['order_count']} orders",
            "barcode": row["barcode"],
            "image_url": row["image_url"],
            "meta": {"sku": row["sku"], "barcode": row["barcode"]},
        }
        for row in rows[:10]
    ]
    table_rows = [
        {
            "variant": row["variant"],
            "barcode": row["barcode"],
            "sales_total": row["sales_total"],
            "quantity_sold": row["quantity_sold"],
            "order_count": row["order_count"],
            "avg_unit_revenue": row["avg_unit_revenue"],
        }
        for row in rows[:15]
    ]
    summary = (
        f"{len(rows)} variants were compared for {window_label}; {leader['variant']} leads revenue."
        if leader
        else f"No variant sales were found for {window_label}."
    )
    explanation = (
        f"The leading variant generated {money_label(leader['sales_total'])} from {count_label(leader['quantity_sold'])} units, so compare both revenue and units before deciding what to restock."
        if leader
        else "There is not enough completed POS history to compare variants for this product."
    )
    return {
        "kind": "insight_response",
        "summary": summary,
        "explanation": explanation,
        "timeframe": {
            "label": window_label,
            "start_date": str(payload.get("_window_start_date") or ""),
            "end_date": str(payload.get("_window_end_date") or ""),
            "period": str(payload.get("_window_period") or ""),
        },
        "insights": [
            {
                "title": "Variant leader",
                "detail": (
                    f"{leader['variant']} is ahead with {money_label(leader['sales_total'])} revenue and {count_label(leader['quantity_sold'])} units."
                    if leader
                    else "No leading variant could be identified."
                ),
            },
            {
                "title": "Revenue spread",
                "detail": (
                    f"The compared variants generated {money_label(total_sales)} total revenue across {count_label(total_units)} units and {count_label(total_orders)} orders."
                    if rows
                    else "No revenue spread is available for this query."
                ),
            },
        ],
        "widgets": [
            {
                "type": "metric_grid",
                "title": f"Variant comparison snapshot for {window_label}",
                "data": [
                    {"label": "Variants Compared", "value": len(rows), "format": "number"},
                    {"label": "Revenue", "value": total_sales, "format": "currency"},
                    {"label": "Units Sold", "value": total_units, "format": "number"},
                    {"label": "Orders", "value": total_orders, "format": "number"},
                ],
            },
            {
                "type": "ranked_list",
                "title": f"Variant revenue ranking for {window_label}",
                "items": ranked_items,
                "ordered_by": "sales_total",
            },
            {
                "type": "line_chart",
                "title": f"Variant revenue trend for {window_label}",
                "subtitle": "Each line tracks one variant across the selected period.",
                "x_key": "label",
                "series": trend_series,
                "value_format": "currency",
                "data": revenue_trend_data,
            },
            {
                "type": "line_chart",
                "title": f"Variant units trend for {window_label}",
                "subtitle": "Use this to see which variant gained or lost unit momentum.",
                "x_key": "label",
                "series": trend_series,
                "value_format": "number",
                "data": units_trend_data,
            },
            {
                "type": "line_chart",
                "title": f"Variant order-count trend for {window_label}",
                "subtitle": "This separates order frequency from quantity and revenue.",
                "x_key": "label",
                "series": trend_series,
                "value_format": "number",
                "data": orders_trend_data,
            },
            {
                "type": "bar_chart",
                "title": f"Variant revenue comparison for {window_label}",
                "x_key": "variant",
                "y_key": "sales_total",
                "value_format": "currency",
                "data": rows,
            },
            {
                "type": "bar_chart",
                "title": f"Variant units comparison for {window_label}",
                "x_key": "variant",
                "y_key": "quantity_sold",
                "value_format": "number",
                "data": rows,
            },
            {
                "type": "comparison_table",
                "title": f"Variant comparison table for {window_label}",
                "columns": ["variant", "barcode", "sales_total", "quantity_sold", "order_count", "avg_unit_revenue"],
                "rows": table_rows,
            },
        ],
        "suggested_actions": [],
        "data_sources": [
            {"service": "pos", "endpoint_or_topic": "get_product_sales_trend", "freshness": "live"},
            {"service": "products", "endpoint_or_topic": "get_variant_lookup", "freshness": "live"},
        ],
        "permissions_checked": ["view_pos_reports", "read_products"],
        "confidence": "high" if rows else "medium",
        "warnings": [] if rows else ["No completed sales matched this product family and date range."],
    }


def _build_pos_best_sales_day_insight(payload: dict[str, Any]) -> dict[str, Any]:
    window_label = _payload_window_label(payload, fallback="the available sales history")
    groups = payload.get("groups") if isinstance(payload, dict) else []
    groups = groups if isinstance(groups, list) else []
    daily_rows = [
        {
            "day": str(item.get("label") or ""),
            "sales": round(float(item.get("total_sales") or 0), 2),
            "orders": int(item.get("order_count") or 0),
        }
        for item in groups
        if isinstance(item, dict)
    ]
    peak_row = max(daily_rows, key=lambda row: (row["sales"], row["orders"]), default=None)
    summary = (
        f"{peak_row['day']} was the strongest sales day in {window_label}."
        if peak_row
        else f"No completed sales days were found for {window_label}."
    )
    explanation = (
        f"The peak day reached {peak_row['sales']} across {peak_row['orders']} completed orders, and the trend below shows how far other days trailed it."
        if peak_row
        else "There is no day-level POS history to analyze for the selected period."
    )
    ranked_days = [
        {
            "label": row["day"],
            "value": row["sales"],
            "format": "currency",
            "secondary_value": row["orders"],
            "secondary_format": "number",
            "detail": "orders",
        }
        for row in sorted(daily_rows, key=lambda row: (row["sales"], row["orders"]), reverse=True)[:5]
    ]
    return {
        "kind": "insight_response",
        "summary": summary,
        "explanation": explanation,
        "insights": (
            [
                {
                    "title": "Peak day",
                    "detail": f"{peak_row['day']} produced {peak_row['sales']} across {peak_row['orders']} orders.",
                }
            ]
            if peak_row
            else []
        ),
        "widgets": [
            {
                "type": "metric_grid",
                "title": f"Best sales day in {window_label}",
                "data": [
                    {"label": "Peak Revenue", "value": peak_row["sales"] if peak_row else 0},
                    {"label": "Peak Orders", "value": peak_row["orders"] if peak_row else 0},
                    {"label": "Tracked Days", "value": len(daily_rows)},
                ],
            },
            {
                "type": "line_chart",
                "title": f"Daily revenue trend for {window_label}",
                "subtitle": "This shows how daily revenue moved across the selected history.",
                "x_key": "day",
                "y_key": "sales",
                "value_format": "currency",
                "data": daily_rows,
            },
            {
                "type": "ranked_list",
                "title": f"Best sales days for {window_label}",
                "items": ranked_days,
                "ordered_by": "sales",
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "pos", "endpoint_or_topic": "get_sales_summary(day)", "freshness": "live"}],
        "permissions_checked": ["view_pos_reports"],
        "confidence": "high",
        "warnings": [] if peak_row else ["No completed sales matched the requested window."],
    }


async def _enrich_top_seller_results_with_variant_context(
    *,
    results_payload: dict[str, Any],
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
    limit: int = 8,
) -> dict[str, Any]:
    results = results_payload.get("results")
    if not isinstance(results, list) or not results:
        return results_payload

    def _lookup_queries_for_item(item: dict[str, Any]) -> list[str]:
        ordered: list[str] = []

        def _push(query: str) -> None:
            value = str(query or "").strip()
            if value and value not in ordered:
                ordered.append(value)

        identifier = str(item.get("barcode_snapshot") or "").strip() or str(item.get("sku_snapshot") or "").strip()
        display_name = str(item.get("variant_name") or "").strip() or str(item.get("product_name") or "").strip()

        _push(display_name)
        _push(identifier)
        return ordered[:2]

    async def _lookup_variant(query: str) -> dict[str, Any] | None:
        try:
            lookup = await tool_executor.call_tool(
                name="product.get_variant_lookup",
                arguments={"query": query, "limit": 1, "active_only": True},
                ctx=tool_ctx,
            )
        except Exception:
            return None
        lookup_results = lookup.get("results") if isinstance(lookup, dict) else None
        first_match = lookup_results[0] if isinstance(lookup_results, list) and lookup_results else None
        return first_match if isinstance(first_match, dict) else None

    seed_items = [item for item in results[:limit] if isinstance(item, dict)]
    lookup_tasks: dict[str, asyncio.Task[dict[str, Any] | None]] = {}

    def _lookup_cache_key(query: str) -> str:
        return re.sub(r"\s+", " ", str(query or "").strip().lower())

    async def _lookup_cached(query: str) -> dict[str, Any] | None:
        cache_key = _lookup_cache_key(query)
        if not cache_key:
            return None
        task = lookup_tasks.get(cache_key)
        if task is None:
            task = asyncio.create_task(_lookup_variant(query))
            lookup_tasks[cache_key] = task
        return await task

    async def _enrich_item(raw_item: dict[str, Any]) -> dict[str, Any]:
        item = dict(raw_item)
        for query in _lookup_queries_for_item(item):
            first_match = await _lookup_cached(query)
            if not isinstance(first_match, dict):
                continue
            item["image_url"] = str(first_match.get("image_url") or item.get("image_url") or "")
            item["barcode"] = str(first_match.get("barcode") or item.get("barcode_snapshot") or item.get("barcode") or "")
            item["sku"] = str(first_match.get("sku") or item.get("sku_snapshot") or item.get("sku") or "")
            if item.get("image_url") or item.get("barcode") or item.get("sku"):
                break
        return item

    enriched_results = await asyncio.gather(*[_enrich_item(raw_item) for raw_item in seed_items])

    if len(results) > limit:
        enriched_results.extend(item for item in results[limit:] if isinstance(item, dict))

    return {**results_payload, "results": enriched_results}


def _build_pos_payment_mix_insight(payload: dict[str, Any]) -> dict[str, Any]:
    methods = payload.get("payment_methods") if isinstance(payload, dict) else []
    methods = methods if isinstance(methods, list) else []
    total_sales = float(payload.get("total_sales") or 0) if isinstance(payload, dict) else 0.0
    total_orders = int(payload.get("total_orders") or 0) if isinstance(payload, dict) else 0
    chart_rows = []
    for item in methods:
        if not isinstance(item, dict):
            continue
        total = float(item.get("total") or 0)
        count = int(item.get("count") or 0)
        chart_rows.append(
            {
                "label": str(item.get("payment_method") or "unknown").replace("_", " ").title(),
                "value": total,
                "count": count,
                "share": round((total / total_sales) * 100, 2) if total_sales > 0 else 0,
            }
        )
    lead_label = chart_rows[0]["label"] if chart_rows else "No payment data"
    return {
        "kind": "insight_response",
        "summary": f"{lead_label} leads the current payment mix." if chart_rows else "No payment activity was recorded.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": "Payment mix snapshot",
                "data": [
                    {"label": "Sales", "value": round(total_sales, 2)},
                    {"label": "Orders", "value": total_orders},
                    {"label": "Methods", "value": len(chart_rows)},
                    {"label": "Held Orders", "value": int(payload.get("held_orders") or 0)},
                ],
            },
            {
                "type": "donut_chart",
                "title": "Sales by payment method",
                "data": chart_rows,
                "label_key": "label",
                "value_key": "value",
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "pos", "endpoint_or_topic": "get_pos_daily_summary", "freshness": "live"}],
        "permissions_checked": ["view_pos_reports"],
        "confidence": "high",
        "warnings": [] if chart_rows else ["No payment records matched the requested view."],
    }


def _build_pos_terminal_activity_insight(payload: dict[str, Any]) -> dict[str, Any]:
    window_label = _payload_window_label(payload, fallback="last 7 days")
    results = payload.get("results") if isinstance(payload, dict) else []
    results = results if isinstance(results, list) else []
    rows = []
    ranked_items = []
    total_sales = 0.0
    total_orders = 0
    for item in results:
        if not isinstance(item, dict):
            continue
        sales = float(item.get("total_sales") or 0)
        orders = int(item.get("order_count") or 0)
        label = str(item.get("terminal_name") or "Unassigned terminal")
        row = {
            "terminal": label,
            "location": str(item.get("location") or ""),
            "orders": orders,
            "completed_orders": int(item.get("completed_orders") or 0),
            "sales": round(sales, 2),
            "avg_basket": round(sales / max(orders, 1), 2),
        }
        rows.append(row)
        ranked_items.append({"label": label, "value": sales, "secondary_value": orders})
        total_sales += sales
        total_orders += orders
    return {
        "kind": "insight_response",
        "summary": (
            f"{rows[0]['terminal']} is leading terminal activity for {window_label}."
            if rows
            else f"No terminal activity matched {window_label}."
        ),
        "widgets": [
            {
                "type": "comparison_table",
                "title": f"Terminal and cashier activity for {window_label}",
                "columns": ["terminal", "location", "orders", "completed_orders", "sales", "avg_basket"],
                "rows": rows,
            },
            {
                "type": "ranked_list",
                "title": "Highest sales contribution",
                "items": ranked_items,
                "ordered_by": "sales",
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "pos", "endpoint_or_topic": "get_terminal_activity", "freshness": "live"}],
        "permissions_checked": ["view_pos_reports"],
        "confidence": "medium",
        "warnings": [] if rows else ["No terminal activity matched the requested window."],
    }


def _build_pos_sessions_orders_insight(summary_payload: dict[str, Any], sales_payload: dict[str, Any]) -> dict[str, Any]:
    total_sales = float(summary_payload.get("total_sales") or 0)
    total_orders = int(summary_payload.get("total_orders") or 0)
    groups = sales_payload.get("groups") if isinstance(sales_payload, dict) else []
    groups = groups if isinstance(groups, list) else []
    chart_rows = [
        {
            "label": str(item.get("label") or "Unassigned"),
            "value": float(item.get("total_sales") or 0),
            "orders": int(item.get("order_count") or 0),
        }
        for item in groups
        if isinstance(item, dict)
    ]
    return {
        "kind": "insight_response",
        "summary": "POS session and order flow is ready." if total_orders else "No POS session or order activity was recorded.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": "Session and order flow",
                "data": [
                    {"label": "Orders", "value": total_orders},
                    {"label": "Sales", "value": round(total_sales, 2)},
                    {"label": "Open Sessions", "value": int(summary_payload.get("open_sessions") or 0)},
                    {"label": "Held Orders", "value": int(summary_payload.get("held_orders") or 0)},
                ],
            },
            {
                "type": "line_chart",
                "title": "Order flow by location",
                "data": chart_rows,
                "x_key": "label",
                "y_key": "orders",
            },
        ],
        "suggested_actions": [],
        "data_sources": [
            {"service": "pos", "endpoint_or_topic": "get_pos_daily_summary", "freshness": "live"},
            {"service": "pos", "endpoint_or_topic": "get_sales_summary", "freshness": "live"},
        ],
        "permissions_checked": ["view_pos_reports"],
        "confidence": "medium",
        "warnings": [] if total_orders or chart_rows else ["No POS order flow matched the requested view."],
    }


def _build_pos_exceptions_insight(summary_payload: dict[str, Any], terminal_payload: dict[str, Any]) -> dict[str, Any]:
    methods = summary_payload.get("payment_methods") if isinstance(summary_payload, dict) else []
    methods = methods if isinstance(methods, list) else []
    terminals = terminal_payload.get("results") if isinstance(terminal_payload, dict) else []
    terminals = terminals if isinstance(terminals, list) else []
    risk_items = []
    if int(summary_payload.get("held_orders") or 0) > 0:
        risk_items.append(
            {
                "label": "Held orders",
                "severity": "medium",
                "description": f"{int(summary_payload.get('held_orders') or 0)} orders still held.",
            }
        )
    if int(summary_payload.get("open_sessions") or 0) > 3:
        risk_items.append(
            {
                "label": "Open sessions",
                "severity": "medium",
                "description": f"{int(summary_payload.get('open_sessions') or 0)} sessions remain open.",
            }
        )
    for item in methods:
        if not isinstance(item, dict):
            continue
        if str(item.get("payment_method") or "").lower() in {"unknown", ""} and float(item.get("total") or 0) > 0:
            risk_items.append(
                {
                    "label": "Unknown payment mapping",
                    "severity": "high",
                    "description": "Some completed sales are attached to an unknown payment method.",
                }
            )
            break
    ranked_items = []
    for item in terminals[:5]:
        if not isinstance(item, dict):
            continue
        ranked_items.append(
            {
                "label": str(item.get("terminal_name") or "Unassigned terminal"),
                "value": int(item.get("order_count") or 0),
                "secondary_value": float(item.get("total_sales") or 0),
            }
        )
    if not risk_items:
        risk_items.append({"label": "Current POS posture", "severity": "low", "description": "No obvious risk spikes in the current snapshot."})
    return {
        "kind": "insight_response",
        "summary": "POS operational risks are ready.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": "Risk snapshot",
                "data": [
                    {"label": "Open Sessions", "value": int(summary_payload.get("open_sessions") or 0)},
                    {"label": "Held Orders", "value": int(summary_payload.get("held_orders") or 0)},
                    {"label": "Payment Methods", "value": len(methods)},
                    {"label": "Active Terminals", "value": len(terminals)},
                ],
            },
            {
                "type": "risk_panel",
                "title": "POS issues to investigate",
                "items": risk_items,
            },
            {
                "type": "ranked_list",
                "title": "Most active terminals",
                "items": ranked_items,
                "ordered_by": "order_count",
            },
        ],
        "suggested_actions": [],
        "data_sources": [
            {"service": "pos", "endpoint_or_topic": "get_pos_daily_summary", "freshness": "live"},
            {"service": "pos", "endpoint_or_topic": "get_terminal_activity", "freshness": "live"},
        ],
        "permissions_checked": ["view_pos_reports"],
        "confidence": "medium",
        "warnings": [],
    }


def _users_named_insight_from_text(text: str) -> str | None:
    normalized = _normalize_user_text(text)
    if not normalized:
        return None
    if any(
        token in normalized
        for token in (
            "staff activity",
            "staff activity from audit events",
            "staff were most active today",
            "timeline of recent staff activity",
            "changed the most operational records",
            "staff activity by role",
            "users are touching inventory most often",
            "recent staff actions across the workspace",
            "rank staff by audited activity volume",
            "staff actions need management review",
            "staff activity summary from the audit trail",
        )
    ) or _text_matches_all_terms(normalized, r"\bstaff\b", r"\bactivity\b"):
        return "staff_activity"
    if "support access" in normalized or "support sessions should i review first" in normalized:
        return "support_access_audit"
    if any(
        token in normalized
        for token in (
            "permission and security activity",
            "roles or permissions changed recently",
            "mfa, access, and role-change events",
            "security-sensitive audit events need attention",
            "permission changes across the workspace",
        )
    ) or _text_matches_all_terms(normalized, r"\b(permission|security)\b", r"\bactivity\b"):
        return "permission_security_activity"
    if (
        any(
            token in normalized
            for token in (
                "audit logs",
                "audit log",
                "audit events",
                "recent audit events",
                "search audit events",
                "find audit events",
                "workspace audit activity",
            )
        )
        and not any(
            token in normalized
            for token in (
                "staff activity",
                "support access",
                "permission",
                "security",
                "timeline",
            )
        )
    ):
        return "audit_search"
    if any(
        token in normalized
        for token in (
            "audit timeline",
            "event timeline",
            "timeline of audit events",
            "timeline for audit events",
            "chronological audit trail",
        )
    ):
        return "audit_timeline"
    if (
        "subscription" in normalized
        or any(
            token in normalized
            for token in (
                "plan pressure",
                "plan limits",
                "remaining headroom",
                "resources are near the limit",
                "upgrade pressure",
                "near exhaustion",
                "billing status",
                "entitlements",
                "capacity planning",
                "current plan pressure",
                "limit breaches",
                "closest limit breaches",
            )
        )
        or _text_matches_all_terms(normalized, r"\blimit\b", r"\bbreaches?\b")
    ):
        return "subscription_usage_limits"
    return None


def _audit_timeline_items(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for event in events[:12]:
        if not isinstance(event, dict):
            continue
        items.append(
            {
                "timestamp": str(event.get("occurred_at") or event.get("timestamp") or ""),
                "title": str(event.get("summary") or event.get("event_name") or "Audit event"),
                "description": str(event.get("action") or event.get("feature_area") or event.get("source_service") or ""),
                "severity": str(event.get("severity") or "info"),
            }
        )
    return items


def _payload_window_label(payload: dict[str, Any], *, fallback: str) -> str:
    label = str(payload.get("_window_label") or "").strip() if isinstance(payload, dict) else ""
    return label or fallback


def _counter_rows_to_ranked_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "label": str(row.get("key") or "Unknown"),
                "value": int(row.get("count") or 0),
            }
        )
    return items


def _timeline_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in rows[:12]:
        if not isinstance(row, dict):
            continue
        detail = (
            str(row.get("detail") or "").strip()
            or str(row.get("description") or "").strip()
            or str(row.get("action") or "").strip()
            or str(row.get("target_label") or "").strip()
            or str(row.get("reference_number") or "").strip()
            or str(row.get("summary") or "").strip()
        )
        events.append(
            {
                "timestamp": str(row.get("timestamp") or row.get("occurred_at") or ""),
                "title": str(row.get("title") or row.get("summary") or row.get("event_name") or "Event"),
                "detail": detail,
                "severity": str(row.get("severity") or "info"),
            }
        )
    return events


def _daily_count_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_timestamp = str(row.get("timestamp") or row.get("occurred_at") or row.get("created_at") or "").strip()
        if len(raw_timestamp) < 10:
            continue
        bucket = raw_timestamp[:10]
        counts[bucket] = counts.get(bucket, 0) + 1
    return [{"bucket": bucket, "count": count} for bucket, count in sorted(counts.items())]


def _purchase_order_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if isinstance(results, list):
        return [item for item in results if isinstance(item, dict)]
    if isinstance(results, dict) and isinstance(results.get("results"), list):
        return [item for item in results.get("results") if isinstance(item, dict)]
    return []


def _purchase_order_status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "unknown").strip().lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _build_audit_entity_activity_insight(
    payload: dict[str, Any],
    *,
    summary: str,
    title: str,
    source_endpoint: str,
    preview_kind: str,
) -> dict[str, Any]:
    window_label = _payload_window_label(payload, fallback="the selected period")
    action_items = _counter_rows_to_ranked_items(payload.get("actions") if isinstance(payload, dict) else [])
    timeline_events = _timeline_events(payload.get("recent_events") if isinstance(payload, dict) else [])
    daily_activity = payload.get("daily_activity") if isinstance(payload, dict) else []
    daily_activity = daily_activity if isinstance(daily_activity, list) else []
    recent = payload.get("recent_events") if isinstance(payload, dict) else []
    recent = recent if isinstance(recent, list) else []
    first = recent[0] if recent and isinstance(recent[0], dict) else {}
    preview_title = (
        str(first.get("target_label") or "").strip()
        or str(first.get("reference_number") or "").strip()
        or str(first.get("event_name") or "").strip()
        or title
    )
    preview_meta = []
    for label, value in (
        ("Actor", first.get("actor_name") or first.get("actor_email")),
        ("SKU", first.get("entity_sku")),
        ("Barcode", first.get("entity_barcode")),
        ("Terminal", first.get("terminal_id")),
    ):
        if str(value or "").strip():
            preview_meta.append({"label": label, "value": value})
    return {
        "kind": "insight_response",
        "summary": f"{summary.rstrip('.')} for {window_label}.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": f"{title} for {window_label}",
                "data": [
                    {"label": "Events", "value": int(payload.get("event_count") or 0)},
                    {"label": "Action Types", "value": len(action_items)},
                    {"label": "Source Services", "value": len(payload.get("source_services") or []) if isinstance(payload, dict) else 0},
                ],
            },
            {
                "type": "entity_preview",
                "title": "Most recent focus",
                "entity": {
                    "kind": preview_kind,
                    "title": preview_title,
                    "subtitle": str(first.get("summary") or ""),
                    "meta": preview_meta,
                },
            },
            {
                "type": "line_chart",
                "title": f"Daily change volume for {window_label}",
                "data": daily_activity,
                "x_key": "bucket",
                "y_key": "count",
            },
            {
                "type": "timeline",
                "title": f"Audit events for {window_label}",
                "events": timeline_events,
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "audit", "endpoint_or_topic": source_endpoint, "freshness": "live"}],
        "permissions_checked": ["view_audit_logs"],
        "confidence": "high",
        "warnings": [],
    }


def _build_procurement_lifecycle_insight(
    pipeline_payload: dict[str, Any],
    activity_payload: dict[str, Any],
) -> dict[str, Any]:
    window_label = _payload_window_label(activity_payload, fallback="last 30 days")
    status_counts = pipeline_payload.get("status_counts") if isinstance(pipeline_payload, dict) else {}
    status_counts = status_counts if isinstance(status_counts, dict) else {}
    results = pipeline_payload.get("results") if isinstance(pipeline_payload, dict) else []
    results = results if isinstance(results, list) else []
    steps = [
        {
            "label": str(status).replace("_", " ").title(),
            "status": "current" if int(count or 0) > 0 else "pending",
            "detail": f"{int(count or 0)} purchase orders",
        }
        for status, count in status_counts.items()
    ]
    rows = [
        {
            "reference": str(item.get("reference") or ""),
            "status": str(item.get("status") or ""),
            "supplier": str(item.get("supplier_name") or ""),
            "target_date": str(item.get("target_date") or ""),
        }
        for item in results
        if isinstance(item, dict)
    ]
    status_chart = [
        {"label": str(status).replace("_", " ").title(), "value": int(count or 0)}
        for status, count in status_counts.items()
    ]
    timeline_rows = activity_payload.get("timeline") if isinstance(activity_payload, dict) and isinstance(activity_payload.get("timeline"), list) else []
    return {
        "kind": "insight_response",
        "summary": f"Purchase-order lifecycle status is ready for {window_label}.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": f"PO pipeline for {window_label}",
                "data": [
                    {"label": "Open Statuses", "value": len([count for count in status_counts.values() if int(count or 0) > 0])},
                    {"label": "Recent POs", "value": len(rows)},
                    {"label": "Audit Events", "value": int(activity_payload.get("event_count") or 0)},
                ],
            },
            {
                "type": "progress_tracker",
                "title": "Workflow progress",
                "steps": steps,
            },
            {
                "type": "bar_chart",
                "title": f"PO status distribution for {window_label}",
                "data": status_chart,
                "x_key": "label",
                "y_key": "value",
            },
            {
                "type": "line_chart",
                "title": f"PO activity volume for {window_label}",
                "data": _daily_count_series([row for row in timeline_rows if isinstance(row, dict)]),
                "x_key": "bucket",
                "y_key": "count",
            },
            {
                "type": "comparison_table",
                "title": "Recent purchase orders",
                "columns": ["reference", "status", "supplier", "target_date"],
                "rows": rows,
            },
            {
                "type": "timeline",
                "title": f"PO activity timeline for {window_label}",
                "events": _timeline_events(activity_payload.get("timeline") if isinstance(activity_payload, dict) else []),
            },
        ],
        "suggested_actions": [],
        "data_sources": [
            {"service": "inventory", "endpoint_or_topic": "get_purchase_order_analytics", "freshness": "live"},
            {"service": "inventory", "endpoint_or_topic": "search_purchase_orders", "freshness": "live"},
            {"service": "audit", "endpoint_or_topic": "get_purchase_order_activity", "freshness": "live"},
        ],
        "permissions_checked": ["read_inventory", "view_audit_logs"],
        "confidence": "high",
        "warnings": [],
    }


def _build_procurement_receiving_insight(
    pipeline_payload: dict[str, Any],
    activity_payload: dict[str, Any],
) -> dict[str, Any]:
    window_label = _payload_window_label(activity_payload, fallback="last 30 days")
    results = pipeline_payload.get("results") if isinstance(pipeline_payload, dict) else []
    results = results if isinstance(results, list) else []
    timeline_rows = activity_payload.get("timeline") if isinstance(activity_payload, dict) and isinstance(activity_payload.get("timeline"), list) else []
    steps = [
        {
            "label": str(item.get("reference") or "PO"),
            "status": "current" if str(item.get("status") or "").lower() in {"approved", "issued", "received", "overdue"} else "completed",
            "detail": f"{str(item.get('status') or '').title()} with supplier {str(item.get('supplier_name') or 'unassigned')}",
        }
        for item in results[:10]
        if isinstance(item, dict)
    ]
    return {
        "kind": "insight_response",
        "summary": f"Purchase-order receiving lifecycle is ready for {window_label}.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": f"Receiving snapshot for {window_label}",
                "data": [
                    {"label": "Tracked POs", "value": len(results)},
                    {"label": "Audit Events", "value": int(activity_payload.get("event_count") or 0)},
                ],
            },
            {
                "type": "progress_tracker",
                "title": "Receiving progress",
                "steps": steps,
            },
            {
                "type": "line_chart",
                "title": f"Receiving activity volume for {window_label}",
                "data": _daily_count_series([row for row in timeline_rows if isinstance(row, dict)]),
                "x_key": "bucket",
                "y_key": "count",
            },
            {
                "type": "timeline",
                "title": f"Receiving activity for {window_label}",
                "events": _timeline_events(activity_payload.get("timeline") if isinstance(activity_payload, dict) else []),
            },
        ],
        "suggested_actions": [],
        "data_sources": [
            {"service": "inventory", "endpoint_or_topic": "search_purchase_orders", "freshness": "live"},
            {"service": "audit", "endpoint_or_topic": "get_purchase_order_activity", "freshness": "live"},
        ],
        "permissions_checked": ["read_inventory", "view_audit_logs"],
        "confidence": "high",
        "warnings": [] if results else ["No purchase orders are active in the current pipeline."],
    }


def _build_procurement_supplier_insight(payload: dict[str, Any]) -> dict[str, Any]:
    window_label = _payload_window_label(payload, fallback="the selected period")
    analytics = payload.get("analytics") if isinstance(payload, dict) else {}
    analytics = analytics if isinstance(analytics, dict) else {}
    rows = analytics.get("supplier_performance") if isinstance(analytics.get("supplier_performance"), list) else []
    table_rows = []
    if rows:
        table_rows = [
            {
                "supplier": str(item.get("supplier_name") or item.get("supplier__name") or ""),
                "orders": int(item.get("order_count") or 0),
                "value": float(item.get("total_value") or 0),
                "avg_delivery_time": str(item.get("avg_delivery_time") or ""),
                "on_time_deliveries": int(item.get("on_time_deliveries") or 0),
            }
            for item in rows
            if isinstance(item, dict)
        ]
    else:
        grouped: dict[str, dict[str, Any]] = {}
        for item in _purchase_order_results(payload):
            supplier = str(item.get("supplier_name") or "Unassigned supplier")
            current = grouped.setdefault(
                supplier,
                {"supplier": supplier, "orders": 0, "value": 0.0, "avg_delivery_time": "", "on_time_deliveries": 0},
            )
            current["orders"] += 1
        table_rows = list(grouped.values())
    ranked_items = [
        {"label": row["supplier"], "value": row["value"], "secondary_value": row["orders"]}
        for row in table_rows
    ]
    chart_rows = [{"label": row["supplier"], "value": row["value"]} for row in table_rows[:8]]
    return {
        "kind": "insight_response",
        "summary": f"Supplier performance for {window_label} is ready.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": f"Supplier performance for {window_label}",
                "data": [
                    {"label": "Suppliers", "value": len(table_rows)},
                    {"label": "On-Time Rate", "value": float(analytics.get("on_time_delivery_rate") or 0)},
                    {"label": "Avg Delivery Days", "value": float(analytics.get("average_delivery_time") or 0)},
                ],
            },
            {
                "type": "comparison_table",
                "title": f"Supplier scorecard for {window_label}",
                "columns": ["supplier", "orders", "value", "avg_delivery_time", "on_time_deliveries"],
                "rows": table_rows,
            },
            {
                "type": "bar_chart",
                "title": f"Supplier value contribution for {window_label}",
                "data": chart_rows,
                "x_key": "label",
                "y_key": "value",
            },
            {
                "type": "ranked_list",
                "title": f"Top suppliers by value for {window_label}",
                "items": ranked_items,
                "ordered_by": "value",
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "inventory", "endpoint_or_topic": "get_purchase_order_analytics", "freshness": "live"}],
        "permissions_checked": ["read_inventory"],
        "confidence": "high",
        "warnings": [] if table_rows else ["No supplier performance rows were returned."],
    }


def _build_procurement_delay_exception_insight(
    pipeline_payload: dict[str, Any],
    activity_payload: dict[str, Any],
) -> dict[str, Any]:
    window_label = _payload_window_label(activity_payload, fallback="the selected period")
    results = pipeline_payload.get("results") if isinstance(pipeline_payload, dict) else []
    results = results if isinstance(results, list) else []
    risk_items = [
        {
            "label": str(item.get("reference") or "PO"),
            "severity": "high" if str(item.get("status") or "").lower() == "overdue" else "medium",
            "description": f"Current status is {str(item.get('status') or 'unknown')} for supplier {str(item.get('supplier_name') or 'unassigned')}.",
        }
        for item in results[:10]
        if isinstance(item, dict)
    ]
    ranked_items = [
        {"label": str(item.get("reference") or "PO"), "value": 100 if str(item.get("status") or "").lower() == "overdue" else 50}
        for item in results[:10]
        if isinstance(item, dict)
    ]
    return {
        "kind": "insight_response",
        "summary": f"Procurement delays and receiving exceptions are ready for {window_label}.",
        "widgets": [
            {
                "type": "risk_panel",
                "title": f"Open procurement risks for {window_label}",
                "items": risk_items or [{"label": "Current procurement posture", "severity": "low", "description": "No open receiving exceptions are active."}],
            },
            {
                "type": "ranked_list",
                "title": f"Largest remaining receipts for {window_label}",
                "items": ranked_items,
                "ordered_by": "remaining_quantity",
            },
            {
                "type": "timeline",
                "title": f"Exception timeline for {window_label}",
                "events": _timeline_events(activity_payload.get("timeline") if isinstance(activity_payload, dict) else []),
            },
        ],
        "suggested_actions": [],
        "data_sources": [
            {"service": "inventory", "endpoint_or_topic": "search_purchase_orders", "freshness": "live"},
            {"service": "audit", "endpoint_or_topic": "get_purchase_order_activity", "freshness": "live"},
        ],
        "permissions_checked": ["read_inventory", "view_audit_logs"],
        "confidence": "high",
        "warnings": [],
    }


def _build_procurement_cost_variance_insight(payload: dict[str, Any]) -> dict[str, Any]:
    window_label = _payload_window_label(payload, fallback="the selected period")
    analytics = payload.get("analytics") if isinstance(payload, dict) else {}
    analytics = analytics if isinstance(analytics, dict) else {}
    rows = analytics.get("supplier_performance") if isinstance(analytics.get("supplier_performance"), list) else []
    if rows:
        table_rows = [
            {
                "supplier": str(item.get("supplier_name") or item.get("supplier__name") or ""),
                "orders": int(item.get("order_count") or 0),
                "value": float(item.get("total_value") or 0),
                "avg_delivery_time": str(item.get("avg_delivery_time") or ""),
            }
            for item in rows
            if isinstance(item, dict)
        ]
    else:
        table_rows = [
            {
                "supplier": str(item.get("supplier_name") or "Unassigned supplier"),
                "orders": 1,
                "value": 0.0,
                "avg_delivery_time": str(item.get("delivery_date") or ""),
            }
            for item in _purchase_order_results(payload)[:10]
        ]
    risk_items = [
        {
            "label": row["supplier"],
            "severity": "medium",
            "description": f"Total order value {row['value']:.2f} across {row['orders']} orders.",
        }
        for row in table_rows[:5]
    ]
    chart_rows = [{"label": row["supplier"], "value": row["value"]} for row in table_rows[:8]]
    return {
        "kind": "insight_response",
        "summary": f"Procurement cost variance indicators are ready for {window_label}.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": f"Cost baseline for {window_label}",
                "data": [
                    {"label": "Total Order Value", "value": float(analytics.get("total_order_value") or 0)},
                    {"label": "Average Order Value", "value": float(analytics.get("average_order_value") or 0)},
                    {"label": "Cost Per Order", "value": float(analytics.get("cost_per_order") or 0)},
                ],
            },
            {
                "type": "comparison_table",
                "title": f"Supplier cost view for {window_label}",
                "columns": ["supplier", "orders", "value", "avg_delivery_time"],
                "rows": table_rows,
            },
            {
                "type": "bar_chart",
                "title": f"Supplier order value variance for {window_label}",
                "data": chart_rows,
                "x_key": "label",
                "y_key": "value",
            },
            {
                "type": "risk_panel",
                "title": f"Price review candidates for {window_label}",
                "items": risk_items or [{"label": "Current cost posture", "severity": "low", "description": "No high-variance supplier rows were returned."}],
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "inventory", "endpoint_or_topic": "get_purchase_order_analytics", "freshness": "live"}],
        "permissions_checked": ["read_inventory"],
        "confidence": "medium",
        "warnings": [] if table_rows else ["No supplier cost rows were returned."],
    }


def _build_product_import_opportunities_insight(matches_payload: dict[str, Any]) -> dict[str, Any]:
    results = matches_payload.get("results") if isinstance(matches_payload, dict) else []
    results = results if isinstance(results, list) else []
    ranked_items = [
        {
            "label": str(item.get("name") or "Catalog product"),
            "value": int(item.get("variant_count") or 0),
            "secondary_value": str(item.get("brand") or ""),
        }
        for item in results
        if isinstance(item, dict) and not bool(item.get("already_imported"))
    ]
    rows = [
        {
            "name": str(item.get("name") or ""),
            "brand": str(item.get("brand") or ""),
            "category": str(item.get("category_name") or ""),
            "variants": int(item.get("variant_count") or 0),
            "already_imported": bool(item.get("already_imported")),
        }
        for item in results[:10]
        if isinstance(item, dict)
    ]
    return {
        "kind": "insight_response",
        "summary": "Global catalog import opportunities are ready.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": "Catalog opportunity snapshot",
                "data": [
                    {"label": "Matches", "value": int(matches_payload.get("count") or len(rows))},
                    {"label": "New Opportunities", "value": len(ranked_items)},
                ],
            },
            {
                "type": "ranked_list",
                "title": "Top import opportunities",
                "items": ranked_items,
                "ordered_by": "variant_count",
            },
            {
                "type": "comparison_table",
                "title": "Catalog opportunity board",
                "columns": ["name", "brand", "category", "variants", "already_imported"],
                "rows": rows,
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "products", "endpoint_or_topic": "get_top_catalog_matches", "freshness": "live"}],
        "permissions_checked": ["read_products"],
        "confidence": "high",
        "warnings": [] if rows else ["No global catalog matches were returned."],
    }


def _build_variant_lookup_insight(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results") if isinstance(payload, dict) else []
    results = results if isinstance(results, list) else []
    first = results[0] if results and isinstance(results[0], dict) else {}
    rows = [
        {
            "product_name": str(item.get("product_name") or ""),
            "name": str(item.get("name") or ""),
            "sku": str(item.get("sku") or ""),
            "barcode": str(item.get("barcode") or ""),
            "selling_price": float(item.get("selling_price") or 0),
        }
        for item in results[:10]
        if isinstance(item, dict)
    ]
    return {
        "kind": "insight_response",
        "summary": "Variant lookup results are ready.",
        "widgets": [
            {
                "type": "entity_preview",
                "title": "Best variant match",
                "entity": {
                    "kind": "Variant",
                    "title": str(first.get("name") or first.get("product_name") or "Variant"),
                    "subtitle": str(first.get("product_name") or ""),
                    "meta": [
                        {"label": "SKU", "value": str(first.get("sku") or "")},
                        {"label": "Barcode", "value": str(first.get("barcode") or "")},
                        {"label": "Price", "value": float(first.get("selling_price") or 0)},
                    ],
                },
            },
            {
                "type": "comparison_table",
                "title": "Closest variant matches",
                "columns": ["product_name", "name", "sku", "barcode", "selling_price"],
                "rows": rows,
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "products", "endpoint_or_topic": "search_product_variants", "freshness": "live"}],
        "permissions_checked": ["read_products"],
        "confidence": "high",
        "warnings": [] if rows else ["No variant matches were returned."],
    }


def _build_catalog_gap_insight(dashboard_payload: dict[str, Any], matches_payload: dict[str, Any], alerts_payload: dict[str, Any]) -> dict[str, Any]:
    dashboard = dashboard_payload.get("dashboard") if isinstance(dashboard_payload, dict) else {}
    dashboard = dashboard if isinstance(dashboard, dict) else {}
    categories = dashboard.get("category_distribution") if isinstance(dashboard.get("category_distribution"), list) else []
    matches = matches_payload.get("results") if isinstance(matches_payload, dict) else []
    matches = matches if isinstance(matches, list) else []
    alerts = alerts_payload.get("alerts") if isinstance(alerts_payload, dict) else {}
    alerts = alerts if isinstance(alerts, dict) else {}
    risk_items = [
        {
            "label": str(item.get("category_name") or "Category"),
            "severity": "medium",
            "description": f"{int(item.get('count') or 0)} active products currently cover this category.",
        }
        for item in categories[-5:]
        if isinstance(item, dict)
    ]
    if int(alerts.get("total_alerts") or 0) > 0:
        risk_items.append(
            {
                "label": "Low-stock exposure",
                "severity": "high",
                "description": f"{int(alerts.get('total_alerts') or 0)} product-stock alerts may be amplifying catalog gaps.",
            }
        )
    ranked_items = [
        {"label": str(item.get("name") or "Catalog product"), "value": int(item.get("variant_count") or 0)}
        for item in matches
        if isinstance(item, dict) and not bool(item.get("already_imported"))
    ]
    return {
        "kind": "insight_response",
        "summary": "Catalog gap signals are ready.",
        "widgets": [
            {
                "type": "risk_panel",
                "title": "Assortment weaknesses",
                "items": risk_items or [{"label": "Catalog posture", "severity": "low", "description": "No obvious assortment imbalance was detected."}],
            },
            {
                "type": "ranked_list",
                "title": "Gap-closing opportunities",
                "items": ranked_items[:10],
                "ordered_by": "variant_count",
            },
            {
                "type": "comparison_table",
                "title": "Category distribution",
                "columns": ["category_name", "count"],
                "rows": [item for item in categories if isinstance(item, dict)],
            },
        ],
        "suggested_actions": [],
        "data_sources": [
            {"service": "products", "endpoint_or_topic": "get_product_dashboard_stats", "freshness": "live"},
            {"service": "products", "endpoint_or_topic": "get_top_catalog_matches", "freshness": "live"},
            {"service": "products", "endpoint_or_topic": "get_product_stock_alerts", "freshness": "live"},
        ],
        "permissions_checked": ["read_products"],
        "confidence": "medium",
        "warnings": [],
    }


def _build_duplicate_code_insight(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results") if isinstance(payload, dict) else []
    results = results if isinstance(results, list) else []
    sku_counts: dict[str, list[dict[str, Any]]] = {}
    barcode_counts: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        sku = str(item.get("sku") or "").strip()
        barcode = str(item.get("barcode") or "").strip()
        if sku:
            sku_counts.setdefault(sku, []).append(item)
        if barcode:
            barcode_counts.setdefault(barcode, []).append(item)
    rows = []
    for code, items in list(sku_counts.items()) + list(barcode_counts.items()):
        if len(items) > 1:
            rows.append(
                {
                    "code": code,
                    "count": len(items),
                    "products": ", ".join(str(item.get("product_name") or item.get("name") or "") for item in items[:3]),
                }
            )
    if not rows:
        for item in results[:10]:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "code": str(item.get("sku") or item.get("barcode") or ""),
                    "count": 1,
                    "products": str(item.get("product_name") or item.get("name") or ""),
                }
            )
    risk_items = [
        {
            "label": row["code"] or "Missing code",
            "severity": "high" if int(row["count"] or 0) > 1 else "low",
            "description": f"{int(row['count'] or 0)} matching variant records detected.",
        }
        for row in rows[:10]
    ]
    return {
        "kind": "insight_response",
        "summary": "Duplicate barcode and SKU risk is ready.",
        "widgets": [
            {
                "type": "comparison_table",
                "title": "Potential code conflicts",
                "columns": ["code", "count", "products"],
                "rows": rows,
            },
            {
                "type": "risk_panel",
                "title": "Identifier risks",
                "items": risk_items,
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "products", "endpoint_or_topic": "search_product_variants", "freshness": "live"}],
        "permissions_checked": ["read_products"],
        "confidence": "medium",
        "warnings": [],
    }


def _build_product_media_category_insight(dashboard_payload: dict[str, Any], variant_payload: dict[str, Any]) -> dict[str, Any]:
    dashboard = dashboard_payload.get("dashboard") if isinstance(dashboard_payload, dict) else {}
    dashboard = dashboard if isinstance(dashboard, dict) else {}
    categories = dashboard.get("category_distribution") if isinstance(dashboard.get("category_distribution"), list) else []
    variants = variant_payload.get("results") if isinstance(variant_payload, dict) else []
    variants = variants if isinstance(variants, list) else []
    ranked_items = [
        {"label": str(item.get("category_name") or "Category"), "value": int(item.get("count") or 0)}
        for item in reversed([item for item in categories if isinstance(item, dict)])
    ]
    first = variants[0] if variants and isinstance(variants[0], dict) else {}
    return {
        "kind": "insight_response",
        "summary": "Product media and content quality opportunities are ready.",
        "widgets": [
            {
                "type": "ranked_list",
                "title": "Category curation opportunities",
                "items": ranked_items,
                "ordered_by": "count",
            },
            {
                "type": "entity_preview",
                "title": "Variant to review",
                "entity": {
                    "kind": "Variant",
                    "title": str(first.get("name") or first.get("product_name") or "Variant"),
                    "subtitle": str(first.get("product_name") or ""),
                    "meta": [
                        {"label": "SKU", "value": str(first.get("sku") or "")},
                        {"label": "Barcode", "value": str(first.get("barcode") or "")},
                    ],
                },
            },
        ],
        "suggested_actions": [],
        "data_sources": [
            {"service": "products", "endpoint_or_topic": "get_product_dashboard_stats", "freshness": "live"},
            {"service": "products", "endpoint_or_topic": "search_product_variants", "freshness": "live"},
        ],
        "permissions_checked": ["read_products"],
        "confidence": "medium",
        "warnings": [],
    }


def _build_host_cross_domain_insight(
    sales_payload: dict[str, Any],
    stock_payload: dict[str, Any],
    pipeline_payload: dict[str, Any],
    subscription_payload: dict[str, Any],
) -> dict[str, Any]:
    window_label = _payload_window_label(sales_payload, fallback="today")
    summary = stock_payload.get("summary") if isinstance(stock_payload, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    features = subscription_payload.get("features") if isinstance(subscription_payload, dict) else []
    features = features if isinstance(features, list) else []
    pipeline_status_counts = pipeline_payload.get("status_counts") if isinstance(pipeline_payload, dict) else {}
    pipeline_status_counts = pipeline_status_counts if isinstance(pipeline_status_counts, dict) else {}
    risk_items = [
        {
            "label": "Out of stock",
            "severity": "high" if int(summary.get("out_of_stock_count") or 0) > 0 else "low",
            "description": f"{int(summary.get('out_of_stock_count') or 0)} items are out of stock.",
        },
        {
            "label": "Open procurement flow",
            "severity": "medium" if len([count for count in pipeline_status_counts.values() if int(count or 0) > 0]) > 0 else "low",
            "description": f"{len([count for count in pipeline_status_counts.values() if int(count or 0) > 0])} purchase-order statuses are currently active.",
        },
    ]
    near_limit = [item for item in features if isinstance(item, dict) and str(item.get("status") or "") in {"near_limit", "at_limit"}]
    if near_limit:
        risk_items.append(
            {
                "label": "Subscription pressure",
                "severity": "medium",
                "description": f"{len(near_limit)} tracked resources are near or at their plan limit.",
            }
        )
    return {
        "kind": "insight_response",
        "summary": f"Workspace operational summary is ready for {window_label}.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": f"Operational snapshot for {window_label}",
                "data": [
                    {"label": "Sales", "value": float(sales_payload.get("total_sales") or 0)},
                    {"label": "Orders", "value": sum(int(item.get("order_count") or 0) for item in (sales_payload.get("groups") or []) if isinstance(item, dict))},
                    {"label": "Reorder Candidates", "value": int(summary.get("reorder_count") or 0)},
                    {"label": "Open PO Statuses", "value": len([count for count in pipeline_status_counts.values() if int(count or 0) > 0])},
                ],
            },
            {
                "type": "risk_panel",
                "title": "Operational risks",
                "items": risk_items,
            },
            {
                "type": "comparison_table",
                "title": f"Sales by location for {window_label}",
                "columns": ["label", "order_count", "total_sales"],
                "rows": [item for item in (sales_payload.get("groups") or []) if isinstance(item, dict)],
            },
        ],
        "suggested_actions": [],
        "data_sources": [
            {"service": "pos", "endpoint_or_topic": "get_sales_summary", "freshness": "live"},
            {"service": "inventory", "endpoint_or_topic": "get_stock_risk", "freshness": "live"},
            {"service": "inventory", "endpoint_or_topic": "get_purchase_order_analytics", "freshness": "live"},
            {"service": "subscriptions", "endpoint_or_topic": "get_usage_and_limits", "freshness": "live"},
        ],
        "permissions_checked": ["view_pos_reports", "read_inventory", "manage_workspace_subscription"],
        "confidence": "high",
        "warnings": [],
    }


def _build_host_location_comparison_insight(
    sales_payload: dict[str, Any],
    location_payload: dict[str, Any],
) -> dict[str, Any]:
    window_label = _payload_window_label(sales_payload, fallback="today")
    sales_groups = sales_payload.get("groups") if isinstance(sales_payload, dict) else []
    sales_groups = sales_groups if isinstance(sales_groups, list) else []
    locations = location_payload.get("results") if isinstance(location_payload, dict) else []
    locations = locations if isinstance(locations, list) else []
    by_name = {
        str(item.get("name") or item.get("label") or "").strip().lower(): item
        for item in locations
        if isinstance(item, dict)
    }
    rows = []
    bar_rows = []
    for sale in sales_groups:
        if not isinstance(sale, dict):
            continue
        label = str(sale.get("label") or "Location")
        location = by_name.get(label.strip().lower(), {})
        row = {
            "location": label,
            "sales": float(sale.get("total_sales") or 0),
            "orders": int(sale.get("order_count") or 0),
            "quantity": float(location.get("total_quantity") or 0),
            "value": float(location.get("total_value") or 0),
            "expiring_soon": int(location.get("expiring_soon_count") or 0),
        }
        rows.append(row)
        bar_rows.append({"label": label, "value": row["sales"]})
    return {
        "kind": "insight_response",
        "summary": f"Location comparison across sales and stock health is ready for {window_label}.",
        "widgets": [
            {
                "type": "comparison_table",
                "title": f"Location scorecard for {window_label}",
                "columns": ["location", "sales", "orders", "quantity", "value", "expiring_soon"],
                "rows": rows,
            },
            {
                "type": "metric_grid",
                "title": "Location comparison",
                "data": [
                    {"label": "Locations", "value": len(rows)},
                    {"label": "Sales Leaders", "value": rows[0]["location"] if rows else "None"},
                ],
            },
            {
                "type": "bar_chart",
                "title": f"Sales by location for {window_label}",
                "data": bar_rows,
                "x_key": "label",
                "y_key": "value",
            },
        ],
        "suggested_actions": [],
        "data_sources": [
            {"service": "pos", "endpoint_or_topic": "get_sales_summary", "freshness": "live"},
            {"service": "inventory", "endpoint_or_topic": "search_stock_locations", "freshness": "live"},
        ],
        "permissions_checked": ["view_pos_reports", "read_inventory"],
        "confidence": "high",
        "warnings": [],
    }


def _build_host_recommendations_insight(
    stock_payload: dict[str, Any],
    pipeline_payload: dict[str, Any],
    subscription_payload: dict[str, Any],
) -> dict[str, Any]:
    summary = stock_payload.get("summary") if isinstance(stock_payload, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    features = subscription_payload.get("features") if isinstance(subscription_payload, dict) else []
    features = features if isinstance(features, list) else []
    pipeline_status_counts = pipeline_payload.get("status_counts") if isinstance(pipeline_payload, dict) else {}
    pipeline_status_counts = pipeline_status_counts if isinstance(pipeline_status_counts, dict) else {}
    ranked_items = []
    risk_items = []
    if int(summary.get("out_of_stock_count") or 0) > 0:
        ranked_items.append({"label": "Replenish out-of-stock items", "value": 1})
        risk_items.append({"label": "Stockouts", "severity": "high", "description": f"{int(summary.get('out_of_stock_count') or 0)} items are already out of stock."})
    if len([count for count in pipeline_status_counts.values() if int(count or 0) > 0]) > 0:
        ranked_items.append({"label": "Advance open purchase orders", "value": 2})
        risk_items.append({"label": "Procurement backlog", "severity": "medium", "description": f"{len([count for count in pipeline_status_counts.values() if int(count or 0) > 0])} purchase-order statuses remain active."})
    if any(isinstance(item, dict) and str(item.get("status") or "") in {"near_limit", "at_limit"} for item in features):
        ranked_items.append({"label": "Review plan pressure", "value": 3})
        risk_items.append({"label": "Capacity pressure", "severity": "medium", "description": "One or more tracked subscription resources are near their limit."})
    if not ranked_items:
        ranked_items.append({"label": "Maintain current operating posture", "value": 1})
        risk_items.append({"label": "Current posture", "severity": "low", "description": "No immediate workspace-wide pressure was detected."})
    return {
        "kind": "insight_response",
        "summary": "Prioritized workspace actions are ready.",
        "widgets": [
            {
                "type": "risk_panel",
                "title": "Why these actions matter",
                "items": risk_items,
            },
            {
                "type": "ranked_list",
                "title": "Top next actions",
                "items": ranked_items,
                "ordered_by": "priority",
            },
        ],
        "suggested_actions": [],
        "data_sources": [
            {"service": "inventory", "endpoint_or_topic": "get_stock_risk", "freshness": "live"},
            {"service": "inventory", "endpoint_or_topic": "get_purchase_order_analytics", "freshness": "live"},
            {"service": "subscriptions", "endpoint_or_topic": "get_usage_and_limits", "freshness": "live"},
        ],
        "permissions_checked": ["read_inventory", "manage_workspace_subscription"],
        "confidence": "high",
        "warnings": [],
    }


def _build_host_business_analyst_insight(
    sales_by_day_payload: dict[str, Any],
    sales_by_location_payload: dict[str, Any],
    top_sellers_payload: dict[str, Any],
    stock_payload: dict[str, Any],
    pipeline_payload: dict[str, Any],
    subscription_payload: dict[str, Any],
) -> dict[str, Any]:
    window_label = _payload_window_label(sales_by_day_payload, fallback="last 1 year")
    day_groups = sales_by_day_payload.get("groups") if isinstance(sales_by_day_payload, dict) else []
    day_groups = day_groups if isinstance(day_groups, list) else []
    location_groups = sales_by_location_payload.get("groups") if isinstance(sales_by_location_payload, dict) else []
    location_groups = location_groups if isinstance(location_groups, list) else []
    top_sellers = top_sellers_payload.get("results") if isinstance(top_sellers_payload, dict) else []
    top_sellers = top_sellers if isinstance(top_sellers, list) else []
    stock_summary = stock_payload.get("summary") if isinstance(stock_payload, dict) else {}
    stock_summary = stock_summary if isinstance(stock_summary, dict) else {}
    pipeline_status_counts = pipeline_payload.get("status_counts") if isinstance(pipeline_payload, dict) else {}
    pipeline_status_counts = pipeline_status_counts if isinstance(pipeline_status_counts, dict) else {}
    features = subscription_payload.get("features") if isinstance(subscription_payload, dict) else []
    features = features if isinstance(features, list) else []

    total_sales = round(sum(float(item.get("total_sales") or 0) for item in day_groups if isinstance(item, dict)), 2)
    total_orders = sum(int(item.get("order_count") or 0) for item in day_groups if isinstance(item, dict))
    active_days = len([item for item in day_groups if isinstance(item, dict) and (float(item.get("total_sales") or 0) > 0 or int(item.get("order_count") or 0) > 0)])
    avg_daily_sales = round(total_sales / active_days, 2) if active_days else 0
    best_day = max(
        (item for item in day_groups if isinstance(item, dict)),
        key=lambda item: (float(item.get("total_sales") or 0), int(item.get("order_count") or 0)),
        default={},
    )
    top_location = max(
        (item for item in location_groups if isinstance(item, dict)),
        key=lambda item: (float(item.get("total_sales") or 0), int(item.get("order_count") or 0)),
        default={},
    )
    out_of_stock_count = int(stock_summary.get("out_of_stock_count") or 0)
    reorder_count = int(stock_summary.get("reorder_count") or 0)
    expiring_count = int(stock_summary.get("expiring_count") or 0)
    active_po_statuses = len([count for count in pipeline_status_counts.values() if int(count or 0) > 0])
    near_limit = [item for item in features if isinstance(item, dict) and str(item.get("status") or "") in {"near_limit", "at_limit"}]

    currency_code = str(sales_by_day_payload.get("currency_code") or sales_by_day_payload.get("currency") or "NGN").upper()
    currency_symbol = {
        "NGN": "₦",
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
        "JPY": "¥",
        "CAD": "C$",
        "AUD": "A$",
        "GHS": "₵",
        "KES": "KSh",
        "ZAR": "R",
    }.get(currency_code, f"{currency_code} ")

    def money_label(amount: Any) -> str:
        return f"{currency_symbol}{float(amount or 0):,.2f}"

    def count_label(value: Any) -> str:
        numeric = float(value or 0)
        return f"{numeric:,.0f}" if numeric.is_integer() else f"{numeric:,.2f}"

    trend_rows = [
        {
            "label": str(item.get("label") or item.get("bucket") or ""),
            "sales": round(float(item.get("total_sales") or 0), 2),
            "orders": int(item.get("order_count") or 0),
        }
        for item in day_groups
        if isinstance(item, dict)
    ]
    location_rows = [
        {
            "location": str(item.get("label") or "Location"),
            "sales": round(float(item.get("total_sales") or 0), 2),
            "orders": int(item.get("order_count") or 0),
        }
        for item in location_groups
        if isinstance(item, dict)
    ]
    ranked_sellers = []
    for item in top_sellers[:8]:
        if not isinstance(item, dict):
            continue
        ranked_sellers.append(
            {
                "label": str(item.get("variant_name") or item.get("product_name") or item.get("name") or "Product"),
                "value": round(float(item.get("sales_total") or 0), 2),
                "format": "currency",
                "secondary_value": round(float(item.get("quantity_sold") or 0), 2),
                "secondary_format": "number",
                "detail": f"{count_label(item.get('order_count'))} orders",
                "barcode": str(item.get("barcode_snapshot") or item.get("barcode") or ""),
                "image_url": _product_image_url(item),
            }
        )

    risk_items = []
    if out_of_stock_count:
        risk_items.append({"label": "Stockouts are blocking demand", "severity": "high", "description": f"{out_of_stock_count} items are out of stock."})
    if reorder_count:
        risk_items.append({"label": "Reorder queue needs attention", "severity": "medium", "description": f"{reorder_count} items are candidates for replenishment."})
    if expiring_count:
        risk_items.append({"label": "Expiry exposure", "severity": "medium", "description": f"{expiring_count} items are close to expiry."})
    if active_po_statuses:
        risk_items.append({"label": "Open procurement work", "severity": "medium", "description": f"{active_po_statuses} purchase-order statuses still have active work."})
    if near_limit:
        risk_items.append({"label": "Plan capacity pressure", "severity": "medium", "description": f"{len(near_limit)} subscription resources are near or at limit."})
    if not risk_items:
        risk_items.append({"label": "No critical cross-service pressure detected", "severity": "low", "description": "Sales, stock, procurement, and plan limits do not show an immediate critical exception."})

    action_items = []
    if out_of_stock_count or reorder_count:
        action_items.append({"label": "Prioritize replenishment before sales campaigns", "value": 1, "format": "number", "hide_value": True, "detail": "Resolve stockouts and reorder candidates first so demand generation does not expose unavailable products."})
    if active_po_statuses:
        action_items.append({"label": "Close the oldest purchase-order bottlenecks", "value": 2, "format": "number", "hide_value": True, "detail": "Move pending, issued, and receiving POs forward before creating more procurement noise."})
    if ranked_sellers:
        action_items.append({"label": "Protect the top sellers from stock disruption", "value": 3, "format": "number", "hide_value": True, "detail": "Use the top-seller list as the first stock-watch set for purchasing and transfer decisions."})
    if near_limit:
        action_items.append({"label": "Review subscription limits before adding more data/users", "value": 4, "format": "number", "hide_value": True, "detail": "Plan pressure can block operational scale if not handled before more imports, staff, or AI usage."})
    if not action_items:
        action_items.append({"label": "Maintain the current operating posture", "value": 1, "format": "number", "hide_value": True, "detail": "No urgent action surfaced from this cross-service review."})

    insight_rows = [
        {
            "title": "Revenue posture",
            "detail": f"{count_label(total_orders)} completed orders generated {money_label(total_sales)} across {window_label}; average active-day revenue is {money_label(avg_daily_sales)}.",
        },
        {
            "title": "Best day",
            "detail": (
                f"{best_day.get('label')} was the strongest day at {money_label(best_day.get('total_sales'))} across {count_label(best_day.get('order_count'))} orders."
                if best_day
                else "No peak sales day was available for the selected window."
            ),
        },
        {
            "title": "Location signal",
            "detail": (
                f"{top_location.get('label')} leads location revenue at {money_label(top_location.get('total_sales'))}."
                if top_location
                else "No location-level sales signal was available."
            ),
        },
    ]

    return {
        "kind": "insight_response",
        "summary": f"Business analyst review is ready for {window_label}.",
        "explanation": "This combines POS revenue, order frequency, top sellers, stock risk, procurement status, and subscription pressure into one owner-level review.",
        "timeframe": {
            "label": window_label,
            "start_date": str(sales_by_day_payload.get("_window_start_date") or ""),
            "end_date": str(sales_by_day_payload.get("_window_end_date") or ""),
            "period": str(sales_by_day_payload.get("_window_period") or ""),
        },
        "insights": insight_rows,
        "widgets": [
            {
                "type": "metric_grid",
                "title": f"Business analyst snapshot for {window_label}",
                "data": [
                    {"label": "Revenue", "value": total_sales, "format": "currency"},
                    {"label": "Orders", "value": total_orders, "format": "number"},
                    {"label": "Active Sales Days", "value": active_days, "format": "number"},
                    {"label": "Avg Active-Day Revenue", "value": avg_daily_sales, "format": "currency"},
                ],
            },
            {
                "type": "line_chart",
                "title": f"Revenue trend for {window_label}",
                "subtitle": "Use this to see when revenue momentum gained or weakened.",
                "x_key": "label",
                "series": [
                    {"key": "sales", "label": "Revenue", "color": "#1d4ed8"},
                ],
                "value_format": "currency",
                "data": trend_rows,
            },
            {
                "type": "line_chart",
                "title": f"Order-count trend for {window_label}",
                "subtitle": "Use this to separate purchase frequency from revenue value.",
                "x_key": "label",
                "series": [
                    {"key": "orders", "label": "Orders", "color": "#0f766e"},
                ],
                "value_format": "number",
                "data": trend_rows,
            },
            {
                "type": "comparison_table",
                "title": f"Location revenue scorecard for {window_label}",
                "columns": ["location", "sales", "orders"],
                "rows": location_rows,
            },
            {
                "type": "ranked_list",
                "title": f"Top products shaping revenue for {window_label}",
                "items": ranked_sellers,
                "ordered_by": "sales_total",
            },
            {
                "type": "risk_panel",
                "title": "What needs management attention",
                "items": risk_items,
            },
            {
                "type": "ranked_list",
                "title": "Recommended owner actions",
                "items": action_items,
                "ordered_by": "priority",
            },
        ],
        "suggested_actions": [],
        "data_sources": [
            {"service": "pos", "endpoint_or_topic": "get_sales_summary", "freshness": "live"},
            {"service": "pos", "endpoint_or_topic": "get_top_sellers", "freshness": "live"},
            {"service": "inventory", "endpoint_or_topic": "get_stock_risk", "freshness": "live"},
            {"service": "inventory", "endpoint_or_topic": "search_purchase_orders", "freshness": "live"},
            {"service": "subscriptions", "endpoint_or_topic": "get_usage_and_limits", "freshness": "live"},
        ],
        "permissions_checked": ["view_pos_reports", "read_inventory", "manage_workspace_subscription"],
        "confidence": "high" if day_groups or ranked_sellers else "medium",
        "warnings": [] if day_groups or ranked_sellers else ["No completed POS sales were available for the selected review window."],
    }


def _build_staff_activity_insight(payload: dict[str, Any]) -> dict[str, Any]:
    window_label = _payload_window_label(payload, fallback="last 30 days")
    action_items = _counter_rows_to_ranked_items(payload.get("actions") if isinstance(payload, dict) else [])
    timeline_items = _audit_timeline_items(payload.get("recent_events") if isinstance(payload, dict) else [])
    daily_activity = payload.get("daily_activity") if isinstance(payload, dict) else []
    daily_activity = daily_activity if isinstance(daily_activity, list) else []
    return {
        "kind": "insight_response",
        "summary": f"Staff audit activity is ready for {window_label}.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": f"Staff audit activity for {window_label}",
                "data": [
                    {"label": "Events", "value": int(payload.get("event_count") or 0)},
                    {"label": "Action Types", "value": len(action_items)},
                    {
                        "label": "Source Services",
                        "value": len(payload.get("source_services") or []) if isinstance(payload, dict) else 0,
                    },
                ],
            },
            {
                "type": "line_chart",
                "title": f"Daily staff activity for {window_label}",
                "data": daily_activity,
                "x_key": "bucket",
                "y_key": "count",
            },
            {
                "type": "ranked_list",
                "title": "Most frequent staff actions",
                "items": action_items,
                "ordered_by": "count",
            },
            {
                "type": "timeline",
                "title": f"Audit events for {window_label}",
                "events": timeline_items,
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "audit", "endpoint_or_topic": "get_staff_activity", "freshness": "live"}],
        "permissions_checked": ["view_audit_logs"],
        "confidence": "high",
        "warnings": [],
    }


def _build_permission_security_insight(payload: dict[str, Any], *, support_access_only: bool) -> dict[str, Any]:
    window_label = _payload_window_label(payload, fallback="last 30 days")
    actor_items = _counter_rows_to_ranked_items(payload.get("actors") if isinstance(payload, dict) else [])
    grant_items = _counter_rows_to_ranked_items(payload.get("support_access_grants") if isinstance(payload, dict) else [])
    timeline_items = _audit_timeline_items(payload.get("recent_events") if isinstance(payload, dict) else [])
    severity_rows = payload.get("severities") if isinstance(payload, dict) else []
    severity_rows = severity_rows if isinstance(severity_rows, list) else []
    summary = (
        f"Support access audit is ready for {window_label}."
        if support_access_only
        else f"Permission and security activity is ready for {window_label}."
    )
    risk_items = [
        {
            "label": str(row.get("key") or "severity"),
            "severity": "high" if str(row.get("key") or "").lower() in {"warning", "error", "critical"} else "medium",
            "description": f"{int(row.get('count') or 0)} events",
        }
        for row in severity_rows
        if isinstance(row, dict)
    ]
    return {
        "kind": "insight_response",
        "summary": summary,
        "widgets": [
            {
                "type": "metric_grid",
                "title": f"Security activity for {window_label}",
                "data": [
                    {"label": "Events", "value": int(payload.get("event_count") or 0)},
                    {"label": "Actors", "value": len(actor_items)},
                    {"label": "Support Grants", "value": len(grant_items)},
                ],
            },
            {
                "type": "risk_panel",
                "title": f"Severity profile for {window_label}",
                "items": risk_items,
            },
            {
                "type": "ranked_list",
                "title": "Most active actors",
                "items": actor_items,
                "ordered_by": "count",
            },
            {
                "type": "timeline",
                "title": "Recent security events",
                "events": timeline_items,
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "audit", "endpoint_or_topic": "get_permission_security_activity", "freshness": "live"}],
        "permissions_checked": ["view_audit_logs"],
        "confidence": "high",
        "warnings": [],
    }


def _build_subscription_usage_insight(payload: dict[str, Any]) -> dict[str, Any]:
    features = payload.get("features") if isinstance(payload, dict) else []
    features = features if isinstance(features, list) else []
    rows = []
    risk_items = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        row = {
            "feature": str(feature.get("name") or feature.get("feature") or "Feature"),
            "usage": feature.get("usage"),
            "limit": "Unlimited" if feature.get("is_unlimited") else feature.get("limit_value"),
            "remaining": feature.get("remaining"),
            "status": str(feature.get("status") or ""),
        }
        rows.append(row)
        status = str(feature.get("status") or "")
        if status in {"at_limit", "near_limit", "usage_unavailable"}:
            risk_items.append(
                {
                    "label": row["feature"],
                    "severity": "high" if status == "at_limit" else "medium",
                    "description": status.replace("_", " "),
                }
            )
    subscription = payload.get("subscription") if isinstance(payload, dict) else {}
    subscription = subscription if isinstance(subscription, dict) else {}
    return {
        "kind": "insight_response",
        "summary": "Subscription usage and limits are ready.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": "Workspace subscription",
                "data": [
                    {"label": "Status", "value": str(subscription.get("status") or "none")},
                    {"label": "Plan", "value": str(((subscription.get("plan") or {}) if isinstance(subscription.get("plan"), dict) else {}).get("name") or "No active plan")},
                    {"label": "Tracked Features", "value": len(rows)},
                ],
            },
            {
                "type": "comparison_table",
                "title": "Usage against limits",
                "columns": ["feature", "usage", "limit", "remaining", "status"],
                "rows": rows,
            },
            {
                "type": "risk_panel",
                "title": "Usage risks",
                "items": risk_items or [{"label": "All tracked features", "severity": "low", "description": "No current limit warnings."}],
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "subscriptions", "endpoint_or_topic": "get_usage_and_limits", "freshness": "live"}],
        "permissions_checked": ["manage_workspace_subscription"],
        "confidence": "high",
        "warnings": [str(item) for item in (payload.get("warnings") or []) if str(item).strip()],
    }


def _build_audit_search_insight(payload: dict[str, Any], *, title: str = "Audit search results") -> dict[str, Any]:
    window_label = _payload_window_label(payload, fallback="the selected period")
    results = payload.get("results") if isinstance(payload, dict) else []
    results = results if isinstance(results, list) else []
    timeline_items = _audit_timeline_items([row for row in results if isinstance(row, dict)])
    source_counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source_service") or "unknown").strip() or "unknown"
        severity = str(row.get("severity") or "info").strip() or "info"
        source_counts[source] = source_counts.get(source, 0) + 1
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
    ranked_sources = [
        {"label": key, "value": value}
        for key, value in sorted(source_counts.items(), key=lambda item: item[1], reverse=True)
    ]
    risk_items = [
        {
            "label": severity.replace("_", " ").title(),
            "severity": "high" if severity in {"critical", "error", "high"} else "medium" if severity in {"warning", "warn"} else "low",
            "detail": f"{count} events in {window_label}",
            "next_action": "Review the matching audit events.",
        }
        for severity, count in sorted(severity_counts.items(), key=lambda item: item[1], reverse=True)[:4]
    ]
    return {
        "kind": "insight_response",
        "summary": f"{int(payload.get('count') or len(results))} audit events matched for {window_label}.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": title,
                "data": [
                    {"label": "Matches", "value": int(payload.get("count") or len(results))},
                    {"label": "Sources", "value": len(ranked_sources)},
                    {"label": "Severities", "value": len(severity_counts)},
                ],
            },
            {
                "type": "ranked_list",
                "title": "Most active source services",
                "items": ranked_sources,
                "ordered_by": "count",
            },
            {
                "type": "risk_panel",
                "title": "Severity mix",
                "items": risk_items,
            },
            {
                "type": "timeline",
                "title": f"Matching audit events for {window_label}",
                "events": timeline_items,
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "audit", "endpoint_or_topic": "search_events", "freshness": "live"}],
        "permissions_checked": ["view_audit_logs"],
        "confidence": "high",
        "warnings": [] if results else ["No audit events matched the requested window."],
    }


def _build_audit_timeline_insight(payload: dict[str, Any], *, title: str = "Audit event timeline") -> dict[str, Any]:
    timeline_rows = payload.get("timeline") if isinstance(payload, dict) else []
    timeline_rows = timeline_rows if isinstance(timeline_rows, list) else []
    events = _timeline_events([row for row in timeline_rows if isinstance(row, dict)])
    first_event = events[0] if events else {}
    return {
        "kind": "insight_response",
        "summary": f"{len(events)} audit timeline events are ready.",
        "widgets": [
            {
                "type": "metric_grid",
                "title": title,
                "data": [
                    {"label": "Timeline Events", "value": len(events)},
                    {"label": "Has Focus", "value": 1 if first_event else 0},
                ],
            },
            {
                "type": "entity_preview",
                "title": "Latest timeline event",
                "entity": {
                    "kind": "Audit Event",
                    "title": str(first_event.get("title") or "Audit event"),
                    "subtitle": str(first_event.get("detail") or ""),
                    "meta": [{"label": "Severity", "value": str(first_event.get("severity") or "info")}],
                },
            },
            {
                "type": "timeline",
                "title": title,
                "events": events,
            },
        ],
        "suggested_actions": [],
        "data_sources": [{"service": "audit", "endpoint_or_topic": "get_event_timeline", "freshness": "live"}],
        "permissions_checked": ["view_audit_logs"],
        "confidence": "high",
        "warnings": [] if events else ["No audit timeline events were returned."],
    }


def _build_access_denied_insight(*, summary: str, detail: str, permission: str, source: str) -> dict[str, Any]:
    return {
        "kind": "insight_response",
        "summary": summary,
        "widgets": [
            {
                "type": "risk_panel",
                "title": "Access required",
                "items": [
                    {
                        "label": permission,
                        "severity": "high",
                        "description": detail,
                    }
                ],
            }
        ],
        "suggested_actions": [],
        "data_sources": [{"service": source, "endpoint_or_topic": "permission_guard", "freshness": "live"}],
        "permissions_checked": [permission],
        "confidence": "high",
        "warnings": [detail],
    }


def _insight_window_tool_arguments(
    window: InsightTimeWindow,
    *,
    include_date: bool = False,
    include_date_range: bool = False,
    include_period_label: bool = False,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"days": int(window["days"])}
    if include_date:
        arguments["date"] = window["anchor_date"]
    if include_date_range:
        arguments["date_from"] = window["start_date"]
        arguments["date_to"] = window["end_date"]
    if include_period_label:
        arguments["period_label"] = window["label"]
    return arguments


def _with_window_label(payload: Any, *, window: InsightTimeWindow) -> dict[str, Any]:
    if isinstance(payload, dict):
        enriched = dict(payload)
        enriched.setdefault("_window_label", window["label"])
        enriched.setdefault("_window_start_date", window["start_date"])
        enriched.setdefault("_window_end_date", window["end_date"])
        enriched.setdefault("_window_period", window["period"])
        return enriched
    return {
        "_window_label": window["label"],
        "_window_start_date": window["start_date"],
        "_window_end_date": window["end_date"],
        "_window_period": window["period"],
    }


async def _users_named_insight_payload(
    *,
    insight_key: str,
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
    user_text: str = "",
) -> dict[str, Any] | None:
    user_window = _resolve_insight_time_window(user_text, default_days=30, default_label="last 30 days")
    if insight_key == "staff_activity":
        output = await tool_executor.call_tool(
            name="audit.get_staff_activity",
            arguments={
                **_insight_window_tool_arguments(
                    user_window,
                    include_date=True,
                    include_date_range=True,
                    include_period_label=True,
                ),
                "limit": 50,
            },
            ctx=tool_ctx,
        )
        return _build_staff_activity_insight(_with_window_label(output, window=user_window))
    if insight_key == "support_access_audit":
        output = await tool_executor.call_tool(
            name="audit.get_permission_security_activity",
            arguments={
                **_insight_window_tool_arguments(
                    user_window,
                    include_date=True,
                    include_date_range=True,
                    include_period_label=True,
                ),
                "support_access_only": True,
                "limit": 50,
            },
            ctx=tool_ctx,
        )
        return _build_permission_security_insight(_with_window_label(output, window=user_window), support_access_only=True)
    if insight_key == "permission_security_activity":
        output = await tool_executor.call_tool(
            name="audit.get_permission_security_activity",
            arguments={
                **_insight_window_tool_arguments(
                    user_window,
                    include_date=True,
                    include_date_range=True,
                    include_period_label=True,
                ),
                "limit": 50,
            },
            ctx=tool_ctx,
        )
        return _build_permission_security_insight(_with_window_label(output, window=user_window), support_access_only=False)
    if insight_key == "audit_search":
        output = await tool_executor.call_tool(
            name="audit.search_events",
            arguments={
                "occurred_from": user_window["start_date"],
                "occurred_to": user_window["end_date"],
                "period_label": user_window["label"],
                "limit": 50,
            },
            ctx=tool_ctx,
        )
        return _build_audit_search_insight(_with_window_label(output if isinstance(output, dict) else {}, window=user_window))
    if insight_key == "audit_timeline":
        output = await tool_executor.call_tool(
            name="audit.get_event_timeline",
            arguments={"search": user_text, "limit": 100},
            ctx=tool_ctx,
        )
        return _build_audit_timeline_insight(output if isinstance(output, dict) else {})
    if insight_key == "subscription_usage_limits":
        output = await tool_executor.call_tool(
            name="subscriptions.get_usage_and_limits",
            arguments={},
            ctx=tool_ctx,
        )
        return _build_subscription_usage_insight(output if isinstance(output, dict) else {})
    return None


async def _pos_admin_named_insight_payload(
    *,
    insight_key: str,
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
    user_text: str = "",
) -> dict[str, Any] | None:
    sales_window = _resolve_insight_time_window(user_text, default_days=1, default_label="today")
    trailing_window = _resolve_insight_time_window(user_text, default_days=7, default_label="last 7 days")

    async def _optional_product_lookup(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            output = await tool_executor.call_tool(
                name="product.get_variant_lookup",
                arguments=arguments,
                ctx=tool_ctx,
            )
        except Exception as exc:
            logger.warning("Optional product variant lookup failed for POS insight: %s", exc)
            return {}
        return output if isinstance(output, dict) else {}

    if insight_key == "variant_comparison":
        product_query = _extract_product_query_from_text(user_text)
        if not product_query:
            return {
                "kind": "insight_response",
                "summary": "I need a product name, barcode, SKU, or variant id to compare variants.",
                "widgets": [
                    {
                        "type": "action_form",
                        "title": "Choose product family to compare",
                        "fields": [
                            {
                                "name": "product_query",
                                "label": "Product name, barcode, SKU, or variant id",
                                "type": "text",
                                "required": True,
                            }
                        ],
                    }
                ],
                "suggested_actions": [],
                "data_sources": [],
                "permissions_checked": ["view_pos_reports", "read_products"],
                "confidence": "medium",
                "warnings": ["No product identifier was detected in the question."],
            }
        trend_window = _resolve_insight_time_window(user_text, default_days=365, default_label="last 1 year")
        group_by = "month" if trend_window["days"] > 120 else "week" if trend_window["days"] > 45 else "day"
        variant_payload = await _optional_product_lookup({"query": product_query, "limit": 10, "active_only": True})
        trend_output = await tool_executor.call_tool(
            name="pos.get_product_sales_trend",
            arguments={
                **_insight_window_tool_arguments(trend_window, include_date=True),
                "query": product_query,
                "group_by": group_by,
                "limit": 25,
                "include_series": True,
                "include_locations": False,
                "include_recent": False,
            },
            ctx=tool_ctx,
        )
        return _build_pos_variant_comparison_insight(
            _with_window_label(trend_output if isinstance(trend_output, dict) else {}, window=trend_window),
            variant_payload=variant_payload,
        )
    if insight_key == "product_comparison":
        product_queries = _extract_product_comparison_queries_from_text(user_text)
        if len(product_queries) < 2:
            return {
                "kind": "insight_response",
                "summary": "I need at least two product names, barcodes, or SKUs to compare products.",
                "widgets": [
                    {
                        "type": "action_form",
                        "title": "Choose products to compare",
                        "fields": [
                            {
                                "name": "products",
                                "label": "Products to compare",
                                "type": "text",
                                "required": True,
                            }
                        ],
                    }
                ],
                "suggested_actions": [],
                "data_sources": [],
                "permissions_checked": ["view_pos_reports"],
                "confidence": "medium",
                "warnings": ["Fewer than two product identifiers were detected in the question."],
            }
        trend_window = _resolve_insight_time_window(user_text, default_days=365, default_label="last 1 year")
        group_by = "month" if trend_window["days"] > 120 else "week" if trend_window["days"] > 45 else "day"
        comparison_payloads: list[dict[str, Any]] = []
        for product_query in product_queries[:5]:
            trend_output = await tool_executor.call_tool(
                name="pos.get_product_sales_trend",
                arguments={
                    **_insight_window_tool_arguments(trend_window, include_date=True),
                    "query": product_query,
                    "group_by": group_by,
                    "limit": 10,
                    "include_series": False,
                    "include_locations": False,
                    "include_recent": False,
                },
                ctx=tool_ctx,
            )
            comparison_payloads.append(_with_window_label(trend_output if isinstance(trend_output, dict) else {}, window=trend_window))
        return _build_pos_product_comparison_insight(comparison_payloads, window=trend_window)
    if insight_key == "product_sales_trend":
        product_query = _extract_product_query_from_text(user_text)
        if not product_query:
            return {
                "kind": "insight_response",
                "summary": "I need a product name, barcode, SKU, or variant id to analyze product sales.",
                "widgets": [
                    {
                        "type": "action_form",
                        "title": "Choose product to analyze",
                        "fields": [
                            {
                                "name": "product_query",
                                "label": "Product barcode, SKU, or name",
                                "type": "text",
                                "required": True,
                            }
                        ],
                    }
                ],
                "suggested_actions": [],
                "data_sources": [],
                "permissions_checked": ["view_pos_reports", "read_products"],
                "confidence": "medium",
                "warnings": ["No product identifier was detected in the question."],
            }
        trend_window = _resolve_insight_time_window(user_text, default_days=365, default_label="last 1 year")
        group_by = "month" if trend_window["days"] > 120 else "week" if trend_window["days"] > 45 else "day"
        variant_payload = await _optional_product_lookup({"query": product_query, "limit": 1, "active_only": True})
        trend_output = await tool_executor.call_tool(
            name="pos.get_product_sales_trend",
            arguments={
                **_insight_window_tool_arguments(trend_window, include_date=True),
                "query": product_query,
                "group_by": group_by,
                "limit": 10,
            },
            ctx=tool_ctx,
        )
        return _build_pos_product_sales_trend_insight(
            _with_window_label(trend_output if isinstance(trend_output, dict) else {}, window=trend_window),
            variant_payload=variant_payload,
        )
    if insight_key == "sales_overview":
        output = await tool_executor.call_tool(
            name="pos.get_sales_summary",
            arguments={**_insight_window_tool_arguments(sales_window, include_date=True), "group_by": "location"},
            ctx=tool_ctx,
        )
        daily_output = await tool_executor.call_tool(
            name="pos.get_sales_summary",
            arguments={**_insight_window_tool_arguments(sales_window, include_date=True), "group_by": "day"},
            ctx=tool_ctx,
        )
        top_sellers_output = await tool_executor.call_tool(
            name="pos.get_top_sellers",
            arguments={**_insight_window_tool_arguments(sales_window, include_date=True), "limit": 5},
            ctx=tool_ctx,
        )
        top_sellers_output = await _enrich_top_seller_results_with_variant_context(
            results_payload=top_sellers_output if isinstance(top_sellers_output, dict) else {},
            tool_executor=tool_executor,
            tool_ctx=tool_ctx,
            limit=5,
        )
        return _build_pos_sales_overview_insight(
            _with_window_label(output, window=sales_window),
            top_sellers_payload=_with_window_label(top_sellers_output, window=sales_window),
            daily_sales_payload=_with_window_label(daily_output if isinstance(daily_output, dict) else {}, window=sales_window),
        )
    if insight_key == "sales_by_location_today":
        output = await tool_executor.call_tool(
            name="pos.get_sales_summary",
            arguments={**_insight_window_tool_arguments(sales_window, include_date=True), "group_by": "location"},
            ctx=tool_ctx,
        )
        return _build_pos_sales_by_location_insight(_with_window_label(output, window=sales_window))
    if insight_key == "top_sellers_seven_days":
        output = await tool_executor.call_tool(
            name="pos.get_top_sellers",
            arguments={**_insight_window_tool_arguments(trailing_window, include_date=True), "limit": 5},
            ctx=tool_ctx,
        )
        output = await _enrich_top_seller_results_with_variant_context(
            results_payload=output if isinstance(output, dict) else {},
            tool_executor=tool_executor,
            tool_ctx=tool_ctx,
            limit=5,
        )
        return _build_pos_top_sellers_insight(_with_window_label(output, window=trailing_window))
    if insight_key == "best_sales_day":
        output = await tool_executor.call_tool(
            name="pos.get_sales_summary",
            arguments={**_insight_window_tool_arguments(sales_window, include_date=True), "group_by": "day"},
            ctx=tool_ctx,
        )
        return _build_pos_best_sales_day_insight(_with_window_label(output if isinstance(output, dict) else {}, window=sales_window))
    if insight_key == "payment_mix":
        output = await tool_executor.call_tool(
            name="pos.get_pos_daily_summary",
            arguments={"date": sales_window["anchor_date"]},
            ctx=tool_ctx,
        )
        return _build_pos_payment_mix_insight(_with_window_label(output, window=sales_window))
    if insight_key == "terminal_cashier_activity":
        output = await tool_executor.call_tool(
            name="pos.get_terminal_activity",
            arguments={**_insight_window_tool_arguments(trailing_window, include_date=True), "limit": 10},
            ctx=tool_ctx,
        )
        return _build_pos_terminal_activity_insight(_with_window_label(output, window=trailing_window))
    if insight_key == "pos_audit_activity":
        output = await tool_executor.call_tool(
            name="audit.get_pos_activity",
            arguments={
                **_insight_window_tool_arguments(
                    trailing_window,
                    include_date=True,
                    include_date_range=True,
                    include_period_label=True,
                ),
                "limit": 50,
            },
            ctx=tool_ctx,
        )
        return _build_audit_entity_activity_insight(
            _with_window_label(output, window=trailing_window),
            summary="POS audit activity is ready.",
            title="POS audit activity",
            source_endpoint="get_pos_activity",
            preview_kind="POS",
        )
    if insight_key == "sessions_orders":
        summary_output = await tool_executor.call_tool(
            name="pos.get_pos_daily_summary",
            arguments={"date": sales_window["anchor_date"]},
            ctx=tool_ctx,
        )
        sales_output = await tool_executor.call_tool(
            name="pos.get_sales_summary",
            arguments={**_insight_window_tool_arguments(sales_window, include_date=True), "group_by": "location"},
            ctx=tool_ctx,
        )
        return _build_pos_sessions_orders_insight(
            _with_window_label(summary_output, window=sales_window),
            _with_window_label(sales_output, window=sales_window),
        )
    if insight_key == "pos_exceptions":
        summary_output = await tool_executor.call_tool(
            name="pos.get_pos_daily_summary",
            arguments={"date": sales_window["anchor_date"]},
            ctx=tool_ctx,
        )
        terminal_output = await tool_executor.call_tool(
            name="pos.get_terminal_activity",
            arguments={**_insight_window_tool_arguments(trailing_window, include_date=True), "limit": 10},
            ctx=tool_ctx,
        )
        return _build_pos_exceptions_insight(
            _with_window_label(summary_output, window=sales_window),
            _with_window_label(terminal_output, window=trailing_window),
        )
    return None


async def _inventory_visibility_named_insight_payload(
    *,
    insight_key: str,
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
    user_text: str = "",
) -> dict[str, Any] | None:
    movement_window = _resolve_insight_time_window(user_text, default_days=30, default_label="last 30 days")
    if insight_key in {"stock_risk_out_of_stock", "stock_risk_low_stock", "stock_risk"}:
        output = await tool_executor.call_tool(
            name="inventory.get_stock_risk",
            arguments={"limit": 12, "expiring_days": 30},
            ctx=tool_ctx,
        )
        focus_map = {
            "stock_risk_out_of_stock": "out_of_stock",
            "stock_risk_low_stock": "low_stock",
            "stock_risk": None,
        }
        return _build_inventory_stock_risk_insight(output if isinstance(output, dict) else {}, focus=focus_map[insight_key])
    if insight_key == "reorder_candidates":
        output = await tool_executor.call_tool(
            name="inventory.get_reorder_candidates",
            arguments={"limit": 12},
            ctx=tool_ctx,
        )
        rows = output.get("results") if isinstance(output, dict) and isinstance(output.get("results"), list) else []
        return _build_inventory_stock_risk_insight(
            {
                "summary": {
                    "out_of_stock_count": len([row for row in rows if isinstance(row, dict) and float(row.get("quantity_available") or row.get("quantity") or 0) <= 0]),
                    "reorder_count": int(output.get("count") or 0) if isinstance(output, dict) else len(rows),
                    "low_stock_count": 0,
                    "expiring_count": 0,
                },
                "risk_items": {"needs_reorder": rows},
            },
            focus="needs_reorder",
        )
    if insight_key == "stock_movements":
        output = await tool_executor.call_tool(
            name="inventory.get_stock_movements",
            arguments={
                "limit": 20,
                "date_from": movement_window["start_date"],
                "date_to": movement_window["end_date"],
            },
            ctx=tool_ctx,
        )
        return _build_inventory_movements_insight(output if isinstance(output, dict) else {})
    if insight_key == "location_health":
        output = await tool_executor.call_tool(
            name="inventory.search_stock_locations",
            arguments={"limit": 25},
            ctx=tool_ctx,
        )
        return _build_inventory_location_health_insight(output if isinstance(output, dict) else {})
    if insight_key == "stock_value_changes":
        analytics_output = await tool_executor.call_tool(
            name="inventory.get_stock_analytics",
            arguments={},
            ctx=tool_ctx,
        )
        movements_output = await tool_executor.call_tool(
            name="inventory.get_stock_movements",
            arguments={
                "limit": 25,
                "date_from": movement_window["start_date"],
                "date_to": movement_window["end_date"],
            },
            ctx=tool_ctx,
        )
        return _build_stock_value_change_insight(
            analytics_output if isinstance(analytics_output, dict) else {},
            movements_output if isinstance(movements_output, dict) else {},
            window_label=movement_window["label"],
        )
    if insight_key == "realtime_snapshot":
        snapshot_output = await tool_executor.call_tool(
            name="audit.get_realtime_dashboard_snapshot",
            arguments={},
            ctx=tool_ctx,
        )
        alerts_output = await tool_executor.call_tool(
            name="notifications.get_alert_summary",
            arguments={"limit": 10},
            ctx=tool_ctx,
        )
        return _build_realtime_dashboard_snapshot_insight(
            snapshot_output if isinstance(snapshot_output, dict) else {},
            alerts_output if isinstance(alerts_output, dict) else {},
        )
    if insight_key == "adjustment_risk":
        output = await tool_executor.call_tool(
            name="inventory.get_stock_movements",
            arguments={
                "limit": 20,
                "movement_type": "adjustment",
                "date_from": movement_window["start_date"],
                "date_to": movement_window["end_date"],
            },
            ctx=tool_ctx,
        )
        return _build_inventory_adjustment_risk_insight(output if isinstance(output, dict) else {})
    return None


async def _inventory_procurement_named_insight_payload(
    *,
    insight_key: str,
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
    user_text: str = "",
) -> dict[str, Any] | None:
    window = _resolve_insight_time_window(user_text, default_days=30, default_label="last 30 days")
    purchase_order_arguments = {
        "limit": 20,
        "date_from": window["start_date"],
        "date_to": window["end_date"],
    }
    audit_arguments = {**_insight_window_tool_arguments(window, include_date=True), "limit": 50}
    audit_arguments.update(
        _insight_window_tool_arguments(
            window,
            include_date_range=True,
            include_period_label=True,
        )
    )

    async def _safe_purchase_order_activity() -> dict[str, Any]:
        try:
            output = await tool_executor.call_tool(
                name="audit.get_purchase_order_activity",
                arguments=audit_arguments,
                ctx=tool_ctx,
            )
        except Exception:
            return {}
        return output if isinstance(output, dict) else {}

    if insight_key == "po_lifecycle":
        orders_output = await tool_executor.call_tool(
            name="inventory.search_purchase_orders",
            arguments=purchase_order_arguments,
            ctx=tool_ctx,
        )
        activity_output = await _safe_purchase_order_activity()
        order_rows = _purchase_order_results(orders_output if isinstance(orders_output, dict) else {})
        return _build_procurement_lifecycle_insight(
            {
                "status_counts": _purchase_order_status_counts(order_rows),
                "results": order_rows,
            },
            _with_window_label(activity_output, window=window),
        )
    if insight_key == "receiving_lifecycle":
        orders_output = await tool_executor.call_tool(
            name="inventory.search_purchase_orders",
            arguments=purchase_order_arguments,
            ctx=tool_ctx,
        )
        activity_output = await _safe_purchase_order_activity()
        return _build_procurement_receiving_insight(
            {"results": _purchase_order_results(orders_output if isinstance(orders_output, dict) else {})},
            _with_window_label(activity_output, window=window),
        )
    if insight_key == "supplier_performance":
        output = await tool_executor.call_tool(
            name="inventory.get_purchase_order_analytics",
            arguments={
                "date_from": window["start_date"],
                "date_to": window["end_date"],
            },
            ctx=tool_ctx,
        )
        return _build_procurement_supplier_insight(_with_window_label(output, window=window))
    if insight_key == "delay_exceptions":
        orders_output = await tool_executor.call_tool(
            name="inventory.search_purchase_orders",
            arguments=purchase_order_arguments,
            ctx=tool_ctx,
        )
        activity_output = await _safe_purchase_order_activity()
        return _build_procurement_delay_exception_insight(
            {"results": _purchase_order_results(orders_output if isinstance(orders_output, dict) else {})},
            _with_window_label(activity_output, window=window),
        )
    if insight_key == "cost_variance":
        output = await tool_executor.call_tool(
            name="inventory.get_purchase_order_analytics",
            arguments={
                "date_from": window["start_date"],
                "date_to": window["end_date"],
            },
            ctx=tool_ctx,
        )
        return _build_procurement_cost_variance_insight(_with_window_label(output, window=window))
    return None


async def _product_discovery_named_insight_payload(
    *,
    insight_key: str,
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
    user_text: str = "",
) -> dict[str, Any] | None:
    if insight_key == "product_audit_activity":
        window = _resolve_insight_time_window(user_text, default_days=30, default_label="last 30 days")
        output = await tool_executor.call_tool(
            name="audit.get_product_activity",
            arguments={**_insight_window_tool_arguments(window, include_date=True), "limit": 50},
            ctx=tool_ctx,
        )
        return _build_audit_entity_activity_insight(
            _with_window_label(output, window=window),
            summary="Product audit activity is ready.",
            title="Product audit activity",
            source_endpoint="get_product_activity",
            preview_kind="Product",
        )
    if insight_key == "import_opportunities":
        dashboard_output = await tool_executor.call_tool(
            name="product.get_product_dashboard_stats",
            arguments={},
            ctx=tool_ctx,
        )
        variant_output = await tool_executor.call_tool(
            name="product.search_product_variants",
            arguments={"limit": 12, "active_only": True},
            ctx=tool_ctx,
        )
        dashboard = dashboard_output.get("dashboard") if isinstance(dashboard_output, dict) else {}
        categories = dashboard.get("category_distribution") if isinstance(dashboard, dict) else []
        results: list[dict[str, Any]] = []
        if not results:
            results = [
                {
                    "name": str(item.get("product_name") or item.get("name") or "Catalog opportunity"),
                    "brand": "",
                    "category_name": "",
                    "variant_count": 1,
                    "already_imported": False,
                }
                for item in (variant_output.get("results") or [])
                if isinstance(item, dict)
            ]
        if not results and isinstance(categories, list):
            results = [
                {
                    "name": str(item.get("category_name") or "Category opportunity"),
                    "brand": "",
                    "category_name": str(item.get("category_name") or ""),
                    "variant_count": int(item.get("count") or 0),
                    "already_imported": False,
                }
                for item in categories
                if isinstance(item, dict)
            ]
        return _build_product_import_opportunities_insight({"count": len(results), "results": results})
    if insight_key == "variant_lookup":
        output = await tool_executor.call_tool(
            name="product.search_product_variants",
            arguments={"limit": 8, "active_only": True},
            ctx=tool_ctx,
        )
        return _build_variant_lookup_insight(output if isinstance(output, dict) else {})
    if insight_key == "catalog_gaps":
        dashboard_output = await tool_executor.call_tool(
            name="product.get_product_dashboard_stats",
            arguments={},
            ctx=tool_ctx,
        )
        variant_output = await tool_executor.call_tool(
            name="product.search_product_variants",
            arguments={"limit": 12, "active_only": True},
            ctx=tool_ctx,
        )
        alerts_output = await tool_executor.call_tool(
            name="product.get_product_stock_alerts",
            arguments={},
            ctx=tool_ctx,
        )
        return _build_catalog_gap_insight(
            dashboard_output if isinstance(dashboard_output, dict) else {},
            variant_output if isinstance(variant_output, dict) else {},
            alerts_output if isinstance(alerts_output, dict) else {},
        )
    if insight_key == "duplicate_codes":
        output = await tool_executor.call_tool(
            name="product.search_product_variants",
            arguments={"limit": 25, "active_only": False},
            ctx=tool_ctx,
        )
        return _build_duplicate_code_insight(output if isinstance(output, dict) else {})
    if insight_key == "media_category":
        dashboard_output = await tool_executor.call_tool(
            name="product.get_product_dashboard_stats",
            arguments={},
            ctx=tool_ctx,
        )
        variant_output = await tool_executor.call_tool(
            name="product.search_product_variants",
            arguments={"limit": 10, "active_only": True},
            ctx=tool_ctx,
        )
        return _build_product_media_category_insight(
            dashboard_output if isinstance(dashboard_output, dict) else {},
            variant_output if isinstance(variant_output, dict) else {},
        )
    return None


async def _host_named_insight_payload(
    *,
    insight_key: str,
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
    user_text: str = "",
) -> dict[str, Any] | None:
    sales_window = _resolve_insight_time_window(user_text, default_days=1, default_label="today")
    analyst_window = _resolve_insight_time_window(user_text, default_days=365, default_label="last 1 year")
    procurement_window = _resolve_insight_time_window(user_text, default_days=30, default_label="last 30 days")

    async def _safe_subscription_usage_payload() -> dict[str, Any]:
        try:
            output = await tool_executor.call_tool(
                name="subscriptions.get_usage_and_limits",
                arguments={},
                ctx=tool_ctx,
            )
        except Exception as exc:
            lowered_error = str(exc).strip().lower()
            if "owner" in lowered_error or "permission" in lowered_error or "forbidden" in lowered_error:
                return {}
            raise
        return output if isinstance(output, dict) else {}

    if insight_key.startswith("pos::"):
        return await _pos_admin_named_insight_payload(
            insight_key=insight_key.split("::", 1)[1],
            tool_executor=tool_executor,
            tool_ctx=tool_ctx,
            user_text=user_text,
        )
    if insight_key.startswith("inventory_visibility::"):
        return await _inventory_visibility_named_insight_payload(
            insight_key=insight_key.split("::", 1)[1],
            tool_executor=tool_executor,
            tool_ctx=tool_ctx,
            user_text=user_text,
        )
    if insight_key.startswith("inventory_procurement::"):
        return await _inventory_procurement_named_insight_payload(
            insight_key=insight_key.split("::", 1)[1],
            tool_executor=tool_executor,
            tool_ctx=tool_ctx,
            user_text=user_text,
        )
    if insight_key.startswith("users::"):
        user_insight_key = insight_key.split("::", 1)[1]
        try:
            return await _users_named_insight_payload(
                insight_key=user_insight_key,
                tool_executor=tool_executor,
                tool_ctx=tool_ctx,
                user_text=user_text,
            )
        except Exception as exc:
            lowered_error = str(exc).strip().lower()
            if "owner" in lowered_error or "permission" in lowered_error or "forbidden" in lowered_error:
                if user_insight_key == "subscription_usage_limits":
                    return _build_access_denied_insight(
                        summary="Subscription usage is restricted.",
                        detail=str(exc).strip() or "Only the workspace owner can view subscription usage and limits.",
                        permission="manage_workspace_subscription",
                        source="subscriptions",
                    )
                return _build_access_denied_insight(
                    summary="Audit insight access is restricted.",
                    detail=str(exc).strip() or "You need audit visibility to view this insight.",
                    permission="view_audit_logs",
                    source="audit",
                )
            raise
    if insight_key.startswith("product_discovery::"):
        return await _product_discovery_named_insight_payload(
            insight_key=insight_key.split("::", 1)[1],
            tool_executor=tool_executor,
            tool_ctx=tool_ctx,
            user_text=user_text,
        )
    if insight_key == "business_analyst_review":
        async def call_optional_tool(name: str, arguments: dict[str, Any], *, timeout_s: float = 12.0) -> dict[str, Any]:
            try:
                direct_lookup = getattr(tool_executor, "_direct_executor_for_tool_name", None)
                direct_executor = direct_lookup(name) if callable(direct_lookup) else None
                executor = direct_executor or tool_executor
                output = await asyncio.wait_for(
                    executor.call_tool(name=name, arguments=arguments, ctx=tool_ctx),
                    timeout=timeout_s,
                )
            except Exception as exc:
                logger.warning("business_analyst_review optional tool failed name=%s error=%s", name, exc)
                return {}
            return output if isinstance(output, dict) else {}

        sales_by_day_output, sales_by_location_output, top_sellers_output = await asyncio.gather(
            call_optional_tool(
                "pos.get_sales_summary",
                {**_insight_window_tool_arguments(analyst_window, include_date=True), "group_by": "day"},
                timeout_s=14.0,
            ),
            call_optional_tool(
                "pos.get_sales_summary",
                {**_insight_window_tool_arguments(analyst_window, include_date=True), "group_by": "location"},
                timeout_s=14.0,
            ),
            call_optional_tool(
                "pos.get_top_sellers",
                {**_insight_window_tool_arguments(analyst_window, include_date=True), "limit": 8},
                timeout_s=14.0,
            ),
        )
        top_sellers_output = await _enrich_top_seller_results_with_variant_context(
            results_payload=top_sellers_output if isinstance(top_sellers_output, dict) else {},
            tool_executor=tool_executor,
            tool_ctx=tool_ctx,
            limit=8,
        )
        stock_output, orders_output, subscription_output = await asyncio.gather(
            call_optional_tool("inventory.get_stock_risk", {"limit": 12, "expiring_days": 30}, timeout_s=8.0),
            call_optional_tool(
                "inventory.search_purchase_orders",
                {
                    "limit": 50,
                    "date_from": analyst_window["start_date"],
                    "date_to": analyst_window["end_date"],
                },
                timeout_s=8.0,
            ),
            asyncio.wait_for(_safe_subscription_usage_payload(), timeout=8.0),
            return_exceptions=True,
        )
        stock_output = stock_output if isinstance(stock_output, dict) else {}
        orders_output = orders_output if isinstance(orders_output, dict) else {}
        subscription_output = subscription_output if isinstance(subscription_output, dict) else {}
        order_rows = _purchase_order_results(orders_output)
        return _build_host_business_analyst_insight(
            _with_window_label(sales_by_day_output, window=analyst_window),
            _with_window_label(sales_by_location_output, window=analyst_window),
            _with_window_label(top_sellers_output if isinstance(top_sellers_output, dict) else {}, window=analyst_window),
            stock_output,
            {"status_counts": _purchase_order_status_counts(order_rows)},
            subscription_output,
        )
    if insight_key == "cross_domain_ops":
        sales_output = await tool_executor.call_tool(
            name="pos.get_sales_summary",
            arguments={**_insight_window_tool_arguments(sales_window, include_date=True), "group_by": "location"},
            ctx=tool_ctx,
        )
        stock_output = await tool_executor.call_tool(
            name="inventory.get_stock_risk",
            arguments={"limit": 12, "expiring_days": 30},
            ctx=tool_ctx,
        )
        orders_output = await tool_executor.call_tool(
            name="inventory.search_purchase_orders",
            arguments={
                "limit": 20,
                "date_from": procurement_window["start_date"],
                "date_to": procurement_window["end_date"],
            },
            ctx=tool_ctx,
        )
        order_rows = _purchase_order_results(orders_output if isinstance(orders_output, dict) else {})
        subscription_output = await _safe_subscription_usage_payload()
        return _build_host_cross_domain_insight(
            _with_window_label(sales_output, window=sales_window),
            stock_output if isinstance(stock_output, dict) else {},
            {"status_counts": _purchase_order_status_counts(order_rows)},
            subscription_output,
        )
    if insight_key == "location_comparison":
        sales_output = await tool_executor.call_tool(
            name="pos.get_sales_summary",
            arguments={**_insight_window_tool_arguments(sales_window, include_date=True), "group_by": "location"},
            ctx=tool_ctx,
        )
        location_output = await tool_executor.call_tool(
            name="inventory.search_stock_locations",
            arguments={"limit": 25},
            ctx=tool_ctx,
        )
        return _build_host_location_comparison_insight(
            _with_window_label(sales_output, window=sales_window),
            location_output if isinstance(location_output, dict) else {},
        )
    if insight_key == "recommendations":
        stock_output = await tool_executor.call_tool(
            name="inventory.get_stock_risk",
            arguments={"limit": 12, "expiring_days": 30},
            ctx=tool_ctx,
        )
        orders_output = await tool_executor.call_tool(
            name="inventory.search_purchase_orders",
            arguments={
                "limit": 20,
                "date_from": procurement_window["start_date"],
                "date_to": procurement_window["end_date"],
            },
            ctx=tool_ctx,
        )
        subscription_output = await _safe_subscription_usage_payload()
        order_rows = _purchase_order_results(orders_output if isinstance(orders_output, dict) else {})
        return _build_host_recommendations_insight(
            stock_output if isinstance(stock_output, dict) else {},
            {"status_counts": _purchase_order_status_counts(order_rows)},
            subscription_output,
        )
    return None


def _parse_pos_admin_prefill_from_text(action: str, text: str) -> dict[str, Any]:
    prefill: dict[str, Any] = {}
    if action == "create_pos_terminal":
        terminal_name = _extract_first_named_value(
            text,
            (
                r"(?:^|\n)\s*(?:pos\s+)?terminal\s+name\s*[:=-]\s*(?P<value>[^\n]+)",
                r"\b(?:create|add|new)\s+(?:a\s+)?(?:pos\s+)?terminal\s+(?:called|named)\s+(?P<value>[^,\n.]+)",
            ),
        )
        if terminal_name:
            prefill["name"] = terminal_name
            if "showroom" in _normalize_user_text(terminal_name):
                prefill["location"] = "Showroom Floor"
        location = _extract_first_named_value(
            text,
            (
                r"(?:^|\n)\s*location\s*[:=-]\s*(?P<value>[^\n]+)",
            ),
        )
        if location:
            prefill["location"] = location
        prefill["is_active"] = True
        return prefill
    if action == "create_pos_discount":
        discount_name = _extract_first_named_value(
            text,
            (
                r"(?:^|\n)\s*(?:pos\s+)?discount\s+name\s*[:=-]\s*(?P<value>[^\n]+)",
                r"\b(?:create|add|new)\s+(?:a\s+)?(?:pos\s+)?discount\s+(?:called|named)\s+(?P<value>[^,\n.]+)",
            ),
        )
        if discount_name:
            prefill["name"] = discount_name
        discount_value = _extract_first_named_value(
            text,
            (
                r"(?:^|\n)\s*(?:discount\s+)?value\s*[:=-]\s*(?P<value>\d+(?:\.\d+)?)",
                r"\b(?:discount|value)\s+(?P<value>\d+(?:\.\d+)?)\b",
            ),
        )
        if discount_value:
            prefill["value"] = discount_value
        normalized = _normalize_user_text(text)
        prefill["discount_type"] = "fixed" if "fixed" in normalized else "percentage"
        prefill["is_active"] = True
        return prefill
    return prefill


async def _pos_admin_dynamic_form_payload(
    *,
    action: str,
    text: str,
) -> dict[str, Any] | None:
    prefill = _parse_pos_admin_prefill_from_text(action, text)
    if action == "create_pos_terminal":
        return _with_interaction_metadata(
            {
                "interaction_type": "dynamic_form",
                "title": "Create POS Terminal",
                "description": "Confirm the POS terminal details before I create it.",
                "fields": [
                    {"name": "name", "type": "text", "label": "Terminal Name", "required": True, "placeholder": "Showroom Counter 1"},
                    {"name": "location", "type": "text", "label": "Location", "required": False, "placeholder": "Showroom Floor"},
                    {"name": "is_active", "type": "boolean", "label": "Active", "required": False},
                ],
                "current_values": {
                    key: value
                    for key, value in {
                        "name": prefill.get("name"),
                        "location": prefill.get("location"),
                        "is_active": prefill.get("is_active"),
                    }.items()
                    if value not in (None, "", [], {})
                },
            },
            workflow="pos_admin_mutation",
            workflow_stage="form",
            mutation_action=action,
        )
    if action == "create_pos_discount":
        return _with_interaction_metadata(
            {
                "interaction_type": "dynamic_form",
                "title": "Create POS Discount",
                "description": "Confirm the POS discount details before I create it.",
                "fields": [
                    {"name": "name", "type": "text", "label": "Discount Name", "required": True, "placeholder": "Launch Weekend Discount"},
                    {"name": "discount_type", "type": "select", "label": "Discount Type", "required": True, "options": [{"value": "percentage", "label": "Percentage"}, {"value": "fixed", "label": "Fixed Amount"}], "placeholder": "Select a discount type"},
                    {"name": "value", "type": "text", "label": "Discount Value", "required": True, "placeholder": "10"},
                    {"name": "is_active", "type": "boolean", "label": "Active", "required": False},
                ],
                "current_values": {
                    key: value
                    for key, value in {
                        "name": prefill.get("name"),
                        "discount_type": prefill.get("discount_type"),
                        "value": prefill.get("value"),
                        "is_active": prefill.get("is_active"),
                    }.items()
                    if value not in (None, "", [], {})
                },
            },
            workflow="pos_admin_mutation",
            workflow_stage="form",
            mutation_action=action,
        )
    return None


async def _pos_admin_prepare_execution(
    *,
    action: str,
    form_data: dict[str, Any],
    tool_specs: list[ToolSpec],
    tool_executor: ToolExecutor,
    tool_ctx: ToolContext,
) -> tuple[str, dict[str, Any], list[str]]:
    if action == "create_pos_discount":
        spec = _tool_spec_by_name(tool_specs, "pos.create_pos_discount")
        payload_spec = _nested_object_tool_spec(spec, "payload")
        payload_arguments: dict[str, Any] = {}
        _set_schema_arg(payload_arguments, payload_spec, ["name"], str(form_data.get("name") or "").strip() or None)
        discount_type = _normalize_user_text(str(form_data.get("discount_type") or ""))
        if discount_type in {"fixed amount", "fixed_price", "fixed_amount"}:
            discount_type = "fixed"
        elif not discount_type:
            discount_type = ""
        _set_schema_arg(payload_arguments, payload_spec, ["discount_type", "discountType"], discount_type or None)
        _set_schema_arg(payload_arguments, payload_spec, ["value"], str(form_data.get("value") or "").strip() or None)
        if "is_active" in form_data:
            _set_schema_arg(payload_arguments, payload_spec, ["is_active", "isActive"], bool(form_data.get("is_active")))
        arguments = {"payload": payload_arguments} if payload_arguments else {}
        filtered = _filtered_tool_arguments(spec, arguments)
        return "pos.create_pos_discount", filtered, _missing_required_arguments(spec, filtered)
    if action != "create_pos_terminal":
        return "", {}, ["unsupported_action"]
    spec = _tool_spec_by_name(tool_specs, "pos.create_pos_terminal")
    payload_spec = _nested_object_tool_spec(spec, "payload")
    configuration_id: str | None = None

    get_config_spec = _tool_spec_by_name(tool_specs, "pos.get_pos_configuration")
    if get_config_spec is not None:
        try:
            config_output = await tool_executor.call_tool(
                name="pos.get_pos_configuration",
                arguments={},
                ctx=tool_ctx,
            )
        except Exception:
            config_output = None
        config_mapping = _coerce_mapping_from_tool_output(config_output)
        if isinstance(config_mapping, dict):
            configuration_payload = config_mapping.get("configuration")
            if isinstance(configuration_payload, dict):
                configuration_id = _first_string(configuration_payload, ["id"])

    config_spec = _tool_spec_by_name(tool_specs, "pos.create_pos_configuration")
    if not configuration_id and config_spec is not None:
        config_payload_spec = _nested_object_tool_spec(config_spec, "payload")
        config_payload: dict[str, Any] = {}
        _set_schema_arg(config_payload, config_payload_spec, ["name"], "Electronics Showroom POS")
        _set_schema_arg(config_payload, config_payload_spec, ["currency"], "NGN")
        config_args = _filtered_tool_arguments(config_spec, {"payload": config_payload})
        try:
            config_output = await tool_executor.call_tool(
                name="pos.create_pos_configuration",
                arguments=config_args,
                ctx=tool_ctx,
            )
        except Exception:
            config_output = None
        config_mapping = _coerce_mapping_from_tool_output(config_output)
        if isinstance(config_mapping, dict):
            configuration_payload = config_mapping.get("configuration")
            if isinstance(configuration_payload, dict):
                configuration_id = _first_string(configuration_payload, ["id"])

    payload_arguments: dict[str, Any] = {}
    _set_schema_arg(payload_arguments, payload_spec, ["name"], str(form_data.get("name") or "").strip() or None)
    _set_schema_arg(payload_arguments, payload_spec, ["location"], str(form_data.get("location") or "").strip() or None)
    if "is_active" in form_data:
        _set_schema_arg(payload_arguments, payload_spec, ["is_active", "isActive"], bool(form_data.get("is_active")))
    _set_schema_arg(payload_arguments, payload_spec, ["configuration_id", "configurationId"], configuration_id)
    arguments = {"payload": payload_arguments} if payload_arguments else {}
    filtered = _filtered_tool_arguments(spec, arguments)
    return "pos.create_pos_terminal", filtered, _missing_required_arguments(spec, filtered)


def _normalize_operation_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


def _normalized_schema_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _tool_spec_by_name(tool_specs: list[ToolSpec], name: str) -> ToolSpec | None:
    for spec in tool_specs:
        if spec.name == name:
            return spec
    return None


def _resolve_schema_fragment(root_schema: dict[str, Any], fragment: Any) -> dict[str, Any]:
    if not isinstance(root_schema, dict) or not isinstance(fragment, dict):
        return {}

    current: dict[str, Any] = fragment
    seen_refs: set[str] = set()
    for _ in range(8):
        ref = current.get("$ref")
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen_refs:
            break
        seen_refs.add(ref)
        resolved: Any = root_schema
        for raw_part in ref[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if not isinstance(resolved, dict) or part not in resolved:
                resolved = None
                break
            resolved = resolved[part]
        if not isinstance(resolved, dict):
            break
        merged = dict(resolved)
        for key, value in current.items():
            if key == "$ref":
                continue
            merged[key] = value
        current = merged
    return current


def _tool_schema_properties(spec: ToolSpec | None) -> dict[str, Any]:
    if spec is None or not isinstance(spec.input_schema, dict):
        return {}
    schema = _resolve_schema_fragment(spec.input_schema, spec.input_schema)
    properties = schema.get("properties")
    return properties if isinstance(properties, dict) else {}


def _tool_schema_required(spec: ToolSpec | None) -> list[str]:
    if spec is None or not isinstance(spec.input_schema, dict):
        return []
    schema = _resolve_schema_fragment(spec.input_schema, spec.input_schema)
    required = schema.get("required")
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
    root_schema = spec.input_schema if spec is not None and isinstance(spec.input_schema, dict) else {}
    nested_schema = _resolve_schema_fragment(root_schema, nested)
    nested_properties = nested.get("properties")
    nested_required = nested.get("required")
    if isinstance(nested_schema, dict):
        nested_properties = nested_schema.get("properties")
        nested_required = nested_schema.get("required")
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
    description = f"{description}\n\nChoose whether to resume it or start a new product import flow."
    return {
        "title": "Resume Product Import",
        "description": description,
        "options": [
            {"value": "resume_saved", "label": "Resume Saved Import"},
            {"value": "start_over", "label": "Start Over"},
            {"value": "cancel_saved", "label": "Cancel Saved Import"},
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
            {"value": "cancel_onboarding", "label": "Cancel For Now"},
        ],
        "multiple": False,
        "allow_input": True,
    }


def _onboarding_completed_text(created_operations: dict[str, Any]) -> str:
    counts = {
        "category": 0,
        "catalog_page": 0,
        "review": 0,
        "product": 0,
    }
    for payload in created_operations.values():
        if not isinstance(payload, dict):
            continue
        operation_type = str(payload.get("operation_type") or "").strip()
        if operation_type in counts:
            counts[operation_type] += 1

    parts: list[str] = []
    if counts["category"]:
        parts.append(f"{counts['category']} category selection" + ("s" if counts["category"] != 1 else ""))
    if counts["catalog_page"]:
        parts.append(f"{counts['catalog_page']} catalog page review" + ("s" if counts["catalog_page"] != 1 else ""))
    if counts["review"]:
        parts.append(f"{counts['review']} import review" + ("s" if counts["review"] != 1 else ""))
    if counts["product"]:
        parts.append(f"{counts['product']} product" + ("s" if counts["product"] != 1 else ""))

    if not parts:
        return "No product import records were created."
    if len(parts) == 1:
        return f"Created {parts[0]} for product import."
    return "Created " + ", ".join(parts[:-1]) + f", and {parts[-1]} for product import."


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
    candidates: list[Any] = [payload]
    normalized = _coerce_mapping_from_tool_output(payload)
    if normalized is not None and normalized is not payload:
        candidates.append(normalized)

    for candidate in candidates:
        current = candidate
        for part in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
            if current in (None, "", [], {}):
                break
        if current not in (None, "", [], {}):
            return current

        if path and path[-1] == "id" and isinstance(candidate, dict):
            direct_id = candidate.get("id")
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


def _resolve_created_result_refs_lenient(value: Any, created_map: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, dict):
        if "__ka2a_created_ref__" in value:
            try:
                return _resolve_created_result_ref_value(value, created_map)
            except (KeyError, ValueError):
                return None
        return {
            key: _resolve_created_result_refs_lenient(item, created_map)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_created_result_refs_lenient(item, created_map) for item in value]
    return value


def _compact_nested_arguments(value: Any) -> Any:
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for key, item in value.items():
            if item is None:
                continue
            nested = _compact_nested_arguments(item)
            if nested is None:
                continue
            compacted[key] = nested
        return compacted
    if isinstance(value, list):
        compacted_list = [_compact_nested_arguments(item) for item in value]
        return [item for item in compacted_list if item is not None]
    return value


def _operation_duplicate_lookup_config(operation_type: str) -> tuple[str, str] | None:
    mapping = {
        "stock_location": ("inventory.search_stock_locations", "location"),
        "inventory_category": ("inventory.list_inventory_categories", "category"),
        "inventory": ("inventory.search_inventory_items", "inventory_item"),
        "product": ("product.search_products", "product"),
    }
    return mapping.get(operation_type)


def _operation_duplicate_lookup_name(arguments: dict[str, Any]) -> str | None:
    payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else None
    candidate = payload if payload is not None else arguments
    if not isinstance(candidate, dict):
        return None
    return _first_string(
        candidate,
        [
            "name",
            "title",
            "location_name",
            "locationName",
            "category_name",
            "categoryName",
            "inventory_name",
            "inventoryName",
            "product_name",
            "productName",
        ],
    )


def _error_indicates_duplicate_conflict(error_text: str | None) -> bool:
    lowered = str(error_text or "").strip().lower()
    if not lowered:
        return False
    return (
        "duplicate key value violates unique constraint" in lowered
        or "already exists" in lowered
        or "unique_name_profile" in lowered
    )


async def _recover_duplicate_onboarding_operation(
    *,
    operation: dict[str, Any],
    resolved_arguments: dict[str, Any],
    tool_executor: Any,
    tool_ctx: Any,
    lookup_cache: dict[tuple[str, tuple[tuple[str, Any], ...]], Any] | None = None,
) -> dict[str, Any] | None:
    operation_type = str(operation.get("operation_type") or "").strip()
    lookup_config = _operation_duplicate_lookup_config(operation_type)
    if lookup_config is None:
        return None

    lookup_tool, result_key = lookup_config
    lookup_name = _operation_duplicate_lookup_name(resolved_arguments)
    if not lookup_name:
        return None

    lookup_arguments: dict[str, Any] = {"limit": 25}
    if "search_" in lookup_tool:
        lookup_arguments["query"] = lookup_name

    cache_key = (lookup_tool, tuple(sorted(lookup_arguments.items())))
    if lookup_cache is not None and cache_key in lookup_cache:
        output = lookup_cache[cache_key]
    else:
        try:
            output = await tool_executor.call_tool(
                name=lookup_tool,
                arguments=lookup_arguments,
                ctx=tool_ctx,
            )
        except Exception:
            return None
        if lookup_cache is not None:
            lookup_cache[cache_key] = output

    normalized_target = _normalize_relation_token(lookup_name)
    for item in _relation_items_from_lookup_output(lookup_tool, output):
        if not isinstance(item, dict):
            continue
        label = _first_string(
            item,
            [
                "name",
                "title",
                "label",
                "inventory_item_name",
                "stock_location_name",
                "category",
            ],
        )
        identifier = _first_string(item, ["id", "uuid", "value"])
        if not label or not identifier:
            continue
        if _normalize_relation_token(label) != normalized_target:
            continue
        return {
            "label": operation.get("label"),
            "tool_name": str(operation.get("tool_name") or "").strip(),
            "operation_type": operation_type,
            "arguments": resolved_arguments,
            "result": {result_key: item},
            "reused_existing": True,
        }
    return None


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


async def _execute_onboarding_plan_operations(
    *,
    selected_scope: str,
    onboarding_data: dict[str, Any],
    tool_specs: list[ToolSpec],
    company_context: dict[str, Any] | None,
    existing_created_map: dict[str, dict[str, Any]] | None = None,
    tool_executor: Any,
    tool_ctx: Any,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], bool]:
    planned_operations = _onboarding_plan_operations(
        scope=selected_scope,
        onboarding_data=onboarding_data,
        tool_specs=tool_specs,
        company_context=company_context,
    )
    created_map: dict[str, dict[str, Any]] = {
        key: value
        for key, value in (existing_created_map or {}).items()
        if isinstance(value, dict)
    }
    failed_items: list[dict[str, Any]] = []
    any_tool_executed = False
    duplicate_lookup_cache: dict[tuple[str, tuple[tuple[str, Any], ...]], Any] = {}

    for operation in planned_operations:
        semantic_key = str(operation.get("semantic_key") or "").strip()
        tool_name = str(operation.get("tool_name") or "").strip()
        if not semantic_key or not tool_name or semantic_key in created_map:
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
            preexisting_entry = await _recover_duplicate_onboarding_operation(
                operation=operation,
                resolved_arguments=resolved_arguments,
                tool_executor=tool_executor,
                tool_ctx=tool_ctx,
                lookup_cache=duplicate_lookup_cache,
            )
            if preexisting_entry is not None:
                created_map[semantic_key] = preexisting_entry
                continue
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
            raw_arguments = operation.get("arguments") if isinstance(operation.get("arguments"), dict) else {}
            lenient_arguments = _compact_nested_arguments(
                _resolve_created_result_refs_lenient(raw_arguments, created_map)
            )
            if isinstance(lenient_arguments, dict) and lenient_arguments and lenient_arguments != raw_arguments:
                try:
                    output = await tool_executor.call_tool(
                        name=tool_name,
                        arguments=lenient_arguments,
                        ctx=tool_ctx,
                    )
                    any_tool_executed = True
                    created_map[semantic_key] = {
                        "label": operation.get("label"),
                        "tool_name": tool_name,
                        "operation_type": operation.get("operation_type"),
                        "arguments": lenient_arguments,
                        "result": output if isinstance(output, dict) else {"value": str(output)},
                    }
                    continue
                except Exception as retry_exc:
                    failed_items.append(
                        _annotate_failed_operation(
                            {
                                "label": operation.get("label"),
                                "tool_name": tool_name,
                                "reason": "tool_error",
                                "error": str(retry_exc),
                            }
                        )
                    )
                    continue
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
            if _error_indicates_duplicate_conflict(str(exc)):
                recovered_entry = await _recover_duplicate_onboarding_operation(
                    operation=operation,
                    resolved_arguments=resolved_arguments if isinstance(locals().get("resolved_arguments"), dict) else {},
                    tool_executor=tool_executor,
                    tool_ctx=tool_ctx,
                    lookup_cache=duplicate_lookup_cache,
                )
                if recovered_entry is not None:
                    created_map[semantic_key] = recovered_entry
                    continue
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

    return created_map, failed_items, any_tool_executed


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


def _is_marketplace_results_payload(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return str(payload.get("interaction_type") or "").strip().lower() == "marketplace_results"


def _is_marketplace_results_response(response: dict[str, Any] | None) -> bool:
    if not isinstance(response, dict):
        return False
    return str(response.get("type") or "").strip().lower() == "marketplace_results_response"


def _marketplace_response_action(response: dict[str, Any] | None) -> str | None:
    if not isinstance(response, dict):
        return None
    action = str(response.get("action") or "").strip().lower()
    return action or None


def _marketplace_response_selected_items(response: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        return []
    items = response.get("selected_items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _is_actionable_marketplace_search_query(value: str) -> bool:
    text = _normalize_user_text(value)
    if not text:
        return False
    marketplace_tokens = (
        "marketplace",
        "website",
        "websites",
        "china",
        "chinese",
        "taobao",
        "tmall",
        "jd",
        "jd.com",
        "1688",
        "amazon",
        "ebay",
        "alibaba",
        "aliexpress",
        "temu",
        "dhgate",
        "supplier",
        "sourcing",
    )
    shopping_tokens = (
        "search",
        "find",
        "look for",
        "source",
        "buy",
        "compare",
        "latest",
        "price",
        "cheap",
        "cheapest",
        "check",
        "browse",
    )
    has_marketplace_hint = any(token in text for token in marketplace_tokens)
    has_shopping_intent = any(token in text for token in shopping_tokens)
    if has_marketplace_hint and has_shopping_intent:
        return True
    return any(token in text for token in marketplace_tokens) or (
        "online" in text and any(token in text for token in shopping_tokens)
    )


def _marketplace_query_from_text(value: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    first_clause = re.split(r"[\n,;]+", raw, maxsplit=1)[0].strip()
    candidate = first_clause or raw
    match = re.search(
        r"(?:search|find|look for|source|compare|buy|check|browse)\s+(?:for\s+)?(?P<query>.+)$",
        candidate,
        flags=re.IGNORECASE,
    )
    if match:
        candidate = match.group("query").strip()
    candidate = re.sub(r"\bon\s+(?:chinese|china)\s+websites?\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bonline\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\b(?:chinese|china)\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\b(?:websites?|marketplaces?)\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bonline\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s+", " ", candidate).strip(" .,-")
    return candidate or None


def _marketplace_keys_from_text(value: str) -> list[str]:
    text = _normalize_user_text(value)
    if any(token in text for token in ("china", "chinese", "taobao", "tmall", "jd", "jd.com", "1688")):
        return ["alibaba", "aliexpress", "temu", "dhgate"]
    marketplaces = (
        ("amazon", "amazon"),
        ("ebay", "ebay"),
        ("aliexpress", "aliexpress"),
        ("alibaba", "alibaba"),
        ("temu", "temu"),
        ("dhgate", "dhgate"),
    )
    selected: list[str] = []
    for token, key in marketplaces:
        if token in text and key not in selected:
            selected.append(key)
    return selected


def _marketplace_search_arguments_from_text(value: str) -> dict[str, Any] | None:
    if not _is_actionable_marketplace_search_query(value):
        return None
    query = _marketplace_query_from_text(value)
    if not query:
        return None
    arguments: dict[str, Any] = {
        "query": query,
        "max_results": 10,
    }
    marketplaces = _marketplace_keys_from_text(value)
    if marketplaces:
        arguments["marketplaces"] = marketplaces
    return arguments


def _marketplace_selected_items_summary(items: list[dict[str, Any]]) -> str:
    labels = [
        str(item.get("title") or item.get("name") or "").strip()
        for item in items
        if str(item.get("title") or item.get("name") or "").strip()
    ]
    if not labels:
        return "I received the selected marketplace items."
    preview = ", ".join(labels[:4])
    if len(labels) > 4:
        preview = f"{preview}, and {len(labels) - 4} more"
    return (
        f"I received these marketplace items: {preview}. "
        "Ask me to compare them or tell me which ones you want to add to your inventory."
    )


def _infer_domain_agent_name(query: str) -> str | None:
    text = _normalize_user_text(query)
    if not text:
        return None

    strong_override = _strong_domain_agent_override(text)
    if strong_override:
        return strong_override

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
    available_names = _available_agent_names(agents)
    if _is_actionable_marketplace_search_query(query):
        if not available_names or "product" in available_names:
            return "product"

    inferred_agent = _infer_domain_agent_name(query)
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


def _select_router_specialist_agent(
    router_agent_name: str,
    query: str,
    agents: list[dict[str, Any]],
) -> str | None:
    available_names = _available_agent_names(agents)
    text = _normalize_user_text(query)
    if not text:
        return None

    def _pick(*candidates: str) -> str | None:
        for candidate in candidates:
            if not available_names or candidate in available_names:
                return candidate
        return None

    read_only_prefixes = (
        "list ",
        "show ",
        "search ",
        "find ",
        "what ",
        "which ",
        "get ",
        "display ",
        "summarize ",
        "summarise ",
        "inspect ",
        "check ",
    )
    is_read_query = text.startswith(read_only_prefixes)

    if router_agent_name == "inventory":
        if any(
            token in text
            for token in (
                "purchase order",
                "purchase orders",
                "receiv",
                "vendor",
                "procurement",
                "purchase return",
            )
        ):
            return _pick("inventory_procurement")
        if any(
            token in text
            for token in (
                "sales order",
                "sales orders",
                "return order",
                "return orders",
                "fulfillment",
                "shipment",
                "reserve stock",
                "stock reservation",
                "stock reservations",
                "transfer stock",
                "adjust inventory",
            )
        ):
            return _pick("inventory_fulfillment")
        if any(
            token in text
            for token in (
                "create stock location",
                "add stock location",
                "new stock location",
                "update stock location",
                "create inventory category",
                "update inventory category",
                "create inventory item",
                "update inventory item",
                "set up stock location",
                "setup stock location",
                "categorize inventory",
                "categorise inventory",
                "categorize inventories",
                "categorise inventories",
                "categorize my inventory",
                "categorise my inventory",
                "categorize my inventories",
                "categorise my inventories",
                "categorize inventory items",
                "categorise inventory items",
                "uncategorized inventory",
                "uncategorised inventory",
            )
        ):
            return _pick("inventory_setup")
        if any(
            token in text
            for token in (
                "inventory category",
                "inventory categories",
                "category tree",
                "category details",
                "category children",
            )
        ):
            return _pick("inventory_setup")
        if any(
            token in text
            for token in (
                "stock location",
                "stock locations",
                "warehouse",
                "low stock",
                "expiring",
                "expiry",
                "alert",
                "stock balance",
                "stock balances",
                "stock movement",
                "stock movements",
                "tracking history",
                "inventory posture",
                "stock analytics",
            )
        ):
            return _pick("inventory_visibility")
        if is_read_query and any(token in text for token in ("inventory item", "inventory items", "inventory ledger")):
            return _pick("inventory_visibility")

    if router_agent_name == "product":
        if (
            any(
                token in text
                for token in (
                    "marketplace",
                    "website",
                    "websites",
                    "china",
                    "chinese",
                    "taobao",
                    "tmall",
                    "jd",
                    "jd.com",
                    "1688",
                    "amazon",
                    "ebay",
                    "alibaba",
                    "aliexpress",
                    "temu",
                    "dhgate",
                    "compare offers",
                    "supplier",
                    "sourcing",
                    "online search",
                )
            )
            or (
                "online" in text
                and any(
                    token in text
                    for token in (
                        "search",
                        "buy",
                        "compare",
                        "latest",
                    )
                )
            )
        ):
            return _pick("marketplace_sourcing")
        if any(
            token in text
            for token in (
                "pricing rule",
                "pricing rules",
                "price history",
                "price trend",
                "price trends",
                "pricing strategy",
                "purchase price",
                "approve price",
                "reject price",
                "bulk update product prices",
            )
        ):
            return _pick("product_pricing")
        if any(
            token in text
            for token in (
                "featured",
                "quick sale",
                "pos visible",
                "attribute",
                "media",
                "attachment",
                "merchandising",
            )
        ):
            return _pick("product_merchandising")
        if any(
            token in text
            for token in (
                "create product",
                "update product",
                "delete product",
                "create variant",
                "update variant",
                "bulk update pos settings",
                "export product",
                "seed the catalog",
            )
        ):
            return _pick("product_catalog_admin")
        if is_read_query and any(
            token in text
            for token in (
                "search my catalog",
                "search products",
                "product details",
                "variant",
                "dashboard stats",
                "stock alerts",
                "product analytics",
                "catalog",
            )
        ):
            return _pick("product_discovery")

    if router_agent_name == "pos":
        if any(
            token in text
            for token in (
                "current pos session",
                "open pos session",
                "draft order",
                "held pos",
                "held cart",
                "checkout",
                "payment",
                "pos order",
                "cashier",
            )
        ):
            return _pick("pos_live")
        if any(
            token in text
            for token in (
                "pos configuration",
                "terminal",
                "table",
                "customer",
                "discount",
                "daily summary",
                "sales summary",
                "revenue",
                "gross sales",
                "sales made",
                "how many sales",
                "order count",
                "sales by location",
                "top sellers",
                "best sellers",
                "pos session details",
                "list pos sessions",
            )
        ):
            return _pick("pos_admin")

    return None


def _select_router_handoff_agent(router_agent_name: str, query: str, agents: list[dict[str, Any]]) -> str | None:
    # Router/domain agents are downstream of the host. They must never bounce a
    # delegated task back to host, or a single request can recurse until timeout.
    route_agents = [
        agent
        for agent in agents
        if _canonical_host_domain_agent(str(agent.get("name") or "")) != "host"
    ]
    if agents and not route_agents:
        return None

    available_names = _available_agent_names(route_agents)
    selected_specialist = _select_router_specialist_agent(router_agent_name, query, route_agents)
    if selected_specialist:
        return selected_specialist
    inferred_domain = _infer_domain_agent_name(query)
    if inferred_domain and inferred_domain != router_agent_name:
        handoff_preferences: dict[str, tuple[str, ...]] = {
            "onboarding": ("onboarding", "inventory"),
            "product": ("product",),
            "inventory": ("inventory",),
            "pos": ("pos",),
            "users": ("users",),
        }
        for candidate in handoff_preferences.get(inferred_domain, (inferred_domain,)):
            if not available_names or candidate in available_names:
                return candidate
        return None
    return _select_router_delegation_agent(query, route_agents)


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
    interaction_payload = _interaction_payload_from_parts(response_parts)
    if interaction_payload is not None:
        response_parts = _strip_placeholder_text_parts(response_parts)
    if (
        not response_text
        or response_text.lower() in {"working", "completed", "submitted"}
    ) and interaction_payload is not None:
        response_text = _interaction_payload_summary_text(interaction_payload) or response_text
    if not response_text and response_parts:
        response_text = _text_from_parts(response_parts)
    if not response_parts:
        response_parts = [TextPart(text="(no result)")]
        response_text = "(no result)"
    if interaction_payload is not None and delegated_final_state == TaskState.completed:
        delegated_final_state = TaskState.input_required
    if (
        interaction_payload is None
        and delegated_final_state == TaskState.completed
        and _plain_text_delegated_response_requires_confirmation(response_text)
    ):
        delegated_final_state = TaskState.input_required
    if not status_updates and interaction_payload is not None:
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


def _strip_placeholder_text_parts(parts: list[Any]) -> list[Any]:
    placeholders = {"working", "completed", "submitted"}
    filtered = [
        part
        for part in parts
        if not (
            isinstance(part, TextPart)
            and str(part.text or "").strip().lower() in placeholders
        )
    ]
    return filtered or parts


def _interaction_payload_summary_text(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    interaction_type = str(payload.get("interaction_type") or "").strip().lower()
    if interaction_type == "marketplace_results":
        query = str(payload.get("query") or "").strip()
        products = payload.get("products") if isinstance(payload.get("products"), list) else []
        count = len(products)
        if query and count:
            return f"Marketplace search found {count} result{'s' if count != 1 else ''} for {query}."
    if title and description:
        return f"{title}\n{description}"
    if title:
        return title
    if description:
        return description
    return None


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
    if "product" in lowered or "catalog" in lowered or "selection" in lowered or "review" in lowered or "matching" in lowered:
        return "product_onboarding"
    if "stock" in lowered and "location" in lowered:
        return "stock_locations"
    if "categor" in lowered:
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
        workflow="product_import",
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
        "product.get_product_pricing_rules",
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
        quantity_available = item.get("quantity_available")
        if category:
            description_parts.append(category)
        if quantity_available not in (None, ""):
            description_parts.append(f"Available: {quantity_available}")
    elif lookup_tool in {"inventory.list_stock_locations", "inventory.search_stock_locations"}:
        total_quantity = item.get("total_quantity")
        if total_quantity not in (None, ""):
            description_parts.append(f"Qty: {total_quantity}")
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
    if lookup_tool in {"inventory.list_stock_locations", "inventory.search_stock_locations"}:
        metadata: dict[str, Any] = {}
        structural_location_id = _first_string(item, ["structural_location_id"])
        structural_location_name = _first_string(item, ["structural_location_name"])
        if structural_location_id:
            metadata["structural_location_id"] = structural_location_id
        if structural_location_name:
            metadata["structural_location_name"] = structural_location_name
        if metadata:
            option["metadata"] = metadata
    return option


def _dynamic_form_option_metadata(
    interaction_payload: dict[str, Any] | None,
    *,
    field_name: str,
    selected_value: str | None,
) -> dict[str, Any]:
    target_value = str(selected_value or "").strip()
    if not target_value or not isinstance(interaction_payload, dict):
        return {}
    fields = interaction_payload.get("fields")
    if not isinstance(fields, list):
        return {}
    for field in fields:
        if not isinstance(field, dict):
            continue
        if str(field.get("name") or "").strip() != field_name:
            continue
        options = field.get("options")
        if not isinstance(options, list):
            return {}
        for option in options:
            if not isinstance(option, dict):
                continue
            if str(option.get("value") or "").strip() != target_value:
                continue
            metadata = option.get("metadata")
            return metadata if isinstance(metadata, dict) else {}
        return {}
    return {}


def _selected_structural_location_id_from_form(
    interaction_payload: dict[str, Any] | None,
    *,
    field_name: str,
    selected_value: str | None,
) -> str | None:
    metadata = _dynamic_form_option_metadata(
        interaction_payload,
        field_name=field_name,
        selected_value=selected_value,
    )
    return str(metadata.get("structural_location_id") or "").strip() or None


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
    source_text: str | None = None,
) -> dict[str, Any] | None:
    if not _relation_tool_error_needs_select_recovery(error_text):
        return None

    spec = _tool_spec_by_name(tool_specs, tool_name)
    if spec is None or not isinstance(spec.input_schema, dict):
        return None

    fields: list[dict[str, Any]] = []
    seen_field_names: set[str] = set()
    current_values: dict[str, Any] = {}

    preferred_queries: dict[str, str] = {}
    text_value = str(source_text or "").strip()
    if text_value:
        if tool_name == "inventory.create_stock_reservation":
            prefill = _parse_inventory_fulfillment_prefill_from_text("create_stock_reservation", text_value)
            if prefill.get("inventory_item_name"):
                preferred_queries["inventory_item_id"] = str(prefill["inventory_item_name"])
            if prefill.get("from_location_name"):
                preferred_queries["stock_location_id"] = str(prefill["from_location_name"])
        elif tool_name == "inventory.transfer_location_stock":
            prefill = _parse_inventory_fulfillment_prefill_from_text("transfer_location_stock", text_value)
            if prefill.get("inventory_item_name"):
                preferred_queries["inventory_item_id"] = str(prefill["inventory_item_name"])
            if prefill.get("from_location_name"):
                preferred_queries["from_location_id"] = str(prefill["from_location_name"])
            if prefill.get("to_location_name"):
                preferred_queries["to_location_id"] = str(prefill["to_location_name"])
        elif tool_name == "inventory.adjust_inventory_item_stock":
            prefill = _parse_inventory_fulfillment_prefill_from_text("adjust_inventory_item_stock", text_value)
            if prefill.get("inventory_item_name"):
                preferred_queries["inventory_item_id"] = str(prefill["inventory_item_name"])
            if prefill.get("from_location_name"):
                preferred_queries["stock_location_id"] = str(prefill["from_location_name"])

    for path, field_schema in _iter_schema_leaf_fields(spec.input_schema):
        relation_specs = _matching_relation_specs_for_texts(tool_specs, path, str(field_schema.get("description") or ""))
        if not relation_specs:
            continue
        field_name = path.split(".")[-1].strip()
        if not field_name or field_name in seen_field_names:
            continue
        relation_spec = dict(relation_specs[0])
        preferred_query = preferred_queries.get(field_name)
        lookup_tool = str(relation_spec.get("lookup_tool") or "").strip()
        if preferred_query and lookup_tool.startswith("inventory.list_"):
            search_lookup_tool = lookup_tool.replace("inventory.list_", "inventory.search_", 1)
            if _tool_spec_by_name(tool_specs, search_lookup_tool) is not None:
                relation_spec["lookup_tool"] = search_lookup_tool
        options = await _load_lookup_options_by_tool_name(
            str(relation_spec.get("lookup_tool") or "").strip(),
            tool_specs=tool_specs,
            tool_executor=tool_executor,
            tool_ctx=tool_ctx,
            preferred_query=preferred_query,
        )
        matched_value: Any = None
        if preferred_query:
            options, matched_value = await _ensure_lookup_option_for_name(
                str(relation_spec.get("lookup_tool") or "").strip(),
                desired_name=preferred_query,
                options=options,
                tool_executor=tool_executor,
                tool_ctx=tool_ctx,
            )
        if not options:
            continue
        seen_field_names.add(field_name)
        if matched_value not in (None, "", [], {}):
            current_values[field_name] = matched_value
        fields.append(
            {
                "name": field_name,
                "type": "select",
                "label": relation_spec["label"],
                "required": True,
                "options": options,
                "placeholder": f"Select {relation_spec['label']}",
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
        "current_values": current_values,
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

    def _strip_leading_tool_call_text(value: str) -> str:
        text = str(value or "")
        if not text:
            return text
        lines = text.splitlines()
        while lines:
            candidate = lines[0].strip()
            if not candidate:
                lines.pop(0)
                continue
            try:
                parsed = json.loads(candidate)
            except Exception:
                break
            if (
                isinstance(parsed, dict)
                and str(parsed.get("kind") or "").strip().lower() == "tool-call"
                and str(parsed.get("name") or "").strip()
            ):
                lines.pop(0)
                continue
            break
        stripped = "\n".join(lines).strip()
        return stripped or text

    if content is None:
        return [TextPart(text="")]
    if isinstance(content, str):
        return [TextPart(text=_strip_leading_tool_call_text(content))]
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
            analysis=existing.analysis if existing and isinstance(existing.analysis, dict) else None,
            updated_at=existing.updated_at if existing else None,
        )
        await _save_memory(context_id=context_id, metadata=metadata, memory=memory)

    def _system_prompt_with_memory(*, base: str, memory: ContextMemory | None) -> str:
        if memory is None or (not memory.summary and not memory.profile and not memory.analysis):
            return base
        blocks: list[str] = []
        if base:
            blocks.append(base)
        if memory.summary:
            blocks.append(f"Session summary:\n{memory.summary}".strip())
        if memory.profile:
            blocks.append("Session profile (JSON):\n" + json.dumps(memory.profile, ensure_ascii=False))
        if memory.analysis:
            blocks.append("Last structured analysis (JSON):\n" + json.dumps(memory.analysis, ensure_ascii=False))
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
        response_parts: list[Any] | None = None,
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
        analysis_payload = None
        if response_parts:
            try:
                analysis_payload = _interaction_payload_from_parts(response_parts)
            except Exception:
                analysis_payload = None
            if not (isinstance(analysis_payload, dict) and str(analysis_payload.get("kind") or "").strip() == "insight_response"):
                analysis_payload = existing.analysis if existing and isinstance(existing.analysis, dict) else None
        else:
            analysis_payload = existing.analysis if existing and isinstance(existing.analysis, dict) else None

        new_memory = ContextMemory(
            summary=str(summary).strip() if isinstance(summary, str) and summary.strip() else None,
            profile=profile if isinstance(profile, dict) and profile else None,
            workflow_state=existing.workflow_state if existing and isinstance(existing.workflow_state, dict) else None,
            analysis=analysis_payload if isinstance(analysis_payload, dict) else None,
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
            if _canonical_host_domain_agent(agent_name) != "host" or tool_executor is None or "list_available_agents" not in tool_names:
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

        if _canonical_host_domain_agent(agent_name) == "host" and interaction_response is None and user_text_for_memory:
            logger.info(
                "host conversation history probe count=%s has_insight=%s",
                len(history) if isinstance(history, list) else 0,
                bool(_latest_history_insight_payload(history)),
            )
            repeated_response_parts = _latest_repeated_question_response_parts(user_text_for_memory, history)
            if repeated_response_parts:
                response_parts = repeated_response_parts
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
                    user_text=user_text_for_memory,
                    assistant_text=_parts_to_text(response_parts),
                    response_parts=response_parts,
                    history=history if isinstance(history, list) else None,
                )
                return
            follow_up_answer = _latest_insight_follow_up_answer(user_text_for_memory, history, memory=mem)
            if follow_up_answer:
                response_parts = [TextPart(text=follow_up_answer)]
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
                    assistant_text=follow_up_answer,
                    response_parts=response_parts,
                )
                return

        if agent_name == "pos_admin" and interaction_response is None and user_text_for_memory and tool_executor is not None:
            named_insight = _pos_admin_named_insight_from_text(user_text_for_memory)
            if named_insight:
                insight_output = await _pos_admin_named_insight_payload(
                    insight_key=named_insight,
                    tool_executor=tool_executor,
                    tool_ctx=tool_ctx,
                    user_text=user_text_for_memory,
                )
                if isinstance(insight_output, dict):
                    response_text = str(insight_output.get("summary") or "").strip() or "POS insight ready."
                    response_parts = [DataPart(data=insight_output)]
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
                        response_parts=response_parts,
                    )
                    return

        if agent_name == "inventory_visibility" and interaction_response is None and user_text_for_memory and tool_executor is not None:
            named_insight = _inventory_visibility_named_insight_from_text(user_text_for_memory)
            if named_insight:
                try:
                    insight_output = await _inventory_visibility_named_insight_payload(
                        insight_key=named_insight,
                        tool_executor=tool_executor,
                        tool_ctx=tool_ctx,
                        user_text=user_text_for_memory,
                    )
                except Exception:
                    logger.exception(
                        "host deterministic insight failed agent=%s insight_key=%s user_text=%s",
                        agent_name,
                        named_insight,
                        user_text_for_memory,
                    )
                    insight_output = None
                if isinstance(insight_output, dict):
                    response_text = str(insight_output.get("summary") or "").strip() or "Inventory insight ready."
                    response_parts = [DataPart(data=insight_output)]
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
                        response_parts=response_parts,
                    )
                    return

        if agent_name == "users" and interaction_response is None and user_text_for_memory and tool_executor is not None:
            named_insight = _users_named_insight_from_text(user_text_for_memory)
            if named_insight:
                try:
                    insight_output = await _users_named_insight_payload(
                        insight_key=named_insight,
                        tool_executor=tool_executor,
                        tool_ctx=tool_ctx,
                        user_text=user_text_for_memory,
                    )
                except Exception as exc:
                    error_text = str(exc).strip()
                    lowered_error = error_text.lower()
                    if "owner" in lowered_error or "permission" in lowered_error or "forbidden" in lowered_error:
                        if named_insight == "subscription_usage_limits":
                            insight_output = _build_access_denied_insight(
                                summary="Subscription usage is restricted.",
                                detail=error_text or "Only the workspace owner can view subscription usage and limits.",
                                permission="manage_workspace_subscription",
                                source="subscriptions",
                            )
                        else:
                            insight_output = _build_access_denied_insight(
                                summary="Audit insight access is restricted.",
                                detail=error_text or "You need audit visibility to view this insight.",
                                permission="view_audit_logs",
                                source="audit",
                            )
                    else:
                        raise
                if isinstance(insight_output, dict):
                    response_text = str(insight_output.get("summary") or "").strip() or "User insight ready."
                    response_parts = [DataPart(data=insight_output)]
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
                        response_parts=response_parts,
                    )
                    return

        if agent_name == "product_discovery" and interaction_response is None and user_text_for_memory and tool_executor is not None:
            named_insight = _product_discovery_named_insight_from_text(user_text_for_memory)
            if named_insight:
                try:
                    insight_output = await _product_discovery_named_insight_payload(
                        insight_key=named_insight,
                        tool_executor=tool_executor,
                        tool_ctx=tool_ctx,
                        user_text=user_text_for_memory,
                    )
                except Exception:
                    insight_output = None
                if isinstance(insight_output, dict):
                    response_text = str(insight_output.get("summary") or "").strip() or "Product insight ready."
                    response_parts = [DataPart(data=insight_output)]
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
                        response_parts=response_parts,
                    )
                    return

        if agent_name == "inventory_procurement" and interaction_response is None and user_text_for_memory and tool_executor is not None:
            named_insight = _inventory_procurement_named_insight_from_text(user_text_for_memory)
            if named_insight:
                try:
                    insight_output = await _inventory_procurement_named_insight_payload(
                        insight_key=named_insight,
                        tool_executor=tool_executor,
                        tool_ctx=tool_ctx,
                        user_text=user_text_for_memory,
                    )
                except Exception:
                    insight_output = None
                if isinstance(insight_output, dict):
                    response_text = str(insight_output.get("summary") or "").strip() or "Procurement insight ready."
                    response_parts = [DataPart(data=insight_output)]
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
                        response_parts=response_parts,
                    )
                    return

        if _canonical_host_domain_agent(agent_name) == "host" and interaction_response is None and user_text_for_memory and tool_executor is not None:
            named_insight = _host_named_insight_from_text(user_text_for_memory)
            logger.info(
                "host deterministic insight probe agent=%s user_text=%r named_insight=%r",
                agent_name,
                user_text_for_memory,
                named_insight,
            )
            if named_insight:
                try:
                    insight_output = await _host_named_insight_payload(
                        insight_key=named_insight,
                        tool_executor=tool_executor,
                        tool_ctx=tool_ctx,
                        user_text=user_text_for_memory,
                    )
                except Exception:
                    insight_output = None
                if isinstance(insight_output, dict):
                    response_text = str(insight_output.get("summary") or "").strip() or "Workspace insight ready."
                    response_parts = [DataPart(data=insight_output)]
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
                        response_parts=response_parts,
                    )
                    return

        async def _update_host_orchestration_state_after_delegation(
            *,
            delegated_response: dict[str, Any],
            original_request: str,
            orchestration_plan: list[str] | None,
            prior_completed_agents: list[str] | None = None,
        ) -> dict[str, Any] | None:
            nonlocal saved_workflow_state
            if _canonical_host_domain_agent(agent_name) != "host":
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
                [
                    _canonical_host_domain_agent(str(name))
                    for name in prior_state.get("completed_agents") or []
                    if str(name).strip()
                ]
                if isinstance(prior_state, dict)
                else [
                    _canonical_host_domain_agent(str(name))
                    for name in (prior_completed_agents or [])
                    if str(name).strip()
                ]
            )
            delegated_agent = str(delegated_response.get("delegated_agent") or "").strip()
            delegated_agent_domain = _canonical_host_domain_agent(delegated_agent)
            interaction_payload = _interaction_payload_from_parts(delegated_response.get("response_parts") or [])
            remaining_agents = [
                name
                for name in plan
                if _canonical_host_domain_agent(name) not in prior_completed
                and _canonical_host_domain_agent(name) != delegated_agent_domain
            ]

            if delegated_response.get("delegated_final_state") == TaskState.input_required:
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
                }
                if isinstance(interaction_payload, dict):
                    workflow_state["pending_interaction"] = interaction_payload
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
            if (
                delegated_response.get("delegated_final_state") == TaskState.completed
                and delegated_agent_domain
                and delegated_agent_domain not in completed_agents
            ):
                completed_agents.append(delegated_agent_domain)
            remaining_agents = [
                name
                for name in plan
                if _canonical_host_domain_agent(name) not in completed_agents
            ]

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
            _canonical_host_domain_agent(agent_name) == "host"
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
                response_parts=response_parts,
            )
            return

        if (
            _canonical_host_domain_agent(agent_name) == "host"
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
                    response_parts=response_parts,
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
                    response_parts=response_parts,
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
                    response_parts=response_parts,
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
                    response_parts=response_parts,
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
                response_parts=response_parts,
            )
            return

        if (
            _canonical_host_domain_agent(agent_name) == "host"
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
                    response_parts=response_parts,
                )
                return

            agent_summaries = (await _load_host_agent_listing()).get("agents")
            available_names = _available_agent_names(agent_summaries)
            if available_names and selected_value not in available_names:
                if _should_offer_host_unavailable_domain_picker(user_text_for_memory) and "create_multiple_choice" in tool_names:
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
                    response_parts=response_parts,
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
                    response_parts=response_parts,
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
                    response_parts=response_parts,
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
                    response_parts=response_parts,
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
                response_parts=response_parts,
            )
            return

        if (
            _canonical_host_domain_agent(agent_name) == "host"
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
                    response_parts=response_parts,
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
                    response_parts=response_parts,
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
                response_parts=response_parts,
            )
            return

        if (
            _canonical_host_domain_agent(agent_name) == "host"
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
                        response_parts=response_parts,
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
                        response_parts=response_parts,
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
                    response_parts=response_parts,
                )
                return

        delegated_interaction = _delegated_interaction_context(last_interaction_payload)
        if (
            _canonical_host_domain_agent(agent_name) == "host"
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
                    response_parts=response_parts,
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
                    response_parts=response_parts,
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
                response_parts=response_parts,
            )
            return

        if (
            _canonical_host_domain_agent(agent_name) == "host"
            and tool_executor is not None
            and "delegate_to_agent" in tool_names
            and interaction_response is None
            and user_text_for_memory
            and isinstance(saved_workflow_state, dict)
            and str(saved_workflow_state.get("workflow") or "").strip().lower() == "host_orchestration"
            and str(saved_workflow_state.get("stage") or "").strip().lower() == "specialist_interaction"
        ):
            delegated_agent_name = str(saved_workflow_state.get("current_agent") or "").strip()
            delegated_task_id = str(saved_workflow_state.get("delegated_task_id") or "").strip() or None
            if delegated_agent_name:
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
                        response_parts=response_parts,
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
                        response_parts=response_parts,
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
                        str(saved_workflow_state.get("original_request") or "").strip() or user_text_for_memory
                    ),
                    orchestration_plan=(
                        [str(name).strip() for name in saved_workflow_state.get("plan") or [] if str(name).strip()]
                        if isinstance(saved_workflow_state.get("plan"), list)
                        else _host_orchestration_plan(user_text_for_memory, agent_summaries)
                    ),
                    prior_completed_agents=(
                        [str(name).strip() for name in saved_workflow_state.get("completed_agents") or [] if str(name).strip()]
                        if isinstance(saved_workflow_state.get("completed_agents"), list)
                        else None
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
                        response_parts=response_parts,
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
                    response_parts=response_parts,
                )
                return

        if (
            _canonical_host_domain_agent(agent_name) == "host"
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

        if agent_name == "marketplace_sourcing" and tool_executor is not None:
            if (
                interaction_response is not None
                and _is_marketplace_results_payload(last_interaction_payload)
                and _is_marketplace_results_response(interaction_response)
            ):
                selected_items = _marketplace_response_selected_items(interaction_response)
                selected_action = _marketplace_response_action(interaction_response) or "share_selected"
                if selected_action == "compare_selected" and "compare_marketplace_products" in tool_names:
                    if len(selected_items) < 2:
                        response_text = "Select at least two marketplace products before asking me to compare them."
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
                            name="compare_marketplace_products",
                            arguments={
                                "items": selected_items,
                                "title": (
                                    f"Compare offers for {str(last_interaction_payload.get('query') or '').strip()}"
                                    if str(last_interaction_payload.get("query") or "").strip()
                                    else "Compare marketplace products"
                                ),
                            },
                            ctx=tool_ctx,
                        )
                    except Exception as exc:
                        response_text = str(exc).strip() or "I couldn't compare the selected marketplace products."
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
                            response_parts=response_parts,
                        )
                        return

                    response_text = json.dumps(output, ensure_ascii=False) if isinstance(output, dict) else str(output)
                    response_parts = [DataPart(data=output)] if isinstance(output, dict) else [TextPart(text=response_text)]
                    yield Artifact(name="result", parts=response_parts)
                    yield TaskStatus(
                        state=TaskState.input_required if isinstance(output, dict) else TaskState.completed,
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
                        response_parts=response_parts,
                    )
                    return

                response_text = _marketplace_selected_items_summary(selected_items)
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
                    response_parts=response_parts,
                )
                return

            if (
                interaction_response is None
                and user_text_for_memory
                and "search_marketplace_products" in tool_names
            ):
                search_arguments = _marketplace_search_arguments_from_text(user_text_for_memory)
                if search_arguments is not None:
                    yield TaskStatus(
                        state=TaskState.working,
                        message=Message(
                            role=Role.agent,
                            parts=[TextPart(text="Searching online marketplaces for matching products.")],
                            context_id=task.context_id,
                        ),
                    )
                    try:
                        output = await tool_executor.call_tool(
                            name="search_marketplace_products",
                            arguments=search_arguments,
                            ctx=tool_ctx,
                        )
                    except Exception as exc:
                        response_text = str(exc).strip() or "Marketplace search failed."
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
                            response_parts=response_parts,
                        )
                        return

                    response_text = json.dumps(output, ensure_ascii=False) if isinstance(output, dict) else str(output)
                    response_parts = [DataPart(data=output)] if isinstance(output, dict) else [TextPart(text=response_text)]
                    yield Artifact(name="result", parts=response_parts)
                    yield TaskStatus(
                        state=TaskState.input_required if isinstance(output, dict) else TaskState.completed,
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
                        response_parts=response_parts,
                    )
                    return

        if agent_name == "onboarding" and tool_executor is not None:
            saved_workflow_state = await _load_workflow_state(context_id=task.context_id, metadata=metadata)
            active_company_context: dict[str, Any] | None = None

            def _is_legacy_product_import_state(state: dict[str, Any] | None) -> bool:
                if not isinstance(state, dict):
                    return False
                flattened = json.dumps(state, default=str, ensure_ascii=False).lower()
                legacy_markers = (
                    "primary_location_for_this_inventory",
                    "default_inventory_name",
                    "inventory_description",
                    "inventory_category_id",
                    "inventory_item_id",
                    "primary_location_mode",
                    "stock_locations",
                    "inventory_setup",
                    "inventory_item",
                )
                return any(marker in flattened for marker in legacy_markers)

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

            if _is_legacy_product_import_state(saved_workflow_state):
                saved_workflow_state = None
                await _save_workflow_state(context_id=task.context_id, metadata=metadata, workflow_state=None)

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
                    response_text = "Saved import flow was canceled. When you are ready, I can start a fresh product import flow."
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
                        response_parts=response_parts,
                    )
                    return

            if (
                interaction_response is not None
                and _is_onboarding_payload(last_interaction_payload, stage="scope_picker")
                and "create_wizard_flow" in tool_names
            ):
                selected_scope = _selected_interaction_value(interaction_response) or "product_onboarding"
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
                        workflow="product_import",
                        workflow_stage="wizard",
                        onboarding_scope=selected_scope,
                    )
                    workflow_state = {
                        "workflow": "product_import",
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
                selected_scope = str(last_interaction_payload.get("onboarding_scope") or "product_onboarding").strip() or "product_onboarding"
                if bool(interaction_response.get("skipped")):
                    workflow_state = {
                        "workflow": "product_import",
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
                    response_text = "Import paused. When you are ready, I can resume the saved import workflow."
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
                        response_parts=response_parts,
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
                        workflow="product_import",
                        workflow_stage="review",
                        onboarding_scope=selected_scope,
                        onboarding_data=onboarding_data,
                        onboarding_summary=summary,
                    )
                    workflow_state = {
                        "workflow": "product_import",
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
                if selected_action == "revise_answers":
                    selected_action = "cancel_onboarding"
                selected_scope = str(last_interaction_payload.get("onboarding_scope") or "product_onboarding").strip() or "product_onboarding"
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
                company_context = (
                    last_interaction_payload.get("company_context")
                    if isinstance(last_interaction_payload.get("company_context"), dict)
                    else await _maybe_active_company_context()
                )

                if selected_action == "cancel_onboarding":
                    await _save_workflow_state(context_id=task.context_id, metadata=metadata, workflow_state=None)
                    response_text = "Import canceled for now. When you are ready, I can restart the import flow."
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
                        response_parts=response_parts,
                    )
                    return

                yield TaskStatus(
                    state=TaskState.working,
                    message=Message(
                        role=Role.agent,
                        parts=[TextPart(text="Applying the product import plan now.")],
                        context_id=task.context_id,
                    ),
                )

                created_map = {
                    key: value for key, value in created_operations.items() if isinstance(value, dict)
                }
                executed_map, failed_items, any_tool_executed = await _execute_onboarding_plan_operations(
                    selected_scope=selected_scope,
                    onboarding_data=onboarding_data,
                    tool_specs=tool_specs,
                    company_context=company_context,
                    existing_created_map=created_map,
                    tool_executor=tool_executor,
                    tool_ctx=tool_ctx,
                )
                created_map = executed_map

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
                                        "workflow": "product_import",
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
                                response_parts=response_parts,
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
                            workflow="product_import",
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
                            "workflow": "product_import",
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
                    response_parts=response_parts,
                )
                return

            onboarding_creation_payload = (
                _extract_onboarding_creation_payload_from_text(user_text_for_memory)
                if interaction_response is None and user_text_for_memory
                else None
            )
            if onboarding_creation_payload is not None:
                selected_scope, onboarding_data = onboarding_creation_payload
                company_context = (
                    onboarding_data.get("company_context")
                    if isinstance(onboarding_data.get("company_context"), dict)
                    else await _maybe_active_company_context()
                )
                created_map, failed_items, _ = await _execute_onboarding_plan_operations(
                    selected_scope=selected_scope,
                    onboarding_data=onboarding_data,
                    tool_specs=tool_specs,
                    company_context=company_context,
                    tool_executor=tool_executor,
                    tool_ctx=tool_ctx,
                )
                if failed_items:
                    summary = _onboarding_summary_text(selected_scope, onboarding_data)
                    response_text = _onboarding_operation_summary(
                        created_operations=created_map,
                        failed_operations=failed_items,
                    ) or "Some approved product import steps could not be completed automatically."
                    if summary:
                        response_text = f"{summary}\n\n{response_text}"
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
                        response_parts=response_parts,
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
                    response_parts=response_parts,
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
                                workflow="product_import",
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
                    direct_scope or "product_onboarding",
                    user_text_for_memory or "",
                )
                if direct_scope == "product_onboarding" and "create_wizard_flow" in tool_names:
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
                        if direct_prefill:
                            existing_responses = _build_onboarding_existing_responses(
                                direct_scope,
                                wizard_payload=interaction_output,
                                prefill_data=direct_prefill,
                            )
                            existing_responses = _normalize_onboarding_existing_responses(
                                direct_scope,
                                wizard_payload=interaction_output,
                                existing_responses=existing_responses,
                            )
                            if existing_responses:
                                interaction_output["existing_responses"] = existing_responses
                        interaction_output = _with_interaction_metadata(
                            interaction_output,
                            workflow="product_import",
                            workflow_stage="wizard",
                            onboarding_scope=direct_scope,
                        )
                        interaction_output["description"] = (
                            "I started the product import workflow. Choose categories and brands first, then review the catalog pages and import the products you want."
                            if not direct_prefill
                            else "I prefilled this product import flow from your message. Review it, correct anything that is off, and complete any remaining fields before I create anything."
                        )
                        workflow_state = {
                            "workflow": "product_import",
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
                description = "Choose the import flow you want to start. I will guide you step by step."
                if isinstance(company_context, dict):
                    company_name = str(company_context.get("name") or "").strip()
                    if company_name:
                        description = f"Current company: {company_name}\n\n{description}"
                try:
                    interaction_output = await tool_executor.call_tool(
                        name="create_wizard_flow",
                        arguments=_onboarding_wizard_arguments("product_onboarding"),
                        ctx=tool_ctx,
                    )
                except Exception:
                    interaction_output = None

                if isinstance(interaction_output, dict):
                    interaction_output = _with_interaction_metadata(
                        interaction_output,
                        workflow="product_import",
                        workflow_stage="wizard",
                        onboarding_scope="product_onboarding",
                    )
                    interaction_output["description"] = description + "\n\nStart by choosing product categories and brands."
                    workflow_state = {
                        "workflow": "product_import",
                        "status": "collecting",
                        "stage": "wizard",
                        "scope": "product_onboarding",
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

        if agent_name in {"inventory_setup", "product_catalog_admin", "inventory_fulfillment", "inventory_procurement", "product_merchandising", "product_pricing", "pos_admin"} and tool_executor is not None:
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
                        interaction_payload=last_interaction_payload if isinstance(last_interaction_payload, dict) else None,
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
                elif agent_name == "pos_admin":
                    tool_name, arguments, missing_required = await _pos_admin_prepare_execution(
                        action=action,
                        form_data=form_data,
                        tool_specs=tool_specs,
                        tool_executor=tool_executor,
                        tool_ctx=tool_ctx,
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
                        source_text=user_text_for_memory,
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
                        response_parts=response_parts,
                    )
                    return

                success_messages = {
                    "create_inventory_item": "Inventory item created successfully.",
                    "create_stock_location": "Stock location created successfully.",
                    "update_stock_location_parent": "Stock location parent updated successfully.",
                    "create_product": "Product created successfully.",
                    "update_product": "Product updated successfully.",
                    "create_stock_reservation": "Stock reservation created successfully.",
                    "transfer_location_stock": "Stock transfer completed successfully.",
                    "adjust_inventory_item_stock": "Inventory adjustment completed successfully.",
                    "add_purchase_order_line_item": "Purchase-order line item added successfully.",
                    "update_product_merchandising": "Product merchandising updated successfully.",
                    "create_pricing_strategy": "Pricing strategy created successfully.",
                    "create_pricing_rule": "Pricing rule created successfully.",
                    "create_pos_terminal": "POS terminal created successfully.",
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
                    response_parts=response_parts,
                )
                return

            if interaction_response is None and user_text_for_memory:
                if agent_name == "inventory_setup":
                    onboarding_creation_payload = _extract_onboarding_creation_payload_from_text(user_text_for_memory)
                    if onboarding_creation_payload is not None:
                        if "delegate_to_agent" in tool_names:
                            try:
                                delegated = await tool_executor.call_tool(
                                    name="delegate_to_agent",
                                    arguments={
                                        "request": user_text_for_memory,
                                        "agent_name": "onboarding",
                                    },
                                    ctx=tool_ctx,
                                )
                            except Exception:
                                delegated = None
                            else:
                                delegated_response = _coerce_delegated_response(
                                    delegated,
                                    fallback_agent_name="onboarding",
                                )
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
                                    await _maybe_update_memory(
                                        llm=llm,
                                        context_id=task.context_id,
                                        metadata=metadata,
                                        existing=mem,
                                        history=history if isinstance(history, list) else None,
                                        user_text=user_text_for_memory,
                                        assistant_text=response_text,
                                        response_parts=response_parts,
                                    )
                                    return

                action = None
                direct_lookup: tuple[str, dict[str, Any]] | None = None
                if agent_name == "inventory_setup":
                    direct_lookup = _inventory_setup_lookup_from_text(user_text_for_memory)
                    action = _inventory_setup_action_from_text(user_text_for_memory)
                elif agent_name == "product_catalog_admin":
                    action = _product_catalog_admin_action_from_text(user_text_for_memory)
                elif agent_name == "inventory_fulfillment":
                    action = _inventory_fulfillment_action_from_text(user_text_for_memory)
                elif agent_name == "inventory_procurement":
                    action = _inventory_procurement_action_from_text(user_text_for_memory)
                elif agent_name == "inventory_visibility":
                    named_insight = _inventory_visibility_named_insight_from_text(user_text_for_memory)
                    if named_insight and tool_executor is not None:
                        try:
                            insight_output = await _inventory_visibility_named_insight_payload(
                                insight_key=named_insight,
                                tool_executor=tool_executor,
                                tool_ctx=tool_ctx,
                                user_text=user_text_for_memory,
                            )
                        except Exception:
                            insight_output = None
                        if isinstance(insight_output, dict):
                            response_text = str(insight_output.get("summary") or "").strip() or "Inventory insight ready."
                            response_parts = [DataPart(data=insight_output)]
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
                                response_parts=response_parts,
                            )
                            return
                    action = None
                elif agent_name == "product_merchandising":
                    action = _product_merchandising_action_from_text(user_text_for_memory)
                elif agent_name == "product_pricing":
                    direct_lookup = _product_pricing_lookup_from_text(user_text_for_memory)
                    action = _product_pricing_action_from_text(user_text_for_memory)
                elif agent_name == "pos_admin":
                    named_insight = _pos_admin_named_insight_from_text(user_text_for_memory)
                    if named_insight and tool_executor is not None:
                        try:
                            insight_output = await _pos_admin_named_insight_payload(
                                insight_key=named_insight,
                                tool_executor=tool_executor,
                                tool_ctx=tool_ctx,
                                user_text=user_text_for_memory,
                            )
                        except Exception:
                            insight_output = None
                        if isinstance(insight_output, dict):
                            response_text = str(insight_output.get("summary") or "").strip() or "POS insight ready."
                            response_parts = [DataPart(data=insight_output)]
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
                                response_parts=response_parts,
                            )
                            return
                    action = _pos_admin_action_from_text(user_text_for_memory)
                else:
                    action = _product_pricing_action_from_text(user_text_for_memory)
                if direct_lookup:
                    lookup_tool, lookup_arguments = direct_lookup
                    lookup_display_name = str(lookup_arguments.get("product_name") or "").strip()
                    if lookup_tool == "product.get_product_pricing_rules" and lookup_display_name:
                        try:
                            search_output = await tool_executor.call_tool(
                                name="product.search_products",
                                arguments={"query": lookup_display_name, "limit": 10},
                                ctx=tool_ctx,
                            )
                        except Exception:
                            search_output = None
                        product_items = _relation_items_from_lookup_output("product.search_products", search_output)
                        selected_product_id = None
                        normalized_name = re.sub(r"\s+", " ", lookup_display_name.strip().lower())
                        for item in product_items:
                            item_name = re.sub(r"\s+", " ", str(_first_string(item, ["name", "title", "label"]) or "").strip().lower())
                            if item_name == normalized_name:
                                selected_product_id = _first_string(item, ["id", "uuid", "value"])
                                break
                        if not selected_product_id and product_items:
                            selected_product_id = _first_string(product_items[0], ["id", "uuid", "value"])
                            lookup_display_name = str(_first_string(product_items[0], ["name", "title", "label"]) or lookup_display_name).strip()
                        lookup_arguments = {key: value for key, value in lookup_arguments.items() if key != "product_name"}
                        if selected_product_id:
                            lookup_arguments["product_id"] = selected_product_id
                    try:
                        output = await tool_executor.call_tool(
                            name=lookup_tool,
                            arguments=lookup_arguments,
                            ctx=tool_ctx,
                        )
                    except Exception as exc:
                        response_text = str(exc).strip() or "I couldn't complete the requested lookup."
                    else:
                        category_items = _relation_items_from_lookup_output(lookup_tool, output)
                        category_names = [
                            _first_string(item, ["name", "label", "category"])
                            for item in category_items
                            if isinstance(item, dict)
                        ]
                        category_names = [name for name in category_names if name]
                        if lookup_tool == "product.get_product_pricing_rules":
                            rule_items = _relation_items_from_lookup_output(lookup_tool, output)
                            rule_names = [
                                _first_string(item, ["name", "label", "rule_name"])
                                for item in rule_items
                                if isinstance(item, dict)
                            ]
                            rule_names = [name for name in rule_names if name]
                            if rule_names:
                                subject = lookup_display_name or "this product"
                                response_text = f"Pricing rules for {subject}: " + ", ".join(rule_names[:20]) + "."
                            else:
                                subject = lookup_display_name or "this product"
                                response_text = f"No pricing rules are configured for {subject}."
                        elif category_names:
                            response_text = "Inventory categories: " + ", ".join(category_names[:25]) + "."
                        else:
                            response_text = "No inventory categories are available yet."
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
                        response_parts=response_parts,
                    )
                    return
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
                    elif agent_name == "pos_admin":
                        interaction_output = await _pos_admin_dynamic_form_payload(
                            action=action,
                            text=user_text_for_memory,
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
            selected_agent = _select_router_handoff_agent(
                agent_name,
                user_text_for_memory,
                router_listing.get("agents") or [],
            )

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
                        response_parts=response_parts,
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
                        response_parts=response_parts,
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
                        response_parts=response_parts,
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
                    response_parts=response_parts,
                )
                return

        if (
            _canonical_host_domain_agent(agent_name) == "host"
            and tool_executor is not None
            and "delegate_to_agent" in tool_names
            and user_text_for_memory
            and not _is_host_introspection_query(user_text_for_memory)
        ):
            agent_listing = await _load_host_agent_listing()
            agent_summaries = agent_listing.get("agents")
            named_insight = _host_named_insight_from_text(user_text_for_memory)
            if named_insight:
                try:
                    insight_output = await _host_named_insight_payload(
                        insight_key=named_insight,
                        tool_executor=tool_executor,
                        tool_ctx=tool_ctx,
                        user_text=user_text_for_memory,
                    )
                except Exception:
                    insight_output = None
                if isinstance(insight_output, dict):
                    response_text = str(insight_output.get("summary") or "").strip() or "Workspace insight ready."
                    response_parts = [DataPart(data=insight_output)]
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
                        response_parts=response_parts,
                    )
                    return

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
                        response_parts=response_parts,
                    )
                    return

                if _should_offer_host_unavailable_domain_picker(user_text_for_memory) and "create_multiple_choice" in tool_names:
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
                    response_parts=response_parts,
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
                        response_parts=response_parts,
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
                        response_parts=response_parts,
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
                        response_parts=response_parts,
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
                    response_parts=response_parts,
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
                        response_parts=response_parts,
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
                        source_text=user_text_for_memory,
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
                response_parts = _strip_placeholder_text_parts(response_parts)
                rewritten_text = _text_from_parts(response_parts)
                response_text = (
                    rewritten_text
                    or _interaction_payload_summary_text(interaction_payload)
                    or json.dumps(interaction_payload, ensure_ascii=False)
                )

        artifact = Artifact(name="result", parts=response_parts or [TextPart(text=response_text)])
        yield artifact

        interaction_payload = _interaction_payload_from_parts(response_parts or [TextPart(text=response_text)])
        agent_message_parts = response_parts or [TextPart(text=response_text)]
        if interaction_payload is not None:
            agent_message_parts = _strip_placeholder_text_parts(agent_message_parts)
        if interaction_payload is not None and not _text_from_parts(agent_message_parts):
            summary_text = _interaction_payload_summary_text(interaction_payload)
            if summary_text:
                agent_message_parts = [TextPart(text=summary_text), *agent_message_parts]
                response_text = summary_text
        agent_msg = Message(role=Role.agent, parts=agent_message_parts)
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
            response_parts=response_parts,
        )

    return _proc
