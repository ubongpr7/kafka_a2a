from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import pathlib
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from typing import Any

from kafka_a2a.client import Ka2aClient, Ka2aClientConfig
from kafka_a2a.control_plane import ControlPlaneClient, ControlPlaneError
from kafka_a2a.core.config import A2AAppSettings
from kafka_a2a.credentials import KA2A_JWT_CLAIM_KEY
from kafka_a2a.mainapps.common.auth import build_agent_auth_context, require_permission
from kafka_a2a.models import Artifact, DataPart, Message, Role, Task, TaskArtifactUpdateEvent, TaskState, TaskStatusUpdateEvent, TextPart
from kafka_a2a.server.auth import JwtBearerConfig
from kafka_a2a.tenancy import Principal, with_principal
from kafka_a2a.transport.kafka import KafkaConfig, KafkaTransport


logger = logging.getLogger(__name__)


_AI_SETUP_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_AI_SETUP_CACHE_LOCK = threading.Lock()
_DIRECT_AI_SETUP_ENGINE: Any | None = None
_DIRECT_AI_SETUP_ENGINE_LOCK = threading.Lock()


_TERMINAL_TASK_STATES = {
    TaskState.completed,
    TaskState.failed,
    TaskState.canceled,
    TaskState.rejected,
    TaskState.input_required,
    TaskState.auth_required,
}


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default


def _voice_backend_delegation_enabled() -> bool:
    return _parse_bool(os.getenv("KA2A_VOICE_BACKEND_DELEGATION_ENABLED"), default=False)


