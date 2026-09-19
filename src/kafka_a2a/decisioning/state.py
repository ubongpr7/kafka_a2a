from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from kafka_a2a.ops import trace_id_from_metadata
from kafka_a2a.tenancy import extract_principal


_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|authorization|bearer|credential|password|secret|token)", re.IGNORECASE)
_BEARER_VALUE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_ASSIGNED_SECRET = re.compile(r"(?i)(\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+")
_JWT_VALUE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")


@dataclass(frozen=True, slots=True)
class BoundedDecisionState:
    state: dict[str, Any]
    state_hash: str | None
    was_redacted: bool
    was_truncated: bool


def _redact_text(value: str) -> tuple[str, bool]:
    redacted = _BEARER_VALUE.sub(r"\1[REDACTED]", value)
    redacted = _ASSIGNED_SECRET.sub(r"\1[REDACTED]", redacted)
    redacted = _JWT_VALUE.sub("[REDACTED_JWT]", redacted)
    return redacted, redacted != value


def _truncate(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    if limit <= 3:
        return value[:limit], True
    return value[: limit - 3] + "...", True


def _message_text(message: Any) -> tuple[str, str]:
    if isinstance(message, Mapping):
        role = str(message.get("role") or "").strip().lower()
        content = message.get("content")
        if isinstance(content, str):
            return role, content.strip()
        parts = message.get("parts")
    else:
        role_value = getattr(message, "role", "")
        role = str(getattr(role_value, "value", role_value) or "").strip().lower()
        parts = getattr(message, "parts", None)
    if not isinstance(parts, list):
        return role, ""
    text_parts: list[str] = []
    for part in parts:
        if isinstance(part, Mapping):
            text = part.get("text") if part.get("kind") == "text" else None
        else:
            text = getattr(part, "text", None) if getattr(part, "kind", None) == "text" else None
        if isinstance(text, str) and text.strip():
            text_parts.append(text.strip())
    return role, "\n".join(text_parts)


def _safe_workflow_state(workflow_state: Mapping[str, Any] | None) -> tuple[dict[str, Any], bool]:
    if not isinstance(workflow_state, Mapping):
        return {}, False
    safe: dict[str, Any] = {}
    for key in ("workflow", "stage", "target_agent", "targetAgent"):
        value = workflow_state.get(key)
        if isinstance(value, (str, int, float, bool)):
            safe["target_agent" if key == "targetAgent" else key] = value
    awaiting = str(workflow_state.get("workflow") or "").strip().lower() == "clarification"
    if awaiting:
        safe["awaiting_clarification"] = True
    return safe, len(safe) != len(workflow_state)


def _safe_extra_state(value: Mapping[str, Any] | None, *, text_limit: int) -> tuple[dict[str, Any], bool, bool]:
    """Keep decision extras primitive, bounded, and free of secret-shaped keys."""

    if not isinstance(value, Mapping):
        return {}, False, False
    safe: dict[str, Any] = {}
    redacted = False
    truncated = False
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip()
        if not key or _SENSITIVE_KEY.search(key):
            redacted = True
            continue
        if isinstance(raw_value, str):
            text, did_redact = _redact_text(raw_value.strip())
            text, did_truncate = _truncate(text, text_limit)
            safe[key] = text
            redacted = redacted or did_redact
            truncated = truncated or did_truncate
            continue
        if isinstance(raw_value, (bool, int, float)) or raw_value is None:
            safe[key] = raw_value
            continue
        if isinstance(raw_value, Mapping):
            child: dict[str, str] = {}
            for child_key, child_value in raw_value.items():
                child_name = str(child_key).strip()
                if not child_name or _SENSITIVE_KEY.search(child_name) or not isinstance(child_value, str):
                    redacted = True
                    continue
                text, did_redact = _redact_text(child_value.strip())
                text, did_truncate = _truncate(text, text_limit)
                child[child_name] = text
                redacted = redacted or did_redact
                truncated = truncated or did_truncate
            safe[key] = child
            continue
        if isinstance(raw_value, list) and all(isinstance(item, str) for item in raw_value):
            values: list[str] = []
            for item in raw_value[:20]:
                text, did_redact = _redact_text(item.strip())
                text, did_truncate = _truncate(text, text_limit)
                values.append(text)
                redacted = redacted or did_redact
                truncated = truncated or did_truncate
            truncated = truncated or len(raw_value) > 20
            safe[key] = values
            continue
        redacted = True
    return safe, redacted, truncated


def _profile_id(metadata: Mapping[str, Any] | None) -> str | None:
    principal = extract_principal(dict(metadata or {}))
    if principal is not None:
        if principal.tenant_id:
            return str(principal.tenant_id)
        if isinstance(principal.claims, dict):
            for key in ("profile_id", "tenant_id"):
                value = str(principal.claims.get(key) or "").strip()
                if value:
                    return value
    for key in ("profile_id", "tenant_id"):
        value = str((metadata or {}).get(key) or "").strip()
        if value:
            return value
    return None


def decision_attribution(
    *,
    metadata: Mapping[str, Any] | None,
    task_id: str | None,
    context_id: str | None,
    agent_slug: str,
):
    from kafka_a2a.decisioning.contracts import DecisionAttribution

    metadata_dict = dict(metadata or {})
    return DecisionAttribution(
        profile_id=_profile_id(metadata_dict),
        task_id=str(task_id or "").strip() or None,
        context_id=str(context_id or "").strip() or None,
        trace_id=trace_id_from_metadata(metadata_dict),
        agent_slug=str(agent_slug or "").strip() or "host",
    )


def build_host_turn_state(
    *,
    current_user_message: str,
    history: Iterable[Any] | None,
    workflow_state: Mapping[str, Any] | None,
    awaiting_clarification: bool,
    previous_host_intent: str | None,
    max_chars: int,
    history_items: int,
    hash_key: str | None,
    extra_state: Mapping[str, Any] | None = None,
) -> BoundedDecisionState:
    """Build the minimal, text-only state needed for host-turn classification."""

    limit = max(512, int(max_chars))
    per_text_limit = max(128, min(1600, limit // 2))
    current_text, redacted = _redact_text(str(current_user_message or "").strip())
    current_text, current_truncated = _truncate(current_text, per_text_limit)
    workflow, workflow_redacted = _safe_workflow_state(workflow_state)
    extras, extras_redacted, extras_truncated = _safe_extra_state(extra_state, text_limit=per_text_limit)

    conversation: list[dict[str, str]] = []
    selected_history = list(history or [])[-max(0, history_items) :]
    history_truncated = False
    for item in selected_history:
        role, text = _message_text(item)
        if not text or role not in {"user", "human", "assistant", "agent", "ai"}:
            continue
        text, did_redact = _redact_text(text)
        text, did_truncate = _truncate(text, per_text_limit)
        redacted = redacted or did_redact
        history_truncated = history_truncated or did_truncate
        conversation.append(
            {
                "role": "user" if role in {"user", "human"} else "assistant",
                "text": text,
            }
        )

    state: dict[str, Any] = {
        "current_user_message": current_text,
        "conversation": conversation,
        "workflow": workflow,
        "routing": {
            "awaiting_clarification": bool(awaiting_clarification),
            "previous_host_intent": str(previous_host_intent or "").strip() or None,
        },
    }
    if extras:
        state["decision_context"] = extras
    truncated = current_truncated or history_truncated or extras_truncated
    encoded = json.dumps(state, ensure_ascii=False)
    while len(encoded) > limit and state["conversation"]:
        state["conversation"].pop(0)
        truncated = True
        encoded = json.dumps(state, ensure_ascii=False)
    if len(encoded) > limit:
        state = {
            "current_user_message": "",
            "routing": {"awaiting_clarification": bool(awaiting_clarification)},
        }
        structural_size = len(json.dumps(state, ensure_ascii=False))
        state["current_user_message"] = _truncate(current_text, max(0, limit - structural_size))[0]
        truncated = True
        encoded = json.dumps(state, ensure_ascii=False)

    state_hash = None
    if hash_key:
        state_hash = hmac.new(hash_key.encode("utf-8"), encoded.encode("utf-8"), hashlib.sha256).hexdigest()
    return BoundedDecisionState(
        state=state,
        state_hash=state_hash,
        was_redacted=redacted or workflow_redacted or extras_redacted,
        was_truncated=truncated,
    )