def _normalize_database_url(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("postgresql://"):
        return normalized.replace("postgresql://", "postgresql+psycopg://", 1)
    return normalized


def _json_row_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _host_runtime_name_from_payload(host_agent: dict[str, Any] | None) -> str:
    if not host_agent:
        return ""
    profile_id = str(host_agent.get("profile") or "").strip()
    slug = str(host_agent.get("slug") or "").strip()
    agent_id = str(host_agent.get("id") or "").strip()
    if not profile_id or not slug or not agent_id:
        return ""
    return f"wa-p{profile_id}-{slug}-{agent_id.replace('-', '')[:12]}"


def _workspace_ai_setup_payload_from_rows(
    *,
    ai: dict[str, Any],
    version: dict[str, Any] | None,
    host_agent: dict[str, Any] | None,
) -> dict[str, Any]:
    from kafka_a2a.mainapps.agents.services import _decrypt_secret

    provider = str((version or {}).get("provider") or "").strip()
    provider_label = str((version or {}).get("provider_label") or "").strip()
    model_name = str((version or {}).get("model_name") or ai.get("version") or "").strip()
    provider_base_url = str((version or {}).get("base_url") or "").strip()
    effective_base_url = str(ai.get("base_url") or provider_base_url or "").strip()
    api_key = _decrypt_secret(str(ai.get("api_key") or ""))
    if not api_key or api_key.startswith("gAAAA"):
        api_key = ""
    tavily_api_key = _decrypt_secret(str(ai.get("tavily_api_key") or ""))
    if not tavily_api_key or tavily_api_key.startswith("gAAAA"):
        tavily_api_key = ""
    return {
        "configured": True,
        "agent": {
            "id": str(ai.get("id") or ""),
            "profile": str(ai.get("profile") or ""),
            "name": str(ai.get("name") or ""),
            "version": str(ai.get("version") or ""),
            "provider": provider,
            "provider_label": provider_label,
            "model_name": model_name,
            "provider_base_url": provider_base_url or None,
            "effective_base_url": effective_base_url or None,
            "special_instruction": str(ai.get("special_instruction") or ""),
            "system_instruction": str(ai.get("system_instruction") or ""),
            "assistant_instruction": str(ai.get("assistant_instruction") or ""),
            "host_runtime_name": _host_runtime_name_from_payload(host_agent),
            "api_key": api_key,
            "tavily_api_key": tavily_api_key,
            "has_api_key": bool(api_key),
            "has_tavily_api_key": bool(tavily_api_key),
        },
        "available_versions": [],
    }


def _get_direct_ai_setup_engine() -> Any | None:
    database_url = (A2AAppSettings.from_env().database_url or "").strip()
    if not database_url:
        return None
    global _DIRECT_AI_SETUP_ENGINE
    with _DIRECT_AI_SETUP_ENGINE_LOCK:
        if _DIRECT_AI_SETUP_ENGINE is not None:
            return _DIRECT_AI_SETUP_ENGINE
        try:
            import sqlalchemy as sa
        except Exception:
            return None
        _DIRECT_AI_SETUP_ENGINE = sa.create_engine(_normalize_database_url(database_url), future=True, pool_pre_ping=True)
        return _DIRECT_AI_SETUP_ENGINE


def _get_workspace_ai_setup_from_database(*, profile_id: str) -> dict[str, Any] | None:
    engine = _get_direct_ai_setup_engine()
    if engine is None:
        return None
    try:
        import sqlalchemy as sa

        with engine.begin() as conn:
            ai_row = conn.execute(
                sa.text("select payload from a2a_workspace_ai_settings where profile = :profile limit 1"),
                {"profile": profile_id},
            ).first()
            if ai_row is None:
                return {"configured": False, "agent": None, "available_versions": []}
            ai_payload = _json_row_payload(ai_row[0])
            version_id = str(ai_payload.get("version") or "").strip()
            version_payload: dict[str, Any] | None = None
            if version_id:
                version_row = conn.execute(
                    sa.text("select payload from a2a_model_versions where id = :version_id limit 1"),
                    {"version_id": version_id},
                ).first()
                if version_row is not None:
                    version_payload = _json_row_payload(version_row[0])
            host_row = conn.execute(
                sa.text(
                    "select payload from a2a_workspace_agents "
                    "where profile = :profile and slug = 'host' and is_enabled = true limit 1"
                ),
                {"profile": profile_id},
            ).first()
            host_payload = _json_row_payload(host_row[0]) if host_row is not None else None
        return _workspace_ai_setup_payload_from_rows(
            ai=ai_payload,
            version=version_payload,
            host_agent=host_payload,
        )
    except Exception:
        logger.debug("voice direct AI setup DB load failed", extra={"profile_id": profile_id}, exc_info=True)
        return None


async def _get_workspace_ai_setup_cached(
    control_plane: ControlPlaneClient,
    *,
    profile_id: str,
) -> dict[str, Any]:
    ttl_s = max(0.0, _env_float("KA2A_VOICE_AI_SETUP_CACHE_TTL_S", 300.0))
    now = time.monotonic()
    if ttl_s > 0:
        with _AI_SETUP_CACHE_LOCK:
            cached = _AI_SETUP_CACHE.get(profile_id)
            if cached and cached[0] > now:
                logger.info("voice ai setup cache hit", extra={"profile_id": profile_id})
                return cached[1]

    setup_source = (os.getenv("KA2A_VOICE_AI_SETUP_SOURCE") or "database_first").strip().lower()
    setup: dict[str, Any] | None = None
    if setup_source in {"database", "database_first"}:
        setup = await asyncio.to_thread(_get_workspace_ai_setup_from_database, profile_id=profile_id)

    if setup is None and setup_source not in {"database"}:
        try:
            setup = await asyncio.to_thread(control_plane.get_internal_workspace_ai_setup, profile_id=profile_id)
        except Exception:
            logger.debug("voice control-plane AI setup load failed", extra={"profile_id": profile_id}, exc_info=True)

    if setup is None and setup_source not in {"control_plane"}:
        setup = await asyncio.to_thread(_get_workspace_ai_setup_from_database, profile_id=profile_id)

    if setup is None:
        setup = await asyncio.to_thread(control_plane.get_internal_workspace_ai_setup, profile_id=profile_id)
    if ttl_s > 0:
        with _AI_SETUP_CACHE_LOCK:
            _AI_SETUP_CACHE[profile_id] = (time.monotonic() + ttl_s, setup)
    return setup


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _jwt_from_env(prefix: str = "KA2A_JWT_") -> JwtBearerConfig | None:
    enabled = _parse_bool(os.getenv(f"{prefix}ENABLED"), default=False)
    key = (os.getenv(f"{prefix}KEY") or "").strip()
    key_path = (os.getenv(f"{prefix}KEY_PATH") or "").strip()
    jwks_url = (os.getenv(f"{prefix}JWKS_URL") or "").strip()
    if not enabled and not key and not key_path and not jwks_url:
        return None

    secret = key
    if not secret and key_path:
        secret = pathlib.Path(key_path).read_text(encoding="utf-8").strip().replace("\\n", "\n")
    elif secret:
        secret = secret.replace("\\n", "\n")
    if not (secret or jwks_url):
        raise RuntimeError(f"{prefix}KEY/{prefix}KEY_PATH or {prefix}JWKS_URL is required when JWT auth is enabled.")

    algorithms = _split_csv(os.getenv(f"{prefix}ALGORITHMS")) or ["HS256"]
    include_claims_env = os.getenv(f"{prefix}INCLUDE_CLAIMS")
    if include_claims_env is None:
        include_claims = True
    else:
        include_claims = _parse_bool(include_claims_env, default=False)

    jwks_headers_raw = (os.getenv(f"{prefix}JWKS_HEADERS") or "").strip()
    jwks_headers: dict[str, str] | None = None
    if jwks_headers_raw:
        try:
            parsed = json.loads(jwks_headers_raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            jwks_headers = {str(key): str(value) for key, value in parsed.items()}

    return JwtBearerConfig(
        secret=secret or "",
        algorithms=algorithms,
        audience=(os.getenv(f"{prefix}AUDIENCE") or "").strip() or None,
        issuer=(os.getenv(f"{prefix}ISSUER") or "").strip() or None,
        leeway_s=int(os.getenv(f"{prefix}LEEWAY_S") or "0"),
        user_claim=(os.getenv(f"{prefix}USER_CLAIM") or "sub").strip(),
        tenant_claim=(os.getenv(f"{prefix}TENANT_CLAIM") or "").strip() or None,
        forward_bearer_token=_parse_bool(os.getenv(f"{prefix}FORWARD_BEARER_TOKEN"), default=False),
        include_claims=include_claims,
        jwks_url=jwks_url or None,
        jwks_cache_lifespan_s=float(os.getenv(f"{prefix}JWKS_CACHE_LIFESPAN_S") or "300"),
        jwks_timeout_s=float(os.getenv(f"{prefix}JWKS_TIMEOUT_S") or "30"),
        jwks_headers=jwks_headers,
    )


def _parse_metadata(raw_metadata: Any) -> dict[str, Any]:
    if isinstance(raw_metadata, dict):
        return raw_metadata
    if not raw_metadata:
        return {}
    if isinstance(raw_metadata, bytes):
        raw_metadata = raw_metadata.decode("utf-8", errors="ignore")
    if isinstance(raw_metadata, str):
        try:
            parsed = json.loads(raw_metadata)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _message_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, TextPart):
        return value.text.strip()
    if isinstance(value, DataPart):
        return _payload_summary_text(value.data)
    if isinstance(value, Message):
        chunks = [_message_text(part) for part in value.parts]
        return " ".join(chunk for chunk in chunks if chunk).strip()
    if isinstance(value, dict):
        text = str(value.get("text") or value.get("content") or "").strip()
        if text:
            return text
        parts = value.get("parts")
        if isinstance(parts, list):
            chunks: list[str] = []
            for part in parts:
                if isinstance(part, dict):
                    if part.get("kind") == "text":
                        piece = str(part.get("text") or "").strip()
                        if piece:
                            chunks.append(piece)
                    elif part.get("kind") == "data" and isinstance(part.get("data"), dict):
                        chunks.append(_message_text(part["data"]))
                else:
                    piece = _message_text(part)
                    if piece:
                        chunks.append(piece)
        return " ".join(chunk for chunk in chunks if chunk).strip()
    parts = getattr(value, "parts", None)
    if isinstance(parts, list):
        chunks: list[str] = []
        for part in parts:
            text = _message_text(part)
            if text:
                chunks.append(text)
        if chunks:
            return " ".join(chunks).strip()
    content = getattr(value, "content", None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = [str(item).strip() for item in content if str(item).strip()]
        if chunks:
            return " ".join(chunks).strip()
    return ""


def _message_role(value: Any) -> str:
    if isinstance(value, Message):
        return str(value.role.value if isinstance(value.role, Role) else value.role)
    if isinstance(value, dict):
        return str(value.get("role") or "").strip()
    role = getattr(value, "role", "")
    return str(role.value if isinstance(role, Role) else role).strip()


def _assistant_message_text(value: Any) -> str:
    role = _message_role(value)
    if role and role != Role.agent.value:
        return ""
    return _message_text(value)


def _transcript_comparison_key(text: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        re.sub(r"[^a-z0-9']+", " ", text.lower().replace("’", "'").replace("‘", "'").replace("`", "'")),
    ).strip()


def _collapse_repeated_phrase(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip())
    if not normalized:
        return ""
    words = normalized.split()
    for size in range(1, (len(words) // 2) + 1):
        if len(words) % size:
            continue
        phrase = words[:size]
        phrase_key = _transcript_comparison_key(" ".join(phrase))
        if phrase and all(
            _transcript_comparison_key(" ".join(words[index : index + size])) == phrase_key
            for index in range(0, len(words), size)
        ):
            return " ".join(phrase)
    return normalized


_VOICE_PROVIDER_KEY_ERROR_MESSAGE = (
    "I could not complete that request because the workspace AI provider key is invalid. "
    "Update the workspace AI settings, then retry."
)


def _is_provider_key_error_text(text: str) -> bool:
    lowered = text.lower()
    return (
        "invalid_api_key" in lowered
        or "incorrect api key" in lowered
        or ("upstream error (401)" in lowered and "openai" in lowered)
        or ("http 401" in lowered and "openai" in lowered)
    )


def _is_unsafe_voice_text(text: str) -> bool:
    lowered = text.lower()
    if _is_provider_key_error_text(text):
        return False
    return any(
        marker in lowered
        for marker in (
            "bearertoken",
            "access_token",
            "accesstoken",
            "authorization",
            "urn:ka2a:principal",
            "taskstate.",
            "taskstatus",
            "metadata={",
            "eyjhb",
            "sk-proj-",
            "api_key",
            "api key provided",
            '"error":',
            "'error':",
        )
    )


def _is_generic_voice_answer(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return normalized in {
        "completed",
        "working",
        "how can i help you today",
        "i m your workspace host agent what can i help you with",
        "i m checking that now",
        "i m checking that with the workspace agent now",
        "voice session connected i m listening now",
        "voice room connected i m preparing your workspace assistant",
    }


def _sanitize_voice_text(text: str, *, reject_generic: bool = False) -> str:
    cleaned = _collapse_repeated_phrase(re.sub(r"\s+", " ", text.strip()))
    if not cleaned:
        return ""
    if cleaned.strip().lower() in {"{}", "[]", "null", "none"}:
        return ""
    if not re.search(r"[a-zA-Z0-9]", cleaned):
        return ""
    if _is_provider_key_error_text(cleaned):
        return _VOICE_PROVIDER_KEY_ERROR_MESSAGE
    if _is_unsafe_voice_text(cleaned):
        return ""
    if reject_generic and _is_generic_voice_answer(cleaned):
        return ""
    return cleaned


def _humanize_voice_agent_label(value: str) -> str:
    normalized = re.sub(r"[_-]+", " ", value.strip())
    normalized = re.sub(r"\bwa p\d+\b", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\b[0-9a-f]{8,}\b", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .,-")
    return normalized or "workspace"


def _humanize_voice_progress_text(text: str) -> str:
    cleaned = _sanitize_voice_text(text)
    if not cleaned:
        return ""

    lowered = cleaned.lower()
    delegating_match = re.match(r"delegating this request to the (.+?) specialist agent\.?$", lowered, flags=re.IGNORECASE)
    if delegating_match:
        agent_label = _humanize_voice_agent_label(delegating_match.group(1))
        return f"I’ve handed this to the {agent_label} specialist."

    accepted_match = re.match(r"(.+?) agent accepted the delegated task\.?$", cleaned, flags=re.IGNORECASE)
    if accepted_match:
        agent_label = _humanize_voice_agent_label(accepted_match.group(1))
        return f"The {agent_label} specialist has accepted the task."

    processing_match = re.match(r"(.+?) agent is processing the delegated task\.?$", cleaned, flags=re.IGNORECASE)
    if processing_match:
        agent_label = _humanize_voice_agent_label(processing_match.group(1))
        return f"The {agent_label} specialist is working on it now."

    working_match = re.match(r"delegating this request to the appropriate specialist agent\.?$", lowered, flags=re.IGNORECASE)
    if working_match:
        return "I’ve handed this to the right specialist."

    if "task state: failed" in lowered or "current task state: failed" in lowered:
        return "That task ran into a problem. I’m checking what detail is missing."

    return cleaned


def _is_priority_voice_progress_update(text: str) -> bool:
    cleaned = _humanize_voice_progress_text(text)
    if not cleaned:
        return False
    lowered = cleaned.lower()
    return any(
        phrase in lowered
        for phrase in (
            "i’m checking that with the workspace agent now",
            "i've handed this to the",
            "i’ve handed this to the",
            "has accepted the task",
            "is working on it now",
            "ran into a problem",
            "needs more information",
            "requires authentication",
            "completed the delegated task",
        )
    )


def _voice_user_facing_failure_follow_up(text: str) -> str | None:
    cleaned = _sanitize_voice_text(text)
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if "time range" in lowered:
        return "I still need the time range for that. For example, you can say last month, past 90 days, or last year."
    if "not currently available" in lowered and "pos" in lowered:
        return "The sales and POS specialist is unavailable right now. I can help with inventory, products, or workspace tasks, or you can retry sales analysis later."
    if "not currently available" in lowered and "inventory" in lowered:
        return "The inventory specialist is unavailable right now. I can help with products, users, or workspace tasks, or you can retry inventory analysis later."
    if "not currently available" in lowered and "product" in lowered:
        return "The product specialist is unavailable right now. I can help with inventory, users, or workspace tasks, or you can retry the product request later."
    if "could not complete that answer" in lowered or "delegation failed" in lowered:
        return "I could not finish that request as asked. Please tell me what result you want, and include any missing detail like the time range, product, barcode, or location."
    return None


def _select_voice_response_candidate(candidates: list[str]) -> str:
    selected = ""
    seen: set[str] = set()
    for candidate in candidates:
        text = _sanitize_voice_text(candidate, reject_generic=True)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        selected = text
    return selected


def _payload_summary_text(payload: dict[str, Any]) -> str:
    candidates: list[str] = []
    for key in ("spoken_summary", "voice_summary", "summary", "explanation", "title", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
        elif isinstance(value, dict):
            nested_text = _payload_summary_text(value)
            if nested_text:
                candidates.append(nested_text)

    insights = payload.get("insights")
    if isinstance(insights, list):
        for item in insights[:2]:
            if isinstance(item, str) and item.strip():
                candidates.append(item.strip())
            elif isinstance(item, dict):
                for key in ("summary", "description", "text", "title"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        candidates.append(value.strip())
                        break

    cleaned: list[str] = []
    for candidate in candidates:
        text = _sanitize_voice_text(candidate)
        if text and text not in cleaned:
            cleaned.append(text)
    return " ".join(cleaned[:3]).strip()


def _artifact_speakable_text(value: Any) -> str:
    if isinstance(value, DataPart):
        return _payload_summary_text(value.data)
    if isinstance(value, Artifact):
        chunks = [_artifact_speakable_text(part) for part in value.parts]
        return " ".join(chunk for chunk in chunks if chunk).strip()
    if isinstance(value, TaskArtifactUpdateEvent):
        return _artifact_speakable_text(value.artifact)
    if isinstance(value, TaskStatusUpdateEvent):
        state = value.status.state
        if state not in _TERMINAL_TASK_STATES:
            return ""
        return _sanitize_voice_text(_assistant_message_text(value.status.message), reject_generic=True)
    if isinstance(value, Task):
        if value.status.state not in _TERMINAL_TASK_STATES:
            return ""
        artifact_chunks = [_artifact_speakable_text(artifact) for artifact in value.artifacts]
        artifact_text = " ".join(chunk for chunk in artifact_chunks if chunk).strip()
        if artifact_text:
            return _sanitize_voice_text(artifact_text, reject_generic=True)
        return _sanitize_voice_text(_assistant_message_text(value.status.message), reject_generic=True)
    if isinstance(value, dict):
        kind = str(value.get("kind") or "").strip()
        if kind == "data" and isinstance(value.get("data"), dict):
            return _payload_summary_text(value["data"])
        if kind == "message":
            return _sanitize_voice_text(_assistant_message_text(value), reject_generic=True)
        for key in ("status_update", "statusUpdate"):
            if isinstance(value.get(key), dict):
                return _artifact_speakable_text(value[key])
        for key in ("artifact_update", "artifactUpdate", "artifact"):
            if isinstance(value.get(key), dict):
                return _artifact_speakable_text(value[key])
        if isinstance(value.get("artifacts"), list):
            artifact_chunks = [_artifact_speakable_text(artifact) for artifact in value["artifacts"]]
            artifact_text = " ".join(chunk for chunk in artifact_chunks if chunk).strip()
            if artifact_text:
                return _sanitize_voice_text(artifact_text, reject_generic=True)
        if isinstance(value.get("status"), dict):
            state = str(value["status"].get("state") or "").strip()
            if state and state not in {task_state.value for task_state in _TERMINAL_TASK_STATES}:
                return ""
            return _artifact_speakable_text(value["status"].get("message"))
        return _payload_summary_text(value)
    return _assistant_message_text(value)


def _artifact_text(value: Any) -> str:
    if isinstance(value, Task):
        return _artifact_speakable_text(value)
    if isinstance(value, TaskStatusUpdateEvent):
        return _artifact_speakable_text(value)
    if isinstance(value, TaskArtifactUpdateEvent):
        return _artifact_speakable_text(value)
    if isinstance(value, dict):
        for key in ("message", "text", "content"):
            text = _artifact_speakable_text(value.get(key))
            if text:
                return text
        if isinstance(value.get("status"), dict):
            return _artifact_text(value["status"])
        if isinstance(value.get("artifact"), dict):
            return _artifact_text(value["artifact"])
    return _artifact_speakable_text(value)


_SENSITIVE_EVENT_KEYS = {
    "authorization",
    "bearertoken",
    "token",
    "accesstoken",
    "refreshtoken",
    "apikey",
    "apisecret",
    "secret",
    "claims",
    "urnka2aprincipal",
}


def _voice_turn_id(transcript: str) -> str:
    seed = f"{time.time_ns()}:{_transcript_comparison_key(transcript)}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _sensitive_key(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    if not normalized:
        return False
    return normalized in _SENSITIVE_EVENT_KEYS or normalized.endswith("token") or normalized.endswith("secret")


def _redact_sensitive_event_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if _sensitive_key(key):
                continue
            redacted[str(key)] = _redact_sensitive_event_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_event_payload(item) for item in value]
    return value


def _stream_payload_from_event(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        raw_payload = event.model_dump(mode="json", by_alias=True, exclude_none=True)
    elif isinstance(event, dict):
        raw_payload = event
    else:
        raw_payload = {}
    if not isinstance(raw_payload, dict):
        return {}
    return _redact_sensitive_event_payload(raw_payload)


def _build_workspace_instruction(settings: dict[str, Any]) -> str:
    agent = settings.get("agent") or {}
    parts = [
        "You are the LiveKit voice layer for the K-A2A workspace.",
        "Answer clearly, keep the user updated while work is running, and speak concise summaries of the host agent output.",
        "Do not mention internal transport details unless the user asks.",
        str(agent.get("special_instruction") or "").strip(),
        str(agent.get("system_instruction") or "").strip(),
        str(agent.get("assistant_instruction") or "").strip(),
    ]
    return "\n".join(part for part in parts if part)


def _normalize_openai_base_url(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    elif not path.endswith("/v1"):
        path = f"{path}/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _agent_profile_id(agent: dict[str, Any]) -> str:
    return str(agent.get("profile") or agent.get("profile_id") or agent.get("profileId") or "").strip()


def _agent_public_names(agent: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key in ("name", "slug", "source_template_slug"):
        value = str(agent.get(key) or "").strip()
        if value:
            names.add(value.lower())
    runtime_config = agent.get("runtime_config")
    if isinstance(runtime_config, dict):
        for key in ("name", "slug", "agentName", "runtimeName"):
            value = str(runtime_config.get(key) or "").strip()
            if value:
                names.add(value.lower())
    return names


def _resolve_runtime_agent_name(
    *,
    registry: dict[str, Any],
    profile_id: str,
    requested_name: str,
) -> str:
    requested = requested_name.strip()
    requested_lower = requested.lower()
    agents = registry.get("agents") if isinstance(registry, dict) else None
    if not isinstance(agents, list):
        return requested

    profile_agents = [agent for agent in agents if isinstance(agent, dict) and _agent_profile_id(agent) == profile_id]
    if not profile_agents:
        return requested

    for agent in profile_agents:
        runtime_name = str(agent.get("runtime_name") or "").strip()
        if runtime_name and runtime_name.lower() == requested_lower:
            return runtime_name

    for agent in profile_agents:
        runtime_name = str(agent.get("runtime_name") or "").strip()
        if runtime_name and requested_lower in _agent_public_names(agent):
            return runtime_name

    if requested_lower == "host":
        for agent in profile_agents:
            runtime_name = str(agent.get("runtime_name") or "").strip()
            public_names = _agent_public_names(agent)
            if runtime_name and ("host" in public_names or "-host-" in runtime_name.lower()):
                return runtime_name

    return requested


def _resolve_voice_host_name_from_setup(*, ai_setup: dict[str, Any], requested_name: str) -> str | None:
    requested = requested_name.strip()
    if requested.lower() != "host":
        return None
    agent_payload = ai_setup.get("agent") if isinstance(ai_setup, dict) else None
    if not isinstance(agent_payload, dict):
        return None
    runtime_name = str(agent_payload.get("host_runtime_name") or "").strip()
    return runtime_name or None


_VOICE_BUSINESS_TERMS = {
    "active-day",
    "activity",
    "analysis",
    "analyst",
    "audit",
    "barcode",
    "best",
    "business",
    "cashier",
    "catalog",
    "category",
    "coin",
    "compare",
    "customer",
    "dashboard",
    "expired",
    "expiry",
    "forecast",
    "import",
    "inventory",
    "invoice",
    "item",
    "items",
    "laggard",
    "leader",
    "limit",
    "location",
    "margin",
    "month",
    "order",
    "orders",
    "payment",
    "permission",
    "po",
    "pos",
    "price",
    "pricing",
    "product",
    "products",
    "purchase",
    "receiving",
    "reorder",
    "report",
    "restock",
    "revenue",
    "risk",
    "sale",
    "sales",
    "sell",
    "selling",
    "sold",
    "staff",
    "stock",
    "subscription",
    "supplier",
    "token",
    "top",
    "trend",
    "unit",
    "units",
    "user",
    "variant",
    "variants",
    "week",
    "year",
}
_VOICE_BUSINESS_PHRASES = (
    "best performing",
    "best-performing",
    "far behind",
    "how many",
    "how much",
    "last month",
    "last year",
    "low stock",
    "out of stock",
    "past month",
    "past year",
    "same period",
    "that period",
    "this month",
    "this year",
    "which location",
    "which product",
    "which variant",
)
_VOICE_REPEAT_PHRASES = (
    "again",
    "come again",
    "repeat",
    "repeat that",
    "say again",
    "say that again",
    "what did you say",
    "i did not hear",
    "i didn't hear",
    "i did not catch",
    "i didn't catch",
)

_VOICE_STATUS_REQUEST_PHRASES = (
    "any update",
    "are you done",
    "are you still checking",
    "give me an update",
    "how far",
    "how is it going",
    "is it done",
    "status of the job",
    "status of the task",
    "still checking",
    "still working",
    "what happened to the task",
    "what is going on",
    "what is happening",
    "what is the status",
    "what's going on",
    "what's happening",
    "what's the status",
    "where are we on that",
)


def _normalize_voice_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _voice_log_preview(text: str, *, max_chars: int = 160) -> str:
    preview = re.sub(r"\s+", " ", text.strip())
    if len(preview) <= max_chars:
        return preview
    return f"{preview[: max_chars - 1].rstrip()}…"


def _voice_direct_reply(transcript: str) -> str | None:
    normalized = _normalize_voice_text(transcript)
    compact = re.sub(r"[^a-z0-9\s']", " ", normalized)
    tokens = [token for token in compact.split() if token]
    if not tokens:
        return None
    joined = " ".join(tokens)
    if len(tokens) <= 10 and any(
        phrase in joined
        for phrase in (
            "hello",
            "hi",
            "hey",
            "are you there",
            "can you hear me",
            "you still there",
            "still there",
        )
    ):
        return "I’m here and listening. Ask me what you want checked in your inventory."
    if len(tokens) <= 8 and any(phrase in joined for phrase in ("thank you", "thanks", "okay", "ok")):
        return "You’re welcome."
    if len(tokens) <= 12 and any(phrase in joined for phrase in ("who are you", "what is your name", "your name")):
        return "I’m your Intera voice assistant, connected to your workspace agent."
    if len(tokens) <= 8 and "how are you" in joined:
        return "I’m doing well and I’m ready to help with your inventory, sales, products, and workspace questions."
    if len(tokens) <= 16 and any(
        phrase in joined
        for phrase in (
            "what can you do",
            "what can you do for me",
            "how can you help",
            "what do you do",
        )
    ):
        return (
            "I can help you check sales, inventory, products, stock levels, imports, purchase orders, "
            "locations, and other workspace questions. Tell me what you want me to check."
        )
    return None


def _voice_repeat_requested(transcript: str) -> bool:
    normalized = _normalize_voice_text(transcript)
    if not normalized:
        return False
    return any(phrase in normalized for phrase in _VOICE_REPEAT_PHRASES)


def _voice_status_requested(transcript: str) -> bool:
    normalized = _normalize_voice_text(transcript)
    if not normalized:
        return False
    return any(phrase in normalized for phrase in _VOICE_STATUS_REQUEST_PHRASES)


_VOICE_TIME_RANGE_HINTS = (
    "today",
    "yesterday",
    "this week",
    "last week",
    "this month",
    "last month",
    "this quarter",
    "last quarter",
    "this year",
    "last year",
    "past week",
    "past month",
    "past quarter",
    "past year",
    "past 7 days",
    "past 30 days",
    "past 90 days",
    "past 12 months",
    "for the last",
    "for last",
    "for the past",
    "for past",
    "in the last",
    "in the past",
    "over the last",
    "over the past",
    "between ",
    "from ",
    "since ",
)

_VOICE_TIME_RANGE_REQUIRED_HINTS = (
    "sales data",
    "sales report",
    "sales analysis",
    "inventory history",
    "inventory analysis",
    "analyze my sales",
    "analyse my sales",
    "analyze my inventory",
    "analyse my inventory",
    "best performing",
    "top performing",
    "top sellers",
    "best sellers",
    "trend",
    "history",
    "performance",
    "breakdown",
    "summary",
    "report",
)

_VOICE_FRAGMENT_ENDINGS = (
    "for",
    "from",
    "between",
    "during",
    "about",
    "with",
    "into",
    "over",
    "across",
    "compare",
    "compare it",
    "give me",
    "show me",
    "tell me",
    "analyze",
    "analyse",
    "check",
    "look at",
)


def _voice_has_time_range(transcript: str) -> bool:
    normalized = _normalize_voice_text(transcript)
    if not normalized:
        return False
    if any(hint in normalized for hint in _VOICE_TIME_RANGE_HINTS):
        return True
    if re.search(r"\b\d+\s+(day|days|week|weeks|month|months|quarter|quarters|year|years)\b", normalized):
        return True
    if re.search(r"\b20\d{2}\b", normalized):
        return True
    if re.search(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b", normalized):
        return True
    return False


def _voice_needs_time_range(transcript: str) -> bool:
    normalized = _normalize_voice_text(transcript)
    if not normalized:
        return False
    if _voice_has_time_range(normalized):
        return False
    return any(hint in normalized for hint in _VOICE_TIME_RANGE_REQUIRED_HINTS)


def _voice_is_likely_incomplete_fragment(transcript: str) -> bool:
    normalized = _normalize_voice_text(transcript)
    if not normalized:
        return True
    if normalized.endswith("?"):
        return False
    if any(normalized.endswith(ending) for ending in _VOICE_FRAGMENT_ENDINGS):
        return True
    tokens = [token for token in re.split(r"[^a-z0-9']+", normalized) if token]
    if len(tokens) <= 2 and not any(token in {"hi", "hello", "hey", "thanks", "thank", "okay", "ok"} for token in tokens):
        return True
    return False


def _voice_clarification_requirement(transcript: str) -> dict[str, str] | None:
    normalized = _normalize_voice_text(transcript)
    if not normalized:
        return {
            "kind": "continuation",
            "question": "I did not catch the full request. Please say the full question.",
        }
    if _voice_needs_time_range(normalized):
        return {
            "kind": "time_range",
            "question": "What time range should I use for that analysis?",
        }
    if _voice_is_likely_incomplete_fragment(normalized):
        return {
            "kind": "continuation",
            "question": "That request sounds incomplete. Please finish the question and I will send it through.",
        }
    return None


def _voice_response_satisfies_clarification(transcript: str, pending: dict[str, Any]) -> bool:
    kind = str(pending.get("kind") or "").strip()
    normalized = _normalize_voice_text(transcript)
    if not normalized:
        return False
    if kind == "time_range":
        return _voice_has_time_range(normalized)
    if kind == "continuation":
        return len([token for token in re.split(r"[^a-z0-9']+", normalized) if token]) >= 2 or _voice_has_time_range(normalized)
    return False


def _voice_merge_clarification_answer(original: str, answer: str, pending: dict[str, Any]) -> str:
    kind = str(pending.get("kind") or "").strip()
    if kind == "time_range":
        return _collapse_repeated_phrase(f"{original.strip()} {answer.strip()}")
    if kind == "continuation":
        return _collapse_repeated_phrase(f"{original.strip()} {answer.strip()}")
    return _collapse_repeated_phrase(answer.strip())


def _should_delegate_voice_transcript(transcript: str) -> bool:
    normalized = _normalize_voice_text(transcript)
    if not normalized:
        return False
    if _voice_direct_reply(normalized):
        return False
    if any(phrase in normalized for phrase in _VOICE_BUSINESS_PHRASES):
        return True
    tokens = {token for token in re.split(r"[^a-z0-9-]+", normalized) if token}
    if tokens & _VOICE_BUSINESS_TERMS:
        return True
    contextual_followup = (
        "what about",
        "what changed",
        "why",
        "show me",
        "tell me",
        "compare it",
        "drill down",
        "break it down",
        "which one",
        "which was",
        "what was",
        "give me",
    )
    if any(normalized.startswith(prefix) for prefix in contextual_followup):
        return True
    tokens = [token for token in re.split(r"[^a-z0-9-]+", normalized) if token]
    if len(tokens) >= 3:
        return True
    return False


@dataclass(slots=True)
class VoiceRuntimeContext:
    profile_id: str
    access_token: str
    user_email: str
    workspace_name: str
    participant_name: str
    host_agent_name: str
    principal: Principal
    metadata: dict[str, Any]


def _voice_request_metadata(runtime: VoiceRuntimeContext) -> dict[str, Any]:
    return with_principal(
        {
            "profileId": runtime.profile_id,
            "workspaceName": runtime.workspace_name,
            "participantName": runtime.participant_name,
            "userEmail": runtime.user_email,
        },
        runtime.principal,
    )


def _merge_voice_workspace_claims(*, principal: Principal, ai_setup: dict[str, Any]) -> Principal:
    from kafka_a2a.mainapps.agents.services import _secret_for_claim

    claims = dict(principal.claims or {})
    existing_ka2a = claims.get(KA2A_JWT_CLAIM_KEY)
    ka2a_claim = dict(existing_ka2a) if isinstance(existing_ka2a, dict) else {}
    agent_payload = ai_setup.get("agent") if isinstance(ai_setup.get("agent"), dict) else {}

    llm_claim: dict[str, Any] = dict(ka2a_claim.get("llm") or {}) if isinstance(ka2a_claim.get("llm"), dict) else {}
    provider = str(
        agent_payload.get("provider")
        or agent_payload.get("provider_slug")
        or agent_payload.get("provider_label")
        or llm_claim.get("provider")
        or ""
    ).strip()
    model_name = str(agent_payload.get("model_name") or llm_claim.get("model") or "").strip()
    base_url = str(
        agent_payload.get("effective_base_url")
        or agent_payload.get("provider_base_url")
        or llm_claim.get("baseUrl")
        or ""
    ).strip()
    api_key = str(agent_payload.get("api_key") or "").strip()

    if provider:
        llm_claim["provider"] = provider
    if model_name:
        llm_claim["model"] = model_name
    if base_url:
        llm_claim["baseUrl"] = base_url
    if api_key:
        llm_claim["apiKey"] = _secret_for_claim(api_key)
    if llm_claim:
        ka2a_claim["llm"] = llm_claim

    tavily_api_key = str(agent_payload.get("tavily_api_key") or "").strip()
    if tavily_api_key:
        ka2a_claim["tavily"] = {
            "apiKey": _secret_for_claim(tavily_api_key),
        }

    if ka2a_claim:
        ka2a_claim["v"] = int(ka2a_claim.get("v") or 1)
        claims[KA2A_JWT_CLAIM_KEY] = ka2a_claim

    merged_principal = principal.model_copy(deep=True)
    merged_principal.claims = claims
    return merged_principal


def _room_participant_identities(room: Any) -> set[str]:
    participants = getattr(room, "remote_participants", None)
    if not participants:
        return set()
    values = participants.values() if isinstance(participants, dict) else participants
    identities: set[str] = set()
    for participant in values:
        identity = str(getattr(participant, "identity", "") or "").strip()
        if identity:
            identities.add(identity)
    return identities


def _livekit_http_url(value: str | None) -> str:
    raw = (value or "").strip()
    if raw.startswith("wss://"):
        return f"https://{raw.removeprefix('wss://')}"
    if raw.startswith("ws://"):
        return f"http://{raw.removeprefix('ws://')}"
    return raw


async def _livekit_room_has_participant(room_name: str, expected_identity: str) -> bool | None:
    livekit_url = _livekit_http_url(os.getenv("LIVEKIT_URL") or os.getenv("NEXT_PUBLIC_LIVEKIT_URL"))
    api_key = (os.getenv("LIVEKIT_API_KEY") or "").strip()
    api_secret = (os.getenv("LIVEKIT_API_SECRET") or "").strip()
    if not livekit_url or not api_key or not api_secret or not room_name:
        return None

    try:
        from livekit import api
    except ImportError:
        return None

    client = api.LiveKitAPI(url=livekit_url, api_key=api_key, api_secret=api_secret)
    try:
        response = await client.room.list_participants(api.ListParticipantsRequest(room=room_name))
        participants = getattr(response, "participants", None) or []
        if expected_identity:
            return any(str(getattr(participant, "identity", "") or "").strip() == expected_identity for participant in participants)
        return bool(participants)
    except Exception:
        return None
    finally:
        await client.aclose()


async def _delete_livekit_room(room_name: str) -> None:
    livekit_url = _livekit_http_url(os.getenv("LIVEKIT_URL") or os.getenv("NEXT_PUBLIC_LIVEKIT_URL"))
    api_key = (os.getenv("LIVEKIT_API_KEY") or "").strip()
    api_secret = (os.getenv("LIVEKIT_API_SECRET") or "").strip()
    if not livekit_url or not api_key or not api_secret or not room_name:
        return

    try:
        from livekit import api
    except ImportError:
        return

    client = api.LiveKitAPI(url=livekit_url, api_key=api_key, api_secret=api_secret)
    try:
        await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
    except Exception:
        return
    finally:
        await client.aclose()


async def _publish_voice_room_event(
    room: Any,
    event_type: str,
    text: str,
    *,
    role: str = "assistant",
    payload: dict[str, Any] | None = None,
) -> None:
    cleaned_text = _sanitize_voice_text(text, reject_generic=False)
    if not cleaned_text and event_type != "a2a_event":
        return
    event_role = "user" if role == "user" else "assistant"
    try:
        participant = getattr(room, "local_participant", None)
        publish_data = getattr(participant, "publish_data", None)
        if not callable(publish_data):
            return
        event_payload = {
            **(payload or {}),
            "source": "ka2a_voice",
            "type": event_type,
            "role": event_role,
            "text": cleaned_text,
            "timestamp": int(time.time() * 1000),
        }
        publish_result = publish_data(
            json.dumps(event_payload),
            reliable=True,
            topic="ka2a.voice",
        )
        if inspect.isawaitable(publish_result):
            await publish_result
    except Exception:
        logger.debug("voice room data event publish failed", extra={"event_type": event_type}, exc_info=True)


def _context_room_name(ctx: Any) -> str:
    room = getattr(ctx, "room", None)
    room_name = str(getattr(room, "name", "") or "").strip()
    if room_name:
        return room_name

    job_room = getattr(getattr(ctx, "job", None), "room", None)
    if isinstance(job_room, str):
        return job_room.strip()
    return str(getattr(job_room, "name", "") or "").strip()


async def _wait_for_voice_participant(ctx: Any, expected_identity: str, room_name: str | None = None) -> bool:
    wait_s = max(0.0, float(os.getenv("KA2A_VOICE_PARTICIPANT_WAIT_S") or "6"))
    deadline = asyncio.get_running_loop().time() + wait_s
    expected = expected_identity.strip()
    resolved_room_name = (room_name or _context_room_name(ctx)).strip()

    while True:
        identities = _room_participant_identities(ctx.room)
        if expected and expected in identities:
            return True
        if not expected and identities:
            return True
        server_presence = await _livekit_room_has_participant(resolved_room_name, expected)
        if server_presence:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.2)


async def _voice_entrypoint(ctx: Any) -> None:
    room_name_for_log = _context_room_name(ctx)
    logger.info("voice entrypoint starting", extra={"room": room_name_for_log})
    try:
        from livekit import agents as lk_agents
        from livekit.agents import Agent, AgentSession, InterruptionOptions, TurnHandlingOptions, inference, room_io
        from livekit.agents.llm import StopResponse
        from livekit.plugins import openai
    except ImportError as exc:  # pragma: no cover - exercised when optional dependency is absent
        raise RuntimeError(
            "livekit-agents is not installed. Install kafka-a2a[voice] to run the LiveKit voice worker."
        ) from exc
    logger.info("voice optional dependencies loaded", extra={"room": room_name_for_log})

    runtime_shared_token = (os.getenv("KA2A_RUNTIME_SHARED_TOKEN") or "").strip() or None
    control_plane = ControlPlaneClient()
    jwt_cfg = _jwt_from_env()
    default_host_agent_name = (os.getenv("KA2A_VOICE_HOST_AGENT_NAME") or "host").strip() or "host"
    default_voice_name = (os.getenv("KA2A_VOICE_TTS_VOICE") or "marin").strip() or "marin"
    stt_model = (os.getenv("KA2A_VOICE_STT_MODEL") or "whisper-1").strip()
    llm_model_override = (os.getenv("KA2A_VOICE_LLM_MODEL") or "").strip() or None
    tts_model = (os.getenv("KA2A_VOICE_TTS_MODEL") or "gpt-4o-mini-tts").strip()

    class Ka2aVoiceAgent(Agent):
        def __init__(self, *, runtime: VoiceRuntimeContext, ai_setup: dict[str, Any], room: Any) -> None:
            self._runtime = runtime
            self._ai_setup = ai_setup
            self._host_agent_name = runtime.host_agent_name
            self._room = room
            self._delegation_lock = asyncio.Lock()
            self._client_start_lock = asyncio.Lock()
            self._last_spoken_response = ""
            self._closing = False
            self._caller_watch_task: asyncio.Task[None] | None = None
            self._active_delegation_task: asyncio.Task[str] | None = None
            self._transcript_buffer: list[str] = []
            self._transcript_flush_task: asyncio.Task[None] | None = None
            self._recent_transcript_keys: dict[str, float] = {}
            self._pending_clarification: dict[str, Any] | None = None
            self._client = Ka2aClient(
                transport=KafkaTransport(KafkaConfig.from_env()),
                config=Ka2aClientConfig(
                    client_id=f"ka2a-voice-{runtime.profile_id}",
                    request_timeout_s=float(os.getenv("KA2A_VOICE_REQUEST_TIMEOUT_S") or "60"),
                ),
            )
            self._client_started = False
            self._last_progress_update = ""
            self._last_progress_spoken_at = 0.0
            self._last_completed_result = ""
            super().__init__(instructions=_build_workspace_instruction(ai_setup))

        def _begin_shutdown(self) -> None:
            self._closing = True
            active_delegation_task = self._active_delegation_task
            if active_delegation_task is not None and not active_delegation_task.done():
                active_delegation_task.cancel()
            transcript_flush_task = self._transcript_flush_task
            if transcript_flush_task is not None and not transcript_flush_task.done():
                transcript_flush_task.cancel()

        async def _ensure_client(self) -> None:
            if self._closing:
                raise asyncio.CancelledError()
            if self._client_started:
                return
            async with self._client_start_lock:
                if self._closing:
                    raise asyncio.CancelledError()
                if self._client_started:
                    return
                client_start_timeout_s = max(3.0, _env_float("KA2A_VOICE_CLIENT_START_TIMEOUT_S", 12.0))
                logger.info(
                    "voice ka2a client starting",
                    extra={
                        "profile_id": self._runtime.profile_id,
                        "timeout_s": client_start_timeout_s,
                    },
                )
                await asyncio.wait_for(self._client.start(), timeout=client_start_timeout_s)
                self._client_started = True
                logger.info(
                    "voice ka2a client started",
                    extra={"profile_id": self._runtime.profile_id},
                )

        async def _say(self, text: str, *, allow_interruptions: bool = False) -> None:
            if self._closing:
                return
            session = getattr(self, "session", None)
            if session is None or not text.strip():
                return
            handle = session.say(text, allow_interruptions=allow_interruptions)
            wait_for_playout = getattr(handle, "wait_for_playout", None)
            if callable(wait_for_playout):
                await wait_for_playout()
            elif asyncio.iscoroutine(handle):
                await handle

        async def _publish_voice_event(
            self,
            event_type: str,
            text: str,
            *,
            role: str = "assistant",
            payload: dict[str, Any] | None = None,
        ) -> None:
            try:
                await _publish_voice_room_event(self._room, event_type, text, role=role, payload=payload)
            except Exception:
                logger.debug(
                    "voice data event publish failed",
                    extra={"profile_id": self._runtime.profile_id, "event_type": event_type},
                    exc_info=True,
                )

        async def _publish_visible_user_transcript(self, transcript: str) -> None:
            await self._publish_voice_event("transcript", transcript, role="user")

        async def _publish_host_synced_transcript(self, transcript: str, *, turn_id: str) -> None:
            await self._publish_voice_event(
                "transcript",
                transcript,
                role="user",
                payload={
                    "syncChat": True,
                    "turnId": turn_id,
                    "displayInTranscript": False,
                },
            )

        def _transcript_key(self, transcript: str) -> str:
            return _transcript_comparison_key(transcript)

        def _remember_progress_update(self, text: str) -> str:
            cleaned = _humanize_voice_progress_text(text)
            if cleaned:
                self._last_progress_update = cleaned
            return cleaned

        async def _speak_progress_update(self, text: str) -> None:
            cleaned = self._remember_progress_update(text)
            if not cleaned or self._closing:
                return
            now = time.monotonic()
            min_interval_s = max(2.0, _env_float("KA2A_VOICE_PROGRESS_SPEAK_MIN_INTERVAL_S", 4.5))
            is_priority_update = _is_priority_voice_progress_update(cleaned)
            if cleaned == self._last_spoken_response and now - self._last_progress_spoken_at < min_interval_s:
                return
            if not is_priority_update and now - self._last_progress_spoken_at < min_interval_s:
                return
            self._last_progress_spoken_at = now
            self._last_spoken_response = cleaned
            await self._say(cleaned, allow_interruptions=True)

        def _status_reply(self) -> str:
            active_delegation_task = self._active_delegation_task
            if active_delegation_task is not None and not active_delegation_task.done():
                if self._last_progress_update:
                    return self._last_progress_update
                return "I’m still checking that with the workspace agent now."
            if self._pending_clarification is not None:
                pending_question = str(self._pending_clarification.get("question") or "").strip()
                if pending_question:
                    return pending_question
            if self._last_completed_result:
                return "The last task is complete. The result is already in your workspace chat."
            if self._last_spoken_response:
                return self._last_spoken_response
            return "I’m ready for your next request."

        def _mark_transcript_for_processing(self, transcript: str, *, source: str) -> bool:
            key = self._transcript_key(transcript)
            if not key:
                return False
            now = time.monotonic()
            expiry_s = max(2.0, _env_float("KA2A_VOICE_TRANSCRIPT_DUPLICATE_WINDOW_S", 12.0))
            self._recent_transcript_keys = {
                existing_key: seen_at
                for existing_key, seen_at in self._recent_transcript_keys.items()
                if now - seen_at <= expiry_s
            }
            if key in self._recent_transcript_keys:
                logger.info(
                    "voice transcript duplicate skipped",
                    extra={
                        "profile_id": self._runtime.profile_id,
                        "source": source,
                        "word_count": len(transcript.split()),
                    },
                )
                return False
            self._recent_transcript_keys[key] = now
            return True

        def _cancel_pending_transcript_flush(self, *, clear_buffer: bool) -> None:
            transcript_flush_task = self._transcript_flush_task
            if transcript_flush_task is not None and not transcript_flush_task.done():
                transcript_flush_task.cancel()
            self._transcript_flush_task = None
            if clear_buffer:
                self._transcript_buffer.clear()

        def queue_final_user_transcript(self, transcript: str) -> None:
            if self._closing:
                return
            cleaned = _collapse_repeated_phrase(re.sub(r"\s+", " ", transcript.strip()))
            if not cleaned:
                return
            if self._transcript_buffer and self._transcript_key(self._transcript_buffer[-1]) == self._transcript_key(cleaned):
                return
            logger.info(
                "voice final transcript observed",
                extra={
                    "profile_id": self._runtime.profile_id,
                    "word_count": len(cleaned.split()),
                },
            )
            self._transcript_buffer.append(cleaned)
            self._cancel_pending_transcript_flush(clear_buffer=False)
            self._transcript_flush_task = asyncio.create_task(
                self._flush_buffered_user_transcript(),
                name="ka2a_voice_flush_buffered_transcript",
            )

        async def _flush_buffered_user_transcript(self) -> None:
            try:
                await asyncio.sleep(max(0.4, _env_float("KA2A_VOICE_TRANSCRIPT_FLUSH_DELAY_S", 1.6)))
            except asyncio.CancelledError:
                return
            if self._closing:
                return
            fragments = [fragment for fragment in self._transcript_buffer if fragment.strip()]
            self._transcript_buffer.clear()
            self._transcript_flush_task = None
            if not fragments:
                return
            unique_fragments: list[str] = []
            seen_fragment_keys: set[str] = set()
            for fragment in fragments:
                key = self._transcript_key(fragment)
                if not key or key in seen_fragment_keys:
                    continue
                seen_fragment_keys.add(key)
                unique_fragments.append(fragment)
            transcript = _collapse_repeated_phrase(" ".join(unique_fragments))
            if not transcript:
                return
            await self._handle_voice_transcript(transcript, source="transcript_event")

        async def _watch_caller_presence(self) -> None:
            room_name = str(self._runtime.metadata.get("roomName") or self._runtime.metadata.get("room_name") or "").strip()
            if not room_name:
                return
            initial_delay_s = max(0.0, float(os.getenv("KA2A_VOICE_CALLER_WATCH_INITIAL_DELAY_S") or "3"))
            interval_s = max(0.5, float(os.getenv("KA2A_VOICE_CALLER_WATCH_INTERVAL_S") or "2"))
            await asyncio.sleep(initial_delay_s)

            while True:
                presence = await _livekit_room_has_participant(room_name, self._runtime.participant_name)
                if presence is False:
                    logger.info(
                        "voice caller left; shutting down voice session",
                        extra={
                            "profile_id": self._runtime.profile_id,
                            "room": room_name,
                        },
                    )
                    self._begin_shutdown()
                    session = getattr(self, "session", None)
                    if session is not None:
                        session.shutdown(drain=False)
                    await _delete_livekit_room(room_name)
                    return
                await asyncio.sleep(interval_s)

        async def on_enter(self) -> None:  # type: ignore[override]
            logger.info(
                "voice session entered",
                extra={
                    "profile_id": self._runtime.profile_id,
                    "room": self._runtime.metadata.get("roomName") or self._runtime.metadata.get("room_name"),
                    "agent": self._host_agent_name,
                },
            )
            room_name = str(self._runtime.metadata.get("roomName") or self._runtime.metadata.get("room_name") or "").strip()
            if not await _wait_for_voice_participant(
                SimpleNamespace(room=self._room),
                self._runtime.participant_name,
                room_name,
            ):
                logger.info(
                    "voice session skipped because caller left before agent greeting",
                    extra={
                        "profile_id": self._runtime.profile_id,
                        "room": self._runtime.metadata.get("roomName") or self._runtime.metadata.get("room_name"),
                    },
                )
                session = getattr(self, "session", None)
                if session is not None:
                    session.shutdown(drain=False)
                return
            self._caller_watch_task = asyncio.create_task(self._watch_caller_presence())
            greeting = "Voice session connected. I’m listening now."
            await self._publish_voice_event("result", greeting)
            await self._say(greeting, allow_interruptions=False)

        async def _delegate_to_host(self, transcript: str, *, turn_id: str) -> str:
            if self._closing:
                raise asyncio.CancelledError()
            initial_status = "I’m checking that with the workspace agent now."
            await self._publish_voice_event(
                "status",
                initial_status,
                payload={"syncChat": True, "turnId": turn_id},
            )
            self._remember_progress_update(initial_status)
            await self._speak_progress_update(initial_status)
            try:
                await self._ensure_client()
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning(
                    "voice ka2a client start timed out",
                    extra={
                        "profile_id": self._runtime.profile_id,
                        "host_agent_name": self._host_agent_name,
                    },
                )
                final_text = "I could not connect to the workspace agent right now. Please try again in a moment."
                self._last_completed_result = final_text
                await self._publish_voice_event(
                    "result",
                    final_text,
                    payload={"syncChat": True, "turnId": turn_id, "voiceLocalResult": True},
                )
                return final_text
            except Exception:
                logger.exception(
                    "voice ka2a client start failed",
                    extra={
                        "profile_id": self._runtime.profile_id,
                        "host_agent_name": self._host_agent_name,
                    },
                )
                final_text = "I could not connect to the workspace agent right now. Please try again in a moment."
                self._last_completed_result = final_text
                await self._publish_voice_event(
                    "result",
                    final_text,
                    payload={"syncChat": True, "turnId": turn_id, "voiceLocalResult": True},
                )
                return final_text
            if self._closing:
                raise asyncio.CancelledError()
            logger.info(
                "delegating voice transcript to host",
                extra={
                    "profile_id": self._runtime.profile_id,
                    "transcript_length": len(transcript),
                    "transcript_preview": _voice_log_preview(transcript),
                    "host_agent_name": self._host_agent_name,
                },
            )
            user_message = Message(
                role=Role.user,
                parts=[TextPart(text=transcript)],
                metadata=_voice_request_metadata(self._runtime),
            )
            request_metadata = _voice_request_metadata(self._runtime)
            response_candidates: list[str] = []
            host_timeout_s = float(os.getenv("KA2A_VOICE_HOST_TIMEOUT_S") or "120")
            first_event_timeout_s = float(os.getenv("KA2A_VOICE_HOST_FIRST_EVENT_TIMEOUT_S") or "20")
            next_event_timeout_s = float(os.getenv("KA2A_VOICE_HOST_NEXT_EVENT_TIMEOUT_S") or "45")
            event_count = 0
            try:
                async with asyncio.timeout(host_timeout_s):
                    stream = await self._client.stream_message(
                        agent_name=self._host_agent_name,
                        message=user_message,
                        metadata=request_metadata,
                        timeout_s=host_timeout_s,
                    )
                    stream_iter = stream.__aiter__()
                    while True:
                        per_event_timeout_s = first_event_timeout_s if event_count == 0 else next_event_timeout_s
                        try:
                            event = await asyncio.wait_for(anext(stream_iter), timeout=per_event_timeout_s)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            logger.warning(
                                "voice delegation stalled waiting for host stream event",
                                extra={
                                    "profile_id": self._runtime.profile_id,
                                    "host_agent_name": self._host_agent_name,
                                    "turn_id": turn_id,
                                    "event_count": event_count,
                                    "per_event_timeout_s": per_event_timeout_s,
                                    "transcript_preview": _voice_log_preview(transcript),
                                },
                            )
                            await self._publish_voice_event(
                                "error",
                                "The workspace agent is taking too long to respond.",
                                payload={"syncChat": True, "turnId": turn_id, "voiceLocalResult": True},
                            )
                            return (
                                "The workspace agent is taking too long to respond. Please try again, or ask me to run it again."
                            )
                        event_count += 1
                        logger.info(
                            "voice delegation received host stream event",
                            extra={
                                "profile_id": self._runtime.profile_id,
                                "host_agent_name": self._host_agent_name,
                                "turn_id": turn_id,
                                "event_count": event_count,
                                "event_type": type(event).__name__,
                            },
                        )
                        if self._closing:
                            raise asyncio.CancelledError()
                        stream_payload = _stream_payload_from_event(event)
                        if stream_payload.get("kind"):
                            await self._publish_voice_event(
                                "a2a_event",
                                "",
                                payload={"syncChat": True, "turnId": turn_id, "event": stream_payload},
                            )
                        if isinstance(event, TaskStatusUpdateEvent):
                            interim_status_text = _sanitize_voice_text(_assistant_message_text(event.status.message))
                            if interim_status_text and not event.final:
                                spoken_status_text = self._remember_progress_update(interim_status_text)
                                if spoken_status_text:
                                    await self._publish_voice_event("status", spoken_status_text, payload={"turnId": turn_id})
                                    await self._speak_progress_update(spoken_status_text)
                            status_text = _artifact_speakable_text(event)
                            if event.final and status_text:
                                response_candidates.append(status_text)
                        elif isinstance(event, Task):
                            task_text = _artifact_speakable_text(event)
                            if task_text:
                                response_candidates.append(task_text)
                        elif isinstance(event, TaskArtifactUpdateEvent):
                            artifact_text = _artifact_speakable_text(event)
                            if artifact_text:
                                response_candidates.append(artifact_text)
                        else:
                            event_text = _artifact_speakable_text(event)
                            if event_text:
                                response_candidates.append(event_text)
            except asyncio.CancelledError:
                logger.info(
                    "voice delegation cancelled before host stream started",
                    extra={
                        "profile_id": self._runtime.profile_id,
                        "host_agent_name": self._host_agent_name,
                    },
                )
                raise
            except TimeoutError:
                logger.warning(
                    "voice delegation timed out waiting for host response",
                    extra={
                        "profile_id": self._runtime.profile_id,
                        "host_agent_name": self._host_agent_name,
                    },
                )
                await self._publish_voice_event(
                    "error",
                    "The analysis service is taking longer than expected.",
                    payload={"syncChat": True, "turnId": turn_id, "voiceLocalResult": True},
                )
                return (
                    "The analysis service is taking longer than expected. Please try again, or ask me to regenerate the analysis."
                )
            except Exception as exc:
                logger.warning(
                    "voice delegation failed before host stream started",
                    extra={
                        "profile_id": self._runtime.profile_id,
                        "host_agent_name": self._host_agent_name,
                        "error_type": type(exc).__name__,
                        "error": repr(exc),
                    },
                )
                await self._publish_voice_event(
                    "error",
                    "I could not reach the analysis service right now.",
                    payload={"syncChat": True, "turnId": turn_id, "voiceLocalResult": True},
                )
                return (
                    "I could not reach the analysis service right now. Please try again in a moment."
                )

            final_text = _select_voice_response_candidate(response_candidates)
            if not final_text.strip():
                final_text = (
                    "I could not complete that answer from the agent service. Please try again, or ask me to regenerate the analysis if you need fresh data."
                )
            follow_up_text = _voice_user_facing_failure_follow_up(final_text)
            if follow_up_text:
                final_text = follow_up_text

            logger.info(
                "voice transcript resolved",
                extra={
                    "profile_id": self._runtime.profile_id,
                    "final_text_length": len(final_text),
                    "host_event_count": event_count,
                },
            )
            self._last_completed_result = final_text.strip()
            self._last_progress_update = ""
            await self._publish_voice_event("result", final_text, payload={"syncChat": True, "turnId": turn_id})
            return final_text.strip()

        async def _handle_voice_transcript(self, transcript: str, *, source: str, turn_ctx: Any | None = None) -> str | None:
            transcript = _collapse_repeated_phrase(re.sub(r"\s+", " ", transcript.strip()))
            if not transcript:
                return None
            if not self._mark_transcript_for_processing(transcript, source=source):
                return None
            if _voice_status_requested(transcript):
                logger.info(
                    "voice transcript handled as status request",
                    extra={
                        "profile_id": self._runtime.profile_id,
                        "source": source,
                        "transcript_preview": _voice_log_preview(transcript),
                    },
                )
                await self._publish_visible_user_transcript(transcript)
                status_reply = self._status_reply()
                self._last_spoken_response = status_reply
                if turn_ctx is not None:
                    turn_ctx.add_message(role="assistant", content=status_reply)
                await self._publish_voice_event("result", status_reply)
                await self._say(status_reply, allow_interruptions=True)
                return status_reply
            if _voice_repeat_requested(transcript):
                logger.info(
                    "voice transcript handled as repeat request",
                    extra={
                        "profile_id": self._runtime.profile_id,
                        "source": source,
                        "transcript_preview": _voice_log_preview(transcript),
                    },
                )
                await self._publish_visible_user_transcript(transcript)
                repeat_text = self._last_spoken_response or "I do not have anything to repeat yet."
                if turn_ctx is not None:
                    turn_ctx.add_message(role="assistant", content=repeat_text)
                self._last_spoken_response = repeat_text
                await self._publish_voice_event("result", repeat_text)
                await self._say(repeat_text, allow_interruptions=True)
                return repeat_text

            pending_clarification = self._pending_clarification
            if pending_clarification is not None:
                await self._publish_visible_user_transcript(transcript)
                if _voice_response_satisfies_clarification(transcript, pending_clarification):
                    transcript = _voice_merge_clarification_answer(
                        str(pending_clarification.get("original") or ""),
                        transcript,
                        pending_clarification,
                    )
                    self._pending_clarification = None
                elif _should_delegate_voice_transcript(transcript) and not _voice_is_likely_incomplete_fragment(transcript):
                    self._pending_clarification = None
                else:
                    logger.info(
                        "voice transcript still waiting on clarification",
                        extra={
                            "profile_id": self._runtime.profile_id,
                            "source": source,
                            "transcript_preview": _voice_log_preview(transcript),
                            "clarification_kind": str(pending_clarification.get("kind") or ""),
                        },
                    )
                    follow_up_prompt = str(pending_clarification.get("question") or "Please give me the missing detail.")
                    self._last_spoken_response = follow_up_prompt
                    if turn_ctx is not None:
                        turn_ctx.add_message(role="assistant", content=follow_up_prompt)
                    await self._publish_voice_event("result", follow_up_prompt)
                    await self._say(follow_up_prompt, allow_interruptions=True)
                    return follow_up_prompt

            clarification = _voice_clarification_requirement(transcript)
            if clarification:
                logger.info(
                    "voice transcript needs clarification",
                    extra={
                        "profile_id": self._runtime.profile_id,
                        "source": source,
                        "transcript_preview": _voice_log_preview(transcript),
                        "clarification_kind": clarification["kind"],
                    },
                )
                await self._publish_visible_user_transcript(transcript)
                question = clarification["question"]
                self._pending_clarification = {
                    "kind": clarification["kind"],
                    "question": question,
                    "original": transcript,
                }
                self._last_spoken_response = question
                if turn_ctx is not None:
                    turn_ctx.add_message(role="assistant", content=question)
                await self._publish_voice_event("result", question)
                await self._say(question, allow_interruptions=True)
                return question
            direct_reply = _voice_direct_reply(transcript)
            if direct_reply:
                logger.info(
                    "voice transcript handled as direct reply",
                    extra={
                        "profile_id": self._runtime.profile_id,
                        "source": source,
                        "transcript_preview": _voice_log_preview(transcript),
                    },
                )
                await self._publish_visible_user_transcript(transcript)
                self._last_spoken_response = direct_reply
                if turn_ctx is not None:
                    turn_ctx.add_message(role="assistant", content=direct_reply)
                await self._publish_voice_event("result", direct_reply)
                await self._say(direct_reply, allow_interruptions=True)
                return direct_reply
            if not _should_delegate_voice_transcript(transcript):
                logger.info(
                    "voice transcript ignored as non-business audio",
                    extra={
                        "profile_id": self._runtime.profile_id,
                        "source": source,
                        "word_count": len(transcript.split()),
                        "transcript_preview": _voice_log_preview(transcript),
                    },
                )
                return None
            logger.info(
                "voice transcript accepted for host delegation",
                extra={
                    "profile_id": self._runtime.profile_id,
                    "source": source,
                    "word_count": len(transcript.split()),
                    "transcript_preview": _voice_log_preview(transcript),
                },
            )
            turn_id = _voice_turn_id(transcript)
            await self._publish_visible_user_transcript(transcript)
            await self._publish_host_synced_transcript(transcript, turn_id=turn_id)
            if not _voice_backend_delegation_enabled():
                acknowledgement = (
                    "I heard that. I’m sending it through the workspace chat so the full result appears there."
                )
                self._remember_progress_update(acknowledgement)
                self._last_spoken_response = acknowledgement
                if turn_ctx is not None:
                    turn_ctx.add_message(role="assistant", content=acknowledgement)
                await self._publish_voice_event("status", acknowledgement)
                await self._publish_voice_event(
                    "result",
                    acknowledgement,
                    payload={"syncChat": True, "turnId": turn_id, "voiceLocalResult": True},
                )
                await self._say(acknowledgement, allow_interruptions=True)
                return acknowledgement
            try:
                async with self._delegation_lock:
                    if self._closing:
                        raise asyncio.CancelledError()
                    delegation_task = asyncio.create_task(self._delegate_to_host(transcript, turn_id=turn_id))
                    self._active_delegation_task = delegation_task
                    try:
                        final_text = await delegation_task
                    finally:
                        if self._active_delegation_task is delegation_task:
                            self._active_delegation_task = None
            except asyncio.CancelledError:
                return None
            if self._closing:
                return None
            self._last_progress_update = ""
            self._last_spoken_response = final_text
            if turn_ctx is not None:
                turn_ctx.add_message(role="assistant", content=final_text)
            await self._say(final_text, allow_interruptions=True)
            return final_text

        async def on_user_turn_completed(self, turn_ctx, new_message) -> None:  # type: ignore[override]
            if self._closing:
                raise StopResponse()
            self._cancel_pending_transcript_flush(clear_buffer=False)
            transcript = _collapse_repeated_phrase(_message_text(new_message))
            # LiveKit can endpoint a user's sentence into multiple completed turns when
            # they pause mid-thought. Buffer completed turns and send one consolidated
            # prompt to A2A after a short silence window instead of delegating fragments.
            self.queue_final_user_transcript(transcript)
            raise StopResponse()

        async def on_exit(self) -> None:  # type: ignore[override]
            self._begin_shutdown()
            caller_watch_task = self._caller_watch_task
            if caller_watch_task is not None and caller_watch_task is not asyncio.current_task():
                caller_watch_task.cancel()
                try:
                    await caller_watch_task
                except asyncio.CancelledError:
                    pass
            self._caller_watch_task = None
            transcript_flush_task = self._transcript_flush_task
            if transcript_flush_task is not None and transcript_flush_task is not asyncio.current_task():
                transcript_flush_task.cancel()
                try:
                    await transcript_flush_task
                except asyncio.CancelledError:
                    pass
            self._transcript_flush_task = None
            if self._client_started:
                await self._client.stop()
                self._client_started = False

    metadata = _parse_metadata(getattr(getattr(ctx, "job", None), "metadata", ""))
    profile_id = str(metadata.get("profileId") or metadata.get("profile_id") or "").strip()
    access_token = str(metadata.get("accessToken") or metadata.get("access_token") or "").strip()
    user_email = str(metadata.get("userEmail") or metadata.get("user_email") or "").strip()
    workspace_name = str(metadata.get("workspaceName") or metadata.get("workspace_name") or "").strip()
    participant_name = str(metadata.get("participantName") or metadata.get("participant_name") or "").strip()
    host_agent_name = str(metadata.get("hostAgentName") or metadata.get("host_agent_name") or default_host_agent_name).strip()
    room_name = str(metadata.get("roomName") or metadata.get("room_name") or _context_room_name(ctx)).strip()
    if room_name:
        metadata["roomName"] = room_name

    if not profile_id:
        raise RuntimeError("LiveKit voice metadata is missing profileId.")
    if not access_token:
        raise RuntimeError("LiveKit voice metadata is missing accessToken.")

    logger.info(
        "voice metadata received",
        extra={
            "profile_id": profile_id,
            "room": room_name,
            "participant_present": bool(participant_name),
            "host_agent_name": host_agent_name,
        },
    )
    auth = build_agent_auth_context(token=access_token, jwt=jwt_cfg)
    if auth.profile_id != profile_id:
        raise RuntimeError("LiveKit voice metadata profile does not match the authenticated workspace.")
    require_permission(auth, "oral_conversation_with_ai")
    logger.info("voice auth accepted", extra={"profile_id": profile_id, "room": room_name})
    logger.info("voice room connect starting", extra={"profile_id": profile_id, "room": room_name})
    await ctx.connect()
    logger.info("voice room connected", extra={"profile_id": profile_id, "room": room_name})
    await _publish_voice_room_event(ctx.room, "status", "Voice room connected. I’m preparing your workspace assistant.")
    principal = Principal(
        user_id=auth.user_id,
        tenant_id=auth.profile_id,
        bearer_token=auth.bearer_token,
        claims=auth.claims,
    )

    try:
        setup_timeout_s = max(5.0, float(os.getenv("KA2A_VOICE_AI_SETUP_TIMEOUT_S") or "20"))
        logger.info("voice ai setup load starting", extra={"profile_id": profile_id, "room": room_name})
        ai_setup = await asyncio.wait_for(
            _get_workspace_ai_setup_cached(control_plane, profile_id=profile_id),
            timeout=setup_timeout_s,
        )
        logger.info("voice ai setup loaded", extra={"profile_id": profile_id, "room": room_name})
    except ControlPlaneError as exc:
        raise RuntimeError(str(exc)) from exc

    if not ai_setup.get("configured"):
        raise RuntimeError("Workspace AI settings are not configured for voice.")

    principal = _merge_voice_workspace_claims(principal=principal, ai_setup=ai_setup)

    agent_payload = ai_setup.get("agent") or {}
    resolved_host_agent_name = host_agent_name
    setup_host_name = _resolve_voice_host_name_from_setup(ai_setup=ai_setup, requested_name=host_agent_name)
    if setup_host_name:
        resolved_host_agent_name = setup_host_name
    elif _parse_bool(os.getenv("KA2A_VOICE_RESOLVE_HOST_FROM_REGISTRY"), default=False):
        try:
            registry_timeout_s = max(2.0, float(os.getenv("KA2A_VOICE_REGISTRY_TIMEOUT_S") or "5"))
            logger.info("voice runtime registry load starting", extra={"profile_id": profile_id, "room": room_name})
            registry = await asyncio.wait_for(
                asyncio.to_thread(control_plane.list_internal_runtime_registry),
                timeout=registry_timeout_s,
            )
            resolved_host_agent_name = _resolve_runtime_agent_name(
                registry=registry,
                profile_id=profile_id,
                requested_name=host_agent_name,
            )
        except Exception as exc:
            logger.warning(
                "voice host runtime resolution failed; falling back to public host name",
                extra={"profile_id": profile_id, "host_agent_name": host_agent_name, "error": repr(exc)},
            )
    logger.info(
        "voice host runtime resolved",
        extra={
            "profile_id": profile_id,
            "requested_host_agent_name": host_agent_name,
            "resolved_host_agent_name": resolved_host_agent_name,
        },
    )

    runtime = VoiceRuntimeContext(
        profile_id=profile_id,
        access_token=access_token,
        user_email=user_email or (auth.claims.get("email") if isinstance(auth.claims.get("email"), str) else ""),
        workspace_name=workspace_name
        or str(auth.claims.get("company_name") or auth.claims.get("profile_name") or "").strip(),
        participant_name=participant_name or (user_email or auth.user_id),
        host_agent_name=resolved_host_agent_name,
        principal=principal,
        metadata=metadata,
    )

    llm_base_url = str(agent_payload.get("effective_base_url") or agent_payload.get("provider_base_url") or "").strip() or None
    voice_base_url = _normalize_openai_base_url(
        os.getenv("KA2A_VOICE_OPENAI_BASE_URL")
        or llm_base_url
        or "https://api.openai.com/v1"
    )
    llm_api_key = str(agent_payload.get("api_key") or "").strip() or None
    llm_model = llm_model_override or str(agent_payload.get("model_name") or "").strip() or "gpt-4o-mini"
    tts_voice = default_voice_name
    tts_api_key = llm_api_key
    tts_base_url = voice_base_url

    if not llm_api_key:
        raise RuntimeError("Workspace AI settings do not include an API key for LiveKit voice.")

    logger.info(
        "voice session components initializing",
        extra={
            "profile_id": profile_id,
            "room": room_name,
            "llm_model": llm_model,
            "stt_model": stt_model,
            "tts_model": tts_model,
            "tts_voice": tts_voice,
        },
    )
    logger.info("voice vad initializing", extra={"profile_id": profile_id, "room": room_name})
    vad = inference.VAD(model="silero", min_speech_duration=0.1, min_silence_duration=0.35)
    logger.info("voice vad initialized", extra={"profile_id": profile_id, "room": room_name})
    session = AgentSession(
        stt=openai.STT(
            base_url=voice_base_url,
            model=stt_model,
            api_key=llm_api_key,
        ),
        vad=vad,
        llm=openai.LLM(
            base_url=voice_base_url,
            model=llm_model,
            api_key=llm_api_key,
        ),
        tts=openai.TTS(
            base_url=tts_base_url,
            model=tts_model,
            voice=tts_voice,
            api_key=tts_api_key,
        ),
        turn_handling=TurnHandlingOptions(
            endpointing={"mode": "dynamic", "min_delay": 0.9, "max_delay": 2.0, "alpha": 0.85},
            interruption=InterruptionOptions(enabled=True, min_duration=0.1, min_words=1, resume_false_interruption=False),
            preemptive_generation={"enabled": False},
        ),
    )
    agent = Ka2aVoiceAgent(runtime=runtime, ai_setup=ai_setup, room=ctx.room)

    def _on_user_input_transcribed(event: Any) -> None:
        if not bool(getattr(event, "is_final", False)):
            return
        agent.queue_final_user_transcript(str(getattr(event, "transcript", "") or ""))

    if _parse_bool(os.getenv("KA2A_VOICE_TRANSCRIPT_EVENT_FALLBACK_ENABLED"), default=False):
        session.on("user_input_transcribed", _on_user_input_transcribed)

    if room_name:
        participant_present = await _livekit_room_has_participant(room_name, runtime.participant_name)
        if participant_present is False:
            logger.info(
                "voice session skipped because caller left before room start",
                extra={"profile_id": profile_id, "room": room_name},
            )
            await _delete_livekit_room(room_name)
            return

    logger.info("voice agent session starting", extra={"profile_id": profile_id, "room": room_name})
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(),
            audio_output=room_io.AudioOutputOptions(),
        ),
    )
    logger.info("voice agent session started", extra={"profile_id": profile_id, "room": room_name})


def build_voice_server():
    try:
        from livekit.agents import JobExecutorType
        from livekit import agents as lk_agents
        # Register the plugin on the main thread before thread-executed jobs start.
        from livekit.plugins import openai as _openai_plugin  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised when optional dependency is absent
        raise RuntimeError(
            "livekit-agents is not installed. Install kafka-a2a[voice] to run the LiveKit voice worker."
        ) from exc

    default_agent_name = (os.getenv("KA2A_LIVEKIT_AGENT_NAME") or "ka2a-voice").strip() or "ka2a-voice"
    server_options: dict[str, Any] = {}
    executor_type = (os.getenv("KA2A_VOICE_JOB_EXECUTOR_TYPE") or "").strip().lower()
    if executor_type:
        if executor_type not in {"process", "thread"}:
            raise RuntimeError("KA2A_VOICE_JOB_EXECUTOR_TYPE must be either 'process' or 'thread'.")
        server_options["job_executor_type"] = JobExecutorType.THREAD if executor_type == "thread" else JobExecutorType.PROCESS
    idle_processes = (os.getenv("KA2A_VOICE_NUM_IDLE_PROCESSES") or "").strip()
    if idle_processes:
        server_options["num_idle_processes"] = max(0, int(idle_processes))
    load_threshold = (os.getenv("KA2A_VOICE_LOAD_THRESHOLD") or "").strip()
    if load_threshold:
        server_options["load_threshold"] = max(0.1, float(load_threshold))
    initialize_timeout = (os.getenv("KA2A_VOICE_INITIALIZE_PROCESS_TIMEOUT_S") or "").strip()
    if initialize_timeout:
        server_options["initialize_process_timeout"] = max(10.0, float(initialize_timeout))
    server = lk_agents.AgentServer(**server_options)
    server.rtc_session(agent_name=default_agent_name)(_voice_entrypoint)
    return server


def run_voice_server() -> None:
    try:
        from livekit.agents import cli
    except ImportError as exc:  # pragma: no cover - exercised when optional dependency is absent
        raise SystemExit("Install kafka-a2a[voice] to run the LiveKit voice worker.") from exc

    server = build_voice_server()
    original_argv = sys.argv[:]
    try:
        sys.argv = [original_argv[0] if original_argv else "ka2a-voice", "start"]
        cli.run_app(server)
    finally:
        sys.argv = original_argv
