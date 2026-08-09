from __future__ import annotations

import importlib
import hashlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kafka_a2a.tools import ToolContext, ToolExecutor, ToolSpec


logger = logging.getLogger("kafka_a2a.mcp_tools")


def _require_mcp() -> Any:
    try:
        import httpx  # type: ignore
        from mcp import ClientSession  # type: ignore
        from mcp.client.streamable_http import streamable_http_client  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("MCP extras not installed. Install the `mcp` extra (e.g. `uv sync --extra mcp`).") from exc
    return httpx, ClientSession, streamable_http_client


def _strip(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _short_agent_name(value: str | None) -> str:
    return _strip(value) or "unknown"


def _format_log_kv(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)


def _log_mcp_operation(level: str, event: str, **fields: Any) -> None:
    ordered = [f"event={event}"]
    for key, value in fields.items():
        if value in (None, "", [], {}, ()):
            continue
        ordered.append(f"{key}={_format_log_kv(value)}")
    message = "mcp " + " ".join(ordered)
    getattr(logger, level)(message)


def _result_preview(value: Any) -> str:
    try:
        if hasattr(value, "model_dump"):
            value = value.model_dump(by_alias=True, exclude_none=True)
        if isinstance(value, dict):
            keys = sorted(str(key) for key in value.keys())
            return f"dict(keys={keys[:12]})"
        if isinstance(value, list):
            return f"list(len={len(value)})"
        return type(value).__name__
    except Exception:
        return type(value).__name__


def _extract_json_candidate_from_text(text: str) -> str | None:
    raw = str(text or "").strip()
    if not raw:
        return None

    import re

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


def _extract_json_value_from_text(text: str) -> Any:
    candidate = _extract_json_candidate_from_text(text)
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _header_names(headers: Mapping[str, str]) -> list[str]:
    names: set[str] = set()
    for key in headers:
        normalized = _strip(str(key).lower())
        if normalized:
            names.add(normalized)
    return sorted(names)


def _build_mcp_failure_message(
    *,
    operation: str,
    phase: str,
    server_url: str,
    headers: Mapping[str, str],
    timeout_s: float,
    remote_tool: str | None = None,
    argument_keys: list[str] | None = None,
    error: Exception,
) -> str:
    details = [
        f"server '{server_url}'",
        f"headers={_format_log_kv(_header_names(headers))}",
        f"timeout_s={float(timeout_s)}",
    ]
    if remote_tool:
        details.append(f"remote_tool='{remote_tool}'")
    if argument_keys:
        details.append(f"argument_keys={_format_log_kv(argument_keys)}")
    return f"MCP {operation} failed during {phase} for " + ", ".join(details) + f": {error}"


def _is_ignorable_cleanup_error(error: Exception) -> bool:
    message = _strip(str(error).lower())
    if not message:
        return False
    return "connection is closed" in message


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(word[:1].upper() + word[1:] for word in parts[1:])


def _import_path(path: str) -> Any:
    if ":" not in path:
        raise ValueError("Import path must look like 'pkg.module:attr'")
    module_name, attr = path.split(":", 1)
    mod = importlib.import_module(module_name)
    obj = getattr(mod, attr, None)
    if obj is None:
        raise ValueError(f"Import not found: {path}")
    return obj


def _load_tool_executor(path: str | None) -> ToolExecutor | None:
    override = _strip(path)
    if not override:
        return None
    obj = _import_path(override)
    if callable(obj) and not hasattr(obj, "call_tool"):
        obj = obj()
    if not hasattr(obj, "list_tools") or not hasattr(obj, "call_tool"):
        raise ValueError("Tool executor import must expose list_tools/call_tool or be a callable returning one.")
    return obj  # type: ignore[return-value]


class _McpToolsModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class McpServerAuthConfig(_McpToolsModel):
    mode: str = "none"
    token: str | None = None
    token_env: str | None = None
    header_name: str = "authorization"
    scheme: str = "Bearer"

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in {
            "none",
            "static",
            "context",
            "forward_bearer",
            "service_account",
            "custom",
            "api_key_header",
            "oauth_user",
            "oauth_workspace",
            "http_message_signature",
        }:
            raise ValueError(
                "MCP auth mode must be one of: none, static, context, forward_bearer, service_account, custom, "
                "api_key_header, oauth_user, oauth_workspace, http_message_signature"
            )
        return normalized

    @field_validator("header_name", "scheme", "token", "token_env")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        return _strip(value)


class McpRuntimeConnectionConfig(_McpToolsModel):
    id: str
    connection_scope: str = "workspace"
    owner_user_id: str | None = None
    auth_type: str = ""
    status: str = "pending"
    server_url_override: str | None = None
    headers: dict[str, str] | None = None
    granted_scopes: list[str] = Field(default_factory=list)
    token_expires_at: str | None = None
    resource_owner_id: str | None = None
    resource_label: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("connection_scope", "auth_type", "status", "server_url_override", "owner_user_id")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        return _strip(value)

    @field_validator("headers")
    @classmethod
    def _normalize_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return McpServerConfig._normalize_headers(value)


class McpServerConfig(_McpToolsModel):
    id: str
    server_url: str
    tools: list[str] | None = None
    tool_name_prefix: str | None = None
    headers: dict[str, str] | None = None
    auth: McpServerAuthConfig = Field(default_factory=McpServerAuthConfig)
    runtime_connections: list[McpRuntimeConnectionConfig] = Field(default_factory=list)
    enabled: bool = True
    timeout_s: float | None = None

    @field_validator("id", "server_url", "tool_name_prefix")
    @classmethod
    def _strip_required_text(cls, value: str | None) -> str | None:
        return _strip(value)

    @field_validator("tools")
    @classmethod
    def _normalize_tools(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            name = _strip(item)
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out or None

    @field_validator("headers")
    @classmethod
    def _normalize_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        out: dict[str, str] = {}
        for key, item in value.items():
            k = _strip(str(key))
            v = _strip(str(item))
            if not k or v is None:
                continue
            out[k] = v
        return out or None


class McpAgentServerConfig(_McpToolsModel):
    ref: str | None = None
    id: str | None = None
    server_url: str | None = None
    tools: list[str] | None = None
    tool_name_prefix: str | None = None
    headers: dict[str, str] | None = None
    auth: McpServerAuthConfig | None = None
    runtime_connections: list[McpRuntimeConnectionConfig] | None = None
    enabled: bool | None = None
    timeout_s: float | None = None

    @field_validator("ref", "id", "server_url", "tool_name_prefix")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        return _strip(value)

    @field_validator("tools")
    @classmethod
    def _normalize_tools(cls, value: list[str] | None) -> list[str] | None:
        return McpServerConfig._normalize_tools(value)

    @field_validator("headers")
    @classmethod
    def _normalize_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return McpServerConfig._normalize_headers(value)

    def resolve(self, *, shared_servers: dict[str, McpServerConfig]) -> McpServerConfig:
        base = shared_servers.get(self.ref or "") if self.ref else None
        if self.ref and base is None:
            raise ValueError(f"Unknown MCP server reference '{self.ref}'.")

        identifier = self.id or (base.id if base is not None else None)
        server_url = self.server_url or (base.server_url if base is not None else None)
        if not identifier:
            raise ValueError("MCP agent server entry requires 'id' or a valid 'ref'.")
        if not server_url:
            raise ValueError(f"MCP agent server '{identifier}' requires 'serverUrl' or a valid 'ref'.")

        return McpServerConfig(
            id=identifier,
            server_url=server_url,
            tools=self.tools if self.tools is not None else (list(base.tools) if base and base.tools is not None else None),
            tool_name_prefix=(
                self.tool_name_prefix
                if self.tool_name_prefix is not None
                else (base.tool_name_prefix if base is not None else None)
            ),
            headers=self.headers if self.headers is not None else (dict(base.headers) if base and base.headers is not None else None),
            auth=self.auth if self.auth is not None else (base.auth.model_copy(deep=True) if base is not None else McpServerAuthConfig()),
            runtime_connections=(
                deepcopy(self.runtime_connections)
                if hasattr(self, "runtime_connections") and self.runtime_connections is not None
                else (deepcopy(base.runtime_connections) if base is not None else [])
            ),
            enabled=self.enabled if self.enabled is not None else (base.enabled if base is not None else True),
            timeout_s=self.timeout_s if self.timeout_s is not None else (base.timeout_s if base is not None else None),
        )


class McpAgentDefinition(_McpToolsModel):
    servers: list[McpAgentServerConfig] = Field(default_factory=list)


class McpAgentConfig(_McpToolsModel):
    servers: list[McpServerConfig] = Field(default_factory=list)


class McpAgentConfigFile(_McpToolsModel):
    version: int = 1
    servers: list[McpServerConfig] = Field(default_factory=list)
    shared_servers: list[McpServerConfig] = Field(default_factory=list)
    agents: dict[str, McpAgentDefinition] = Field(default_factory=dict)

    def resolve_agent(self, *, agent_name: str | None) -> McpAgentConfig:
        name = _strip(agent_name)
        if name and name in self.agents:
            shared_servers = {server.id: server for server in [*self.servers, *self.shared_servers]}
            return McpAgentConfig(
                servers=[
                    server.resolve(shared_servers=shared_servers)
                    for server in self.agents[name].servers
                ]
            )
        return McpAgentConfig(
            servers=[
                McpAgentServerConfig(
                    id=server.id,
                    server_url=server.server_url,
                    tools=list(server.tools) if server.tools is not None else None,
                    tool_name_prefix=server.tool_name_prefix,
                    headers=dict(server.headers) if server.headers is not None else None,
                    auth=server.auth.model_copy(deep=True),
                    runtime_connections=deepcopy(server.runtime_connections),
                    enabled=server.enabled,
                    timeout_s=server.timeout_s,
                )
                for server in self.servers
            ]
        )


@dataclass(slots=True)
class McpHttpToolExecutorConfig:
    server_url: str | None = None
    token: str | None = None
    timeout_s: float = 30.0
    tools_cache_s: float = 60.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "McpHttpToolExecutorConfig":
        env_map = env or os.environ

        server_url = _strip(env_map.get("KA2A_MCP_SERVER_URL"))

        token = _strip(env_map.get("KA2A_MCP_TOKEN"))
        if not token:
            token_env = _strip(env_map.get("KA2A_MCP_TOKEN_ENV"))
            if token_env:
                token = _strip(env_map.get(token_env))

        timeout_s = float(env_map.get("KA2A_MCP_TIMEOUT_S") or "30")
        tools_cache_s = float(env_map.get("KA2A_MCP_TOOLS_CACHE_S") or "60")

        return cls(server_url=server_url, token=token, timeout_s=timeout_s, tools_cache_s=tools_cache_s)


@dataclass(slots=True)
class MultiMcpToolExecutorConfig:
    servers: list[McpServerConfig] = field(default_factory=list)
    timeout_s: float = 30.0
    tools_cache_s: float = 60.0
    agent_name: str | None = None
    config_path: str | None = None

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        agent_name: str | None = None,
    ) -> "MultiMcpToolExecutorConfig":
        env_map = env or os.environ
        resolved_agent_name = _strip(agent_name or env_map.get("KA2A_MCP_AGENT_NAME") or env_map.get("KA2A_AGENT_NAME"))
        timeout_s = float(env_map.get("KA2A_MCP_TIMEOUT_S") or "30")
        tools_cache_s = float(env_map.get("KA2A_MCP_TOOLS_CACHE_S") or "60")
        config_path = _strip(env_map.get("KA2A_MCP_CONFIG_PATH"))
        if not config_path:
            return cls(
                servers=[],
                timeout_s=timeout_s,
                tools_cache_s=tools_cache_s,
                agent_name=resolved_agent_name,
                config_path=None,
            )

        try:
            with open(config_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except OSError as exc:
            raise RuntimeError(f"Unable to read MCP config file '{config_path}': {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid MCP config JSON in '{config_path}': {exc}") from exc

        parsed = McpAgentConfigFile.model_validate(raw)
        agent_cfg = parsed.resolve_agent(agent_name=resolved_agent_name)
        return cls(
            servers=[server for server in agent_cfg.servers if server.enabled],
            timeout_s=timeout_s,
            tools_cache_s=tools_cache_s,
            agent_name=resolved_agent_name,
            config_path=config_path,
        )


@dataclass(slots=True)
class _RemoteToolSpec:
    name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None


@dataclass(slots=True)
class _ConfiguredToolSpec:
    exposed_name: str
    remote_name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None

    def to_tool_spec(self) -> ToolSpec:
        return ToolSpec(name=self.exposed_name, description=self.description, input_schema=self.input_schema)


async def _run_mcp_session(
    *,
    server_url: str,
    headers: Mapping[str, str],
    timeout_s: float,
    operation: str,
    callback: Callable[[Any], Awaitable[Any]],
    remote_tool: str | None = None,
    argument_keys: list[str] | None = None,
) -> Any:
    httpx, ClientSession, streamable_http_client = _require_mcp()
    log_fields = {
        "operation": operation,
        "server_url": server_url,
        "timeout_s": float(timeout_s),
        "header_names": _header_names(headers),
        "remote_tool": remote_tool,
        "argument_keys": argument_keys,
    }
    phase = "connect_stream"
    started_at = time.monotonic()
    result: Any = None
    callback_succeeded = False

    _log_mcp_operation("info", "session_start", **log_fields)
    try:
        async with httpx.AsyncClient(
            headers=dict(headers),
            timeout=float(timeout_s),
            follow_redirects=True,
        ) as client:
            async with streamable_http_client(server_url, http_client=client) as (read_stream, write_stream, _):
                phase = "session_initialize"
                _log_mcp_operation("info", "session_connected", **log_fields)
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    phase = "session_operation"
                    _log_mcp_operation("info", "session_initialized", **log_fields)
                    result = await callback(session)
                    callback_succeeded = True
                    _log_mcp_operation(
                        "info",
                        "session_success",
                        **log_fields,
                        elapsed_ms=int((time.monotonic() - started_at) * 1000),
                        result_type=type(result).__name__,
                        result_preview=_result_preview(result),
                    )
    except Exception as exc:
        if callback_succeeded and _is_ignorable_cleanup_error(exc):
            _log_mcp_operation(
                "warning",
                "session_cleanup_ignored",
                **log_fields,
                phase=phase,
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return result
        _log_mcp_operation(
            "warning",
            "session_failed",
            **log_fields,
            phase=phase,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise RuntimeError(
            _build_mcp_failure_message(
                operation=operation,
                phase=phase,
                server_url=server_url,
                headers=headers,
                timeout_s=timeout_s,
                remote_tool=remote_tool,
                argument_keys=argument_keys,
                error=exc,
            )
        ) from exc
    return result


def _parse_remote_tool_specs(result: Any) -> list[_RemoteToolSpec]:
    raw_tools = result.get("tools", result) if isinstance(result, dict) else getattr(result, "tools", result)
    if not isinstance(raw_tools, list):
        return []

    out: list[_RemoteToolSpec] = []
    for item in raw_tools:
        name = getattr(item, "name", None) if not isinstance(item, dict) else item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        desc = getattr(item, "description", None) if not isinstance(item, dict) else item.get("description")
        schema = (
            (item.get("inputSchema") or item.get("input_schema")) if isinstance(item, dict)
            else (getattr(item, "inputSchema", None) or getattr(item, "input_schema", None))
        )
        out.append(
            _RemoteToolSpec(
                name=name.strip(),
                description=str(desc).strip() if isinstance(desc, str) and desc.strip() else "",
                input_schema=schema if isinstance(schema, dict) else None,
            )
        )
    return out


def _dump_result(result: Any) -> Any:
    if hasattr(result, "model_dump"):
        result = result.model_dump(by_alias=True, exclude_none=True)  # type: ignore[assignment]

    if isinstance(result, dict):
        structured = result.get("structuredContent")
        if structured is None:
            structured = result.get("structured_content")
        if structured is not None:
            return structured

        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                nested = item.get("structuredContent")
                if nested is None:
                    nested = item.get("structured_content")
                if nested is not None:
                    return nested
                if isinstance(item.get("data"), dict):
                    return item["data"]
                text = item.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                parsed = _extract_json_value_from_text(text)
                if parsed is not None:
                    return parsed

    return result


def _error_text_from_mcp_result(result: Any) -> str | None:
    if hasattr(result, "model_dump"):
        result = result.model_dump(by_alias=True, exclude_none=True)  # type: ignore[assignment]
    if not isinstance(result, dict) or not bool(result.get("isError")):
        return None

    content = result.get("content")
    if isinstance(content, list):
        messages: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                messages.append(text.strip())
        if messages:
            return "\n".join(messages)
    return "Remote MCP tool returned an error."


def _compact_tool_arguments(value: Any) -> Any:
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for key, item in value.items():
            if item is None:
                continue
            nested = _compact_tool_arguments(item)
            if nested is None:
                continue
            compacted[key] = nested
        return compacted
    if isinstance(value, list):
        compacted_list = [_compact_tool_arguments(item) for item in value]
        return [item for item in compacted_list if item is not None]
    return value


_INVENTORY_SCOPE_REQUIRED_READ_TOOLS = {
    "list_inventory_items",
    "search_inventory_items",
    "get_inventory_item_details",
    "get_inventory_alerts",
    "search_stock_balances",
    "get_stock_analytics",
}

_INVENTORY_SCOPE_REQUIRED_WRITE_TOOLS = {
    "create_stock_reservation",
    "transfer_location_stock",
    "adjust_inventory_item_stock",
}

_LOCATION_SCOPED_ORDER_WRITE_TOOLS = {
    "receive_purchase_order_items",
    "reserve_sales_order",
    "release_sales_order",
    "ship_sales_order",
    "dispatch_return_order",
}

_INVENTORY_MULTI_SCOPE_ADMIN_PERMISSIONS = {
    "manage_inventory_item_settings",
    "view_inventory_item_reports",
    "can_view_dashboard",
    "create_stock_location",
    "update_stock_location",
    "delete_stock_location",
}

_TOOL_PERMISSIONS_OWNER_ROLES = {
    "admin",
    "administrator",
    "manager",
    "owner",
    "super_admin",
    "superadmin",
}

_WORKSPACE_SCOPED_TOOL_WORKSPACE_FIELDS = {
    "search_events": "workspace_id",
    "get_event_timeline": "workspace_id",
    "get_staff_activity": "workspace_id",
    "get_product_activity": "workspace_id",
    "get_pos_activity": "workspace_id",
    "get_purchase_order_activity": "workspace_id",
    "get_realtime_dashboard_snapshot": "workspace_id",
    "get_permission_security_activity": "workspace_id",
    "get_usage_and_limits": "profile_id",
    "get_alert_summary": "workspace_id",
}

_WORKSPACE_SCOPED_TOOL_NAMES = set(_WORKSPACE_SCOPED_TOOL_WORKSPACE_FIELDS.keys())

_TOOL_PERMISSION_REQUIREMENTS: dict[str, set[str]] = {
    "search_events": {"view_audit_trail", "view_support_access_audit"},
    "get_event_timeline": {"view_audit_trail", "view_support_access_audit"},
    "get_staff_activity": {"view_audit_trail", "view_support_access_audit"},
    "get_product_activity": {"view_audit_trail", "view_support_access_audit"},
    "get_pos_activity": {"view_audit_trail", "view_support_access_audit"},
    "get_purchase_order_activity": {"view_audit_trail", "view_support_access_audit"},
    "get_realtime_dashboard_snapshot": {"view_audit_trail", "view_support_access_audit"},
    "get_permission_security_activity": {"view_audit_trail", "view_support_access_audit"},
    "get_usage_and_limits": {"workspace_owner"},
}

_INVENTORY_MULTI_SCOPE_ADMIN_ROLES = {
    "admin",
    "administrator",
    "inventory_admin",
    "inventory_manager",
    "manager",
    "owner",
    "super_admin",
    "superadmin",
}


def _nested_metadata_value(source: Any, path: str) -> Any:
    current = source
    for segment in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(segment)
        if current is None:
            return None
    return current


def _first_metadata_value(sources: list[Mapping[str, Any]], paths: list[str]) -> Any:
    for source in sources:
        for path in paths:
            value = _nested_metadata_value(source, path)
            if value not in (None, "", [], {}, ()):
                return value
    return None


def _tool_context_sources(ctx: ToolContext) -> list[Mapping[str, Any]]:
    sources: list[Mapping[str, Any]] = []
    if isinstance(ctx.metadata, Mapping):
        sources.append(ctx.metadata)
    principal_claims = ctx.principal.claims if ctx.principal else None
    if isinstance(principal_claims, Mapping):
        sources.append(principal_claims)
    return sources


def _tool_context_structural_location_id(ctx: ToolContext) -> str | None:
    sources = _tool_context_sources(ctx)
    value = _first_metadata_value(
        sources,
        [
            "primary_structural_location_id",
            "structural_location_id",
            "default_structural_location_id",
            "scope.primary_structural_location_id",
            "scope.structural_location_id",
            "bootstrap.scope.primary_structural_location_id",
            "bootstrap.scope.structural_location_id",
        ],
    )
    return _strip(str(value)) if value not in (None, "") else None


def _tool_context_role(ctx: ToolContext) -> str | None:
    sources = _tool_context_sources(ctx)
    value = _first_metadata_value(
        sources,
        [
            "role",
            "session.role",
            "bootstrap.session.role",
            "user.role",
        ],
    )
    return _strip(str(value).lower()) if value not in (None, "") else None


def _tool_context_permissions(ctx: ToolContext) -> set[str]:
    sources = _tool_context_sources(ctx)
    raw = _first_metadata_value(
        sources,
        [
            "permissions",
            "session.permissions",
            "bootstrap.session.permissions",
            "user.permissions",
        ],
    )
    if isinstance(raw, str):
        return {item.strip() for item in raw.split(",") if item.strip()}
    if isinstance(raw, (list, tuple, set)):
        return {str(item).strip() for item in raw if str(item).strip()}
    return set()


def _tool_context_workspace_id(ctx: ToolContext) -> str | None:
    principal = ctx.principal
    if principal is not None:
        value = _strip(principal.tenant_id)
        if value:
            return value
    sources = _tool_context_sources(ctx)
    raw = _first_metadata_value(
        sources,
        [
            "workspace_id",
            "profile_id",
            "active_profile_id",
            "session.workspace_id",
            "session.profile_id",
            "session.active_profile_id",
            "bootstrap.profile_id",
            "bootstrap.workspace_id",
        ],
    )
    return _strip(str(raw)) if raw not in (None, "", [], {}, ()) else None


def _tool_context_has_permission(ctx: ToolContext, permissions: set[str]) -> bool:
    if not permissions:
        return True
    if _tool_context_is_workspace_owner(ctx):
        return True
    role = _tool_context_role(ctx)
    if role in _TOOL_PERMISSIONS_OWNER_ROLES:
        return True
    active_permissions = _tool_context_permissions(ctx)
    return bool(active_permissions.intersection(permissions))


def _coerce_workspace_candidates(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        normalized = _strip(value)
        return [normalized] if normalized else []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            normalized = _strip(str(item))
            if normalized and normalized not in out:
                out.append(normalized)
        return out
    return []


def _collect_workspace_ids(value: Any, *, target_fields: set[str]) -> list[str]:
    values: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_name = str(key).lower()
            if key_name in target_fields:
                values.extend(_coerce_workspace_candidates(item))
            else:
                values.extend(_collect_workspace_ids(item, target_fields=target_fields))
    elif isinstance(value, list):
        for item in value:
            values.extend(_collect_workspace_ids(item, target_fields=target_fields))
    return values


def _tool_context_workspace_id_match(
    remote_tool_name: str,
    arguments: Mapping[str, Any],
    ctx: ToolContext,
) -> tuple[bool, dict[str, Any]]:
    workspace_field = _WORKSPACE_SCOPED_TOOL_WORKSPACE_FIELDS.get(remote_tool_name)
    if workspace_field is None:
        return True, {}

    target_workspace_id = _tool_context_workspace_id(ctx)
    if target_workspace_id is None:
        return True, {}

    target_fields = {workspace_field, _to_camel(workspace_field), _to_camel(workspace_field).replace("_", "")}
    request_workspace_values = _collect_workspace_ids(arguments, target_fields=target_fields)

    if request_workspace_values:
        mismatches = [value for value in request_workspace_values if value != target_workspace_id]
        if mismatches:
            return (
                False,
                {
                    "tool": remote_tool_name,
                    "expected_workspace_id": target_workspace_id,
                    "requested_workspace_ids": sorted(set(request_workspace_values)),
                },
            )
        return True, {}

    if workspace_field in ("profile_id", "workspaceId", "workspace_id"):
        workspace_inject = target_workspace_id if target_workspace_id != "" else None
        if workspace_inject:
            return True, {workspace_field: workspace_inject}

    return True, {}


def _assert_tool_access(remote_tool_name: str, arguments: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
    required_permissions = _TOOL_PERMISSION_REQUIREMENTS.get(remote_tool_name)
    if required_permissions:
        if not _tool_context_has_permission(ctx=ctx, permissions=required_permissions):
            if remote_tool_name == "get_usage_and_limits":
                raise RuntimeError(
                    f"Tool '{remote_tool_name}' requires workspace ownership or explicit workspace-level permission."
                )
            if "view_support_access_audit" in required_permissions:
                raise RuntimeError(
                    f"Tool '{remote_tool_name}' requires the '{'view_support_access_audit'}' permission to inspect security/audit data."
                )
            raise RuntimeError(f"Tool '{remote_tool_name}' requires audit access permission to execute.")

    allowed, injected = _tool_context_workspace_id_match(
        remote_tool_name=remote_tool_name,
        arguments=arguments,
        ctx=ctx,
    )
    if not allowed:
        raise RuntimeError(
            "Tool '{tool}' cannot access requested workspace data. "
            "Expected workspace '{expected}', got: {requested}.".format(
                tool=remote_tool_name,
                expected=injected.get("expected_workspace_id", "<unknown>"),
                requested=",".join(injected.get("requested_workspace_ids", [])),
            )
        )
    return injected


def _tool_context_is_workspace_owner(ctx: ToolContext) -> bool:
    principal = ctx.principal
    if principal is None:
        return False
    sources = _tool_context_sources(ctx)
    owner_id = _first_metadata_value(
        sources,
        [
            "owner_id",
            "session.owner_id",
            "bootstrap.session.owner_id",
            "workspace.owner_id",
        ],
    )
    owner = _strip(str(owner_id)) if owner_id not in (None, "") else None
    return bool(owner and _strip(principal.user_id) == owner)


def _tool_context_allows_inventory_multi_scope(ctx: ToolContext) -> bool:
    if _tool_context_is_workspace_owner(ctx):
        return True
    role = _tool_context_role(ctx)
    if role in _INVENTORY_MULTI_SCOPE_ADMIN_ROLES:
        return True
    permissions = _tool_context_permissions(ctx)
    return bool(permissions.intersection(_INVENTORY_MULTI_SCOPE_ADMIN_PERMISSIONS))


def _tool_context_terminal_scope_required(ctx: ToolContext) -> bool:
    sources = _tool_context_sources(ctx)
    scope_mode = _strip(
        str(
            _first_metadata_value(
                sources,
                [
                    "scope_mode",
                    "scope.scope_mode",
                    "bootstrap.scope.scope_mode",
                ],
            )
            or ""
        ).lower()
    )
    role = _strip(
        str(
            _first_metadata_value(
                sources,
                [
                    "role",
                    "session.role",
                    "bootstrap.session.role",
                ],
            )
            or ""
        ).lower()
    )
    device_mode = _strip(
        str(
            _first_metadata_value(
                sources,
                [
                    "device_mode",
                    "session.device_mode",
                    "bootstrap.session.device_mode",
                ],
            )
            or ""
        ).lower()
    )
    if scope_mode == "terminal_location":
        return True
    if role == "cashier":
        return True
    return device_mode == "terminal"


def _normalize_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        normalized = _strip(value)
        return [normalized] if normalized else []
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = _strip(str(item))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _inventory_request_widens_scope(arguments: Mapping[str, Any]) -> bool:
    structural_scope_ids = _normalize_text_list(arguments.get("structural_location_ids"))
    scope_mode = _strip(str(arguments.get("scope") or "")).lower() if arguments.get("scope") is not None else None
    return bool(structural_scope_ids) or scope_mode in {"all", "all_locations"}


def _collect_scalar_ids(value: Any) -> list[tuple[str, str]]:
    collected: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_name = str(key)
            if key_name.endswith("_id"):
                normalized = _strip(str(item))
                if normalized:
                    collected.append((key_name, normalized))
                    continue
            if key_name.endswith("_ids"):
                for normalized in _normalize_text_list(item):
                    collected.append((key_name, normalized))
                continue
            collected.extend(_collect_scalar_ids(item))
        return collected
    if isinstance(value, list):
        for item in value:
            collected.extend(_collect_scalar_ids(item))
    return collected


def _inventory_sync_metadata(remote_tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    compact_arguments = _compact_tool_arguments(arguments)
    scalar_ids = _collect_scalar_ids(compact_arguments)
    structural_ids = sorted(
        {
            value
            for key, value in scalar_ids
            if key in {"structural_location_id", "structural_location_ids"}
        }
    )
    leaf_ids = sorted(
        {
            value
            for key, value in scalar_ids
            if key in {"stock_location_id", "stock_location_ids"}
        }
    )
    entity_ids = sorted(
        {
            f"{key}:{value}"
            for key, value in scalar_ids
            if key
            not in {
                "structural_location_id",
                "structural_location_ids",
                "stock_location_id",
                "stock_location_ids",
                "profile_id",
            }
        }
    )
    digest_payload = json.dumps(compact_arguments, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(f"{remote_tool_name}:{digest_payload}".encode("utf-8")).hexdigest()[:16]
    key_parts = [f"op={remote_tool_name}"]
    if structural_ids:
        key_parts.append("struct=" + ",".join(structural_ids))
    if leaf_ids:
        key_parts.append("leaf=" + ",".join(leaf_ids))
    if entity_ids:
        key_parts.append("entities=" + ",".join(entity_ids[:8]))
    key_parts.append(f"digest={digest}")
    return {
        "sync_key": "|".join(key_parts),
        "structural_location_ids": structural_ids,
        "stock_location_ids": leaf_ids,
        "entity_ids": entity_ids,
    }


def _location_scoped_sync_metadata(remote_tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    compact_arguments = _compact_tool_arguments(arguments)
    scalar_ids = _collect_scalar_ids(compact_arguments)
    structural_ids = sorted(
        {
            value
            for key, value in scalar_ids
            if key in {"structural_location_id", "structural_location_ids"}
        }
    )
    leaf_ids = sorted(
        {
            value
            for key, value in scalar_ids
            if key in {"stock_location_id", "stock_location_ids", "location_id", "location_ids"}
        }
    )
    entity_ids = sorted(
        {
            f"{key}:{value}"
            for key, value in scalar_ids
            if key
            not in {
                "structural_location_id",
                "structural_location_ids",
                "stock_location_id",
                "stock_location_ids",
                "location_id",
                "location_ids",
                "profile_id",
            }
        }
    )
    digest_payload = json.dumps(compact_arguments, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(f"{remote_tool_name}:{digest_payload}".encode("utf-8")).hexdigest()[:16]
    key_parts = [f"op={remote_tool_name}"]
    if structural_ids:
        key_parts.append("struct=" + ",".join(structural_ids))
    if leaf_ids:
        key_parts.append("leaf=" + ",".join(leaf_ids))
    if entity_ids:
        key_parts.append("entities=" + ",".join(entity_ids[:8]))
    key_parts.append(f"digest={digest}")
    return {
        "sync_key": "|".join(key_parts),
        "structural_location_ids": structural_ids,
        "stock_location_ids": leaf_ids,
        "entity_ids": entity_ids,
    }


def _with_inventory_structural_scope(
    *,
    remote_tool_name: str,
    arguments: dict[str, Any],
    ctx: ToolContext,
) -> dict[str, Any]:
    scoped_arguments = deepcopy(arguments or {})
    scope_id = _tool_context_structural_location_id(ctx)
    scope_required = _tool_context_terminal_scope_required(ctx)
    widened_scope = _inventory_request_widens_scope(scoped_arguments)

    if remote_tool_name in _INVENTORY_SCOPE_REQUIRED_READ_TOOLS:
        if widened_scope:
            if not _tool_context_allows_inventory_multi_scope(ctx):
                raise RuntimeError(
                    f"Inventory tool '{remote_tool_name}' can only widen structural scope for administrative contexts."
                )
            return scoped_arguments
        if _strip(str(scoped_arguments.get("structural_location_id") or "")):
            return scoped_arguments
        if scope_id:
            scoped_arguments["structural_location_id"] = scope_id
            return scoped_arguments
        if scope_required:
            raise RuntimeError(
                f"Inventory tool '{remote_tool_name}' requires a structural location scope for terminal or cashier execution."
            )
        return scoped_arguments

    if remote_tool_name in _INVENTORY_SCOPE_REQUIRED_WRITE_TOOLS:
        payload = scoped_arguments.get("payload")
        if not isinstance(payload, dict):
            payload = {}
            scoped_arguments["payload"] = payload
        payload_scope_id = _strip(str(payload.get("structural_location_id") or ""))
        effective_scope_id = payload_scope_id or scope_id
        if effective_scope_id:
            payload["structural_location_id"] = effective_scope_id
        elif scope_required:
            raise RuntimeError(
                f"Inventory tool '{remote_tool_name}' requires a structural location scope for terminal or cashier execution."
            )

        if remote_tool_name == "adjust_inventory_item_stock":
            adjustments = payload.get("adjustments")
            if isinstance(adjustments, list):
                for item in adjustments:
                    if isinstance(item, dict) and effective_scope_id and not _strip(str(item.get("structural_location_id") or "")):
                        item["structural_location_id"] = effective_scope_id
        if remote_tool_name == "transfer_location_stock":
            transfers = payload.get("transfers")
            if isinstance(transfers, list):
                for item in transfers:
                    if isinstance(item, dict) and effective_scope_id and not _strip(str(item.get("structural_location_id") or "")):
                        item["structural_location_id"] = effective_scope_id
        return scoped_arguments

    return scoped_arguments


def _with_location_scoped_order_structural_scope(
    *,
    remote_tool_name: str,
    arguments: dict[str, Any],
    ctx: ToolContext,
) -> dict[str, Any]:
    if remote_tool_name not in _LOCATION_SCOPED_ORDER_WRITE_TOOLS:
        return deepcopy(arguments or {})

    scoped_arguments = deepcopy(arguments or {})
    payload = scoped_arguments.get("payload")
    if not isinstance(payload, dict):
        payload = {}
        scoped_arguments["payload"] = payload

    scope_id = _tool_context_structural_location_id(ctx)
    payload_scope_id = _strip(str(payload.get("structural_location_id") or ""))
    effective_scope_id = payload_scope_id or scope_id
    if effective_scope_id:
        payload["structural_location_id"] = effective_scope_id
    elif _tool_context_terminal_scope_required(ctx):
        raise RuntimeError(
            f"Order tool '{remote_tool_name}' requires a structural location scope for terminal or cashier execution."
        )
    return scoped_arguments


async def _list_remote_tools(*, server_url: str, headers: Mapping[str, str], timeout_s: float) -> list[_RemoteToolSpec]:
    result = await _run_mcp_session(
        server_url=server_url,
        headers=headers,
        timeout_s=timeout_s,
        operation="list_tools",
        callback=lambda session: session.list_tools(),
    )
    return _parse_remote_tool_specs(result)


async def _call_remote_tool(
    *,
    server_url: str,
    headers: Mapping[str, str],
    timeout_s: float,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    compact_arguments = _compact_tool_arguments(arguments or {})
    argument_keys = sorted(str(key) for key in compact_arguments.keys() if str(key).strip())
    result = await _run_mcp_session(
        server_url=server_url,
        headers=headers,
        timeout_s=timeout_s,
        operation="call_tool",
        callback=lambda session: session.call_tool(name, arguments=compact_arguments),
        remote_tool=name,
        argument_keys=argument_keys,
    )
    error_text = _error_text_from_mcp_result(result)
    if error_text:
        raise RuntimeError(error_text)
    return _dump_result(result)


def _headers_cache_key(headers: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(k).lower(), str(v)) for k, v in headers.items()))


class McpHttpToolExecutor(ToolExecutor):
    """
    Legacy single-endpoint MCP ToolExecutor.

    This keeps the existing env/JWT-driven behavior where `ctx.mcp.server_url` and
    `ctx.mcp.token` can override the default endpoint for the current request.
    """

    def __init__(self, *, config: McpHttpToolExecutorConfig | None = None) -> None:
        self._cfg = config or McpHttpToolExecutorConfig.from_env()
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, list[ToolSpec]]] = {}

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "McpHttpToolExecutor":
        return cls(config=McpHttpToolExecutorConfig.from_env(env))

    def _resolve_server_url(self, *, ctx: ToolContext) -> str | None:
        return _strip((ctx.mcp.server_url if ctx.mcp else None) or self._cfg.server_url)

    def _resolve_token(self, *, ctx: ToolContext) -> str | None:
        return _strip((ctx.mcp.token if ctx.mcp else None) or self._cfg.token)

    def _resolve_headers(self, *, ctx: ToolContext) -> dict[str, str]:
        token = self._resolve_token(ctx=ctx)
        headers: dict[str, str] = {}
        if token:
            headers["authorization"] = f"Bearer {token}"
        return headers

    async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
        server_url = self._resolve_server_url(ctx=ctx)
        if not server_url:
            return []

        headers = self._resolve_headers(ctx=ctx)
        cache_key = (server_url, _headers_cache_key(headers))
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached is not None:
            ts, tools = cached
            if self._cfg.tools_cache_s <= 0 or (now - ts) < self._cfg.tools_cache_s:
                return list(tools)

        remote_tools = await _list_remote_tools(server_url=server_url, headers=headers, timeout_s=self._cfg.timeout_s)
        tools = [ToolSpec(name=item.name, description=item.description, input_schema=item.input_schema) for item in remote_tools]
        self._cache[cache_key] = (now, list(tools))
        return tools

    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        server_url = self._resolve_server_url(ctx=ctx)
        if not server_url:
            raise RuntimeError("MCP server URL is not configured (set KA2A_MCP_SERVER_URL or ka2a.mcp.serverUrl).")
        headers = self._resolve_headers(ctx=ctx)
        return await _call_remote_tool(
            server_url=server_url,
            headers=headers,
            timeout_s=self._cfg.timeout_s,
            name=name,
            arguments=arguments or {},
        )


class _ConfiguredMcpServerExecutor(ToolExecutor):
    def __init__(self, *, config: McpServerConfig, timeout_s: float, tools_cache_s: float, agent_name: str | None = None) -> None:
        self._cfg = config
        self._timeout_s = float(timeout_s)
        self._tools_cache_s = float(tools_cache_s)
        self._agent_name = _short_agent_name(agent_name)
        self._cache: dict[tuple[Any, ...], tuple[float, list[_ConfiguredToolSpec]]] = {}

    def debug_metadata(self) -> dict[str, Any]:
        return {
            "executor_label": f"mcp:{self._cfg.id}",
            "executor_type": self.__class__.__name__,
            "agent_name": self._agent_name,
            "server_id": self._cfg.id,
            "server_url": self._cfg.server_url,
            "auth_mode": self._cfg.auth.mode,
            "tool_name_prefix": self._cfg.tool_name_prefix,
            "allowed_tools": list(self._cfg.tools or []),
            "runtime_connection_count": len(self._cfg.runtime_connections or []),
        }

    def _runtime_connections(self) -> list[McpRuntimeConnectionConfig]:
        return [
            item
            for item in (self._cfg.runtime_connections or [])
            if (item.status or "").strip().lower() == "connected"
        ]

    def _select_runtime_connection(self, *, ctx: ToolContext) -> McpRuntimeConnectionConfig | None:
        connections = self._runtime_connections()
        if not connections:
            return None

        principal_user_id = _strip(ctx.principal.user_id if ctx.principal else None)
        if principal_user_id:
            for item in connections:
                if (item.connection_scope or "").strip().lower() != "user":
                    continue
                if _strip(item.owner_user_id) == principal_user_id:
                    return item

        for item in connections:
            if (item.connection_scope or "").strip().lower() == "workspace":
                return item
        return None

    def _uses_runtime_connection_auth(self) -> bool:
        return self._cfg.auth.mode in {
            "api_key_header",
            "oauth_user",
            "oauth_workspace",
            "service_account",
            "custom",
        }

    def _resolve_request_target(
        self,
        *,
        ctx: ToolContext,
    ) -> tuple[str, dict[str, str], McpRuntimeConnectionConfig | None]:
        headers = dict(self._cfg.headers or {})
        connection = self._select_runtime_connection(ctx=ctx)

        if self._cfg.auth.mode == "http_message_signature":
            raise RuntimeError(
                f"MCP server '{self._cfg.id}' requires HTTP message signing, which is not implemented yet."
            )

        if self._uses_runtime_connection_auth():
            if connection is None:
                raise RuntimeError(
                    f"MCP server '{self._cfg.id}' requires a connected workspace or user tool connection, but none is available."
                )
            if connection.headers:
                headers.update(connection.headers)
        else:
            token = self._resolve_auth_token(ctx=ctx)
            if token is not None:
                header_name = self._cfg.auth.header_name or "authorization"
                scheme = self._cfg.auth.scheme or ""
                header_value = f"{scheme} {token}".strip() if scheme else token
                headers[header_name] = header_value

        server_url = _strip(connection.server_url_override if connection is not None else None) or self._cfg.server_url
        return server_url, headers, connection

    def _timeout_seconds(self) -> float:
        if self._cfg.timeout_s is not None:
            return float(self._cfg.timeout_s)
        return self._timeout_s

    def _resolve_auth_token(self, *, ctx: ToolContext) -> str | None:
        auth = self._cfg.auth
        if auth.mode == "none":
            return None
        if auth.mode == "static":
            token = auth.token
            if not token and auth.token_env:
                token = _strip(os.environ.get(auth.token_env))
            if not token:
                raise RuntimeError(f"MCP server '{self._cfg.id}' requires a static token, but none is configured.")
            return token
        if auth.mode == "context":
            token = _strip(ctx.mcp.token if ctx.mcp else None)
            if not token:
                raise RuntimeError(
                    f"MCP server '{self._cfg.id}' requires request-scoped MCP credentials, but ctx.mcp.token is missing."
                )
            return token
        if auth.mode == "forward_bearer":
            token = _strip(ctx.principal.bearer_token if ctx.principal else None)
            if not token:
                raise RuntimeError(
                    f"MCP server '{self._cfg.id}' requires a forwarded bearer token, but none is present in the request."
                )
            return token
        raise RuntimeError(f"Unsupported MCP auth mode for server '{self._cfg.id}': {auth.mode}")

    def _filter_tools(self, remote_tools: list[_RemoteToolSpec]) -> list[_ConfiguredToolSpec]:
        prefix = self._cfg.tool_name_prefix or ""
        allowed = set(self._cfg.tools or [])
        out: list[_ConfiguredToolSpec] = []
        for item in remote_tools:
            exposed_name = f"{prefix}{item.name}"
            if allowed and item.name not in allowed and exposed_name not in allowed:
                continue
            out.append(
                _ConfiguredToolSpec(
                    exposed_name=exposed_name,
                    remote_name=item.name,
                    description=item.description,
                    input_schema=item.input_schema,
                )
            )
        return out

    async def _resolved_tools(self, *, ctx: ToolContext) -> list[_ConfiguredToolSpec]:
        server_url, headers, connection = self._resolve_request_target(ctx=ctx)
        cache_key = (server_url, *_headers_cache_key(headers))
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached is not None:
            ts, tools = cached
            if self._tools_cache_s <= 0 or (now - ts) < self._tools_cache_s:
                _log_mcp_operation(
                    "info",
                    "list_tools_cache_hit",
                    agent=self._agent_name,
                    server=self._cfg.id,
                    server_url=server_url,
                    cached_tools=len(tools),
                    connection_id=connection.id if connection is not None else None,
                )
                return list(tools)

        _log_mcp_operation(
            "info",
            "list_tools_start",
            agent=self._agent_name,
            server=self._cfg.id,
            server_url=server_url,
            auth_mode=self._cfg.auth.mode,
            bearer_present=bool(ctx.principal and ctx.principal.bearer_token),
            allowed_tools=len(self._cfg.tools or []),
            connection_id=connection.id if connection is not None else None,
            connection_scope=connection.connection_scope if connection is not None else None,
        )
        try:
            remote_tools = await _list_remote_tools(
                server_url=server_url,
                headers=headers,
                timeout_s=self._timeout_seconds(),
            )
        except Exception as exc:
            _log_mcp_operation(
                "warning",
                "list_tools_failed",
                agent=self._agent_name,
                server=self._cfg.id,
                server_url=server_url,
                auth_mode=self._cfg.auth.mode,
                connection_id=connection.id if connection is not None else None,
                error=str(exc),
            )
            raise RuntimeError(
                f"MCP list_tools failed for agent '{self._agent_name}' on server '{self._cfg.id}' "
                f"({server_url}): {exc}"
            ) from exc
        tools = self._filter_tools(remote_tools)
        _log_mcp_operation(
            "info",
            "list_tools_success",
            agent=self._agent_name,
            server=self._cfg.id,
            server_url=server_url,
            remote_tools=len(remote_tools),
            exposed_tools=len(tools),
            connection_id=connection.id if connection is not None else None,
        )
        self._cache[cache_key] = (now, list(tools))
        return tools

    async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
        return [item.to_tool_spec() for item in await self._resolved_tools(ctx=ctx)]

    def _configured_route_for_name(self, name: str) -> _ConfiguredToolSpec | None:
        allowed = set(self._cfg.tools or [])
        if not allowed:
            return None
        prefix = self._cfg.tool_name_prefix or ""
        requested = str(name or "").strip()
        remote_name = requested[len(prefix) :] if prefix and requested.startswith(prefix) else requested
        exposed_name = f"{prefix}{remote_name}"
        if requested not in allowed and remote_name not in allowed and exposed_name not in allowed:
            return None
        return _ConfiguredToolSpec(
            exposed_name=exposed_name,
            remote_name=remote_name,
            description="Configured MCP tool",
            input_schema={},
        )

    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        route = self._configured_route_for_name(name)
        if route is None:
            tools = await self._resolved_tools(ctx=ctx)
            route = next((item for item in tools if item.exposed_name == name), None)
        if route is None:
            raise RuntimeError(f"Tool '{name}' is not available from MCP server '{self._cfg.id}'.")

        workspace_injected_arguments = _assert_tool_access(
            remote_tool_name=route.remote_name,
            arguments=arguments or {},
            ctx=ctx,
        )
        server_url, headers, connection = self._resolve_request_target(ctx=ctx)
        scoped_arguments = _with_inventory_structural_scope(
            remote_tool_name=route.remote_name,
            arguments={**(arguments or {}), **workspace_injected_arguments},
            ctx=ctx,
        )
        scoped_arguments = _with_location_scoped_order_structural_scope(
            remote_tool_name=route.remote_name,
            arguments=scoped_arguments,
            ctx=ctx,
        )
        compact_arguments = _compact_tool_arguments(scoped_arguments)
        argument_keys = sorted(str(key) for key in compact_arguments.keys() if str(key).strip())
        sync_metadata = (
            _location_scoped_sync_metadata(route.remote_name, compact_arguments)
            if route.remote_name
            in (
                _INVENTORY_SCOPE_REQUIRED_READ_TOOLS
                | _INVENTORY_SCOPE_REQUIRED_WRITE_TOOLS
                | _LOCATION_SCOPED_ORDER_WRITE_TOOLS
            )
            else None
        )
        _log_mcp_operation(
            "info",
            "call_tool_start",
            agent=self._agent_name,
            server=self._cfg.id,
            server_url=server_url,
            auth_mode=self._cfg.auth.mode,
            exposed_tool=name,
            remote_tool=route.remote_name,
            argument_keys=argument_keys,
            connection_id=connection.id if connection is not None else None,
            connection_scope=connection.connection_scope if connection is not None else None,
            sync_key=sync_metadata.get("sync_key") if sync_metadata else None,
            structural_location_ids=sync_metadata.get("structural_location_ids") if sync_metadata else None,
            stock_location_ids=sync_metadata.get("stock_location_ids") if sync_metadata else None,
            entity_ids=sync_metadata.get("entity_ids")[:8] if sync_metadata else None,
        )
        try:
            result = await _call_remote_tool(
                server_url=server_url,
                headers=headers,
                timeout_s=self._timeout_seconds(),
                name=route.remote_name,
                arguments=compact_arguments,
            )
        except Exception as exc:
            _log_mcp_operation(
                "warning",
                "call_tool_failed",
                agent=self._agent_name,
                server=self._cfg.id,
                server_url=server_url,
                exposed_tool=name,
                remote_tool=route.remote_name,
                argument_keys=argument_keys,
                connection_id=connection.id if connection is not None else None,
                error=str(exc),
                sync_key=sync_metadata.get("sync_key") if sync_metadata else None,
            )
            raise RuntimeError(
                f"MCP call_tool failed for agent '{self._agent_name}' on server '{self._cfg.id}' "
                f"({server_url}) for exposed tool '{name}' -> remote tool '{route.remote_name}': {exc}"
            ) from exc
        _log_mcp_operation(
            "info",
            "call_tool_success",
            agent=self._agent_name,
            server=self._cfg.id,
            server_url=server_url,
            exposed_tool=name,
            remote_tool=route.remote_name,
            result_type=type(result).__name__,
            connection_id=connection.id if connection is not None else None,
            sync_key=sync_metadata.get("sync_key") if sync_metadata else None,
            structural_location_ids=sync_metadata.get("structural_location_ids") if sync_metadata else None,
            stock_location_ids=sync_metadata.get("stock_location_ids") if sync_metadata else None,
        )
        return result


class CompositeToolExecutor(ToolExecutor):
    def __init__(self, *, executors: list[ToolExecutor] | None = None, skip_unavailable: bool = False) -> None:
        self._executors = [executor for executor in (executors or []) if executor is not None]
        self._skip_unavailable = bool(skip_unavailable)
        self._last_list_tool_failures: list[dict[str, Any]] = []

    @staticmethod
    def _executor_debug_metadata(executor: ToolExecutor) -> dict[str, Any]:
        metadata_getter = getattr(executor, "debug_metadata", None)
        if callable(metadata_getter):
            try:
                metadata = metadata_getter()
            except Exception:
                metadata = None
            if isinstance(metadata, dict):
                return {
                    "executor_label": str(metadata.get("executor_label") or executor.__class__.__name__),
                    "executor_type": str(metadata.get("executor_type") or executor.__class__.__name__),
                    **metadata,
                }
        return {
            "executor_label": executor.__class__.__name__,
            "executor_type": executor.__class__.__name__,
        }

    def list_tool_failures(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._last_list_tool_failures]

    def _direct_executor_for_tool_name(self, name: str) -> ToolExecutor | None:
        namespace, separator, _ = str(name or "").partition(".")
        if not namespace or not separator:
            return None
        expected_prefix = f"{namespace}."
        for executor in self._executors:
            metadata = self._executor_debug_metadata(executor)
            server_id = str(metadata.get("server_id") or "").strip()
            tool_name_prefix = str(metadata.get("tool_name_prefix") or "").strip()
            if server_id == namespace or tool_name_prefix == expected_prefix:
                return executor
        return None

    async def _resolve_routes(self, *, ctx: ToolContext) -> tuple[list[ToolSpec], dict[str, ToolExecutor]]:
        tools: list[ToolSpec] = []
        routes: dict[str, ToolExecutor] = {}
        failures: list[dict[str, Any]] = []
        _log_mcp_operation(
            "info",
            "resolve_routes_start",
            executor_count=len(self._executors),
            skip_unavailable=self._skip_unavailable,
            bearer_present=bool(ctx.principal and ctx.principal.bearer_token),
            mcp_ctx_server=getattr(ctx.mcp, "server_url", None) if ctx.mcp else None,
        )
        for executor in self._executors:
            try:
                current = await executor.list_tools(ctx=ctx)
            except Exception as exc:
                if not self._skip_unavailable:
                    raise
                failure = {
                    **self._executor_debug_metadata(executor),
                    "error": str(exc),
                }
                failures.append(failure)
                executor_label = str(failure.get("executor_label") or executor.__class__.__name__)
                agent_name = str(failure.get("agent_name") or "").strip()
                server_url = str(failure.get("server_url") or "").strip()
                auth_mode = str(failure.get("auth_mode") or "").strip()
                agent_suffix = f" on agent {agent_name}" if agent_name else ""
                server_suffix = f" ({server_url})" if server_url else ""
                auth_suffix = f" auth={auth_mode}" if auth_mode else ""
                logger.warning(
                    "tool executor failed during list_tools; skipping executor %s%s%s%s: %s",
                    executor_label,
                    agent_suffix,
                    server_suffix,
                    auth_suffix,
                    str(exc),
                    extra=failure,
                    exc_info=True,
                )
                continue
            for item in current:
                if item.name in routes:
                    raise RuntimeError(f"Duplicate tool name exposed by multiple executors: {item.name}")
                routes[item.name] = executor
                tools.append(item)
        self._last_list_tool_failures = failures
        _log_mcp_operation(
            "info",
            "resolve_routes_complete",
            exposed_tools=len(tools),
            failure_count=len(failures),
            failed_executors=[item.get("executor_label") for item in failures],
        )
        return tools, routes

    async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
        tools, _ = await self._resolve_routes(ctx=ctx)
        return tools

    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        direct_executor = self._direct_executor_for_tool_name(name)
        if direct_executor is not None:
            _log_mcp_operation(
                "info",
                "composite_call_tool_direct_route",
                exposed_tool=name,
                executor=self._executor_debug_metadata(direct_executor).get("executor_label"),
                argument_keys=sorted(str(key) for key in (arguments or {}).keys()),
            )
            try:
                return await direct_executor.call_tool(name=name, arguments=arguments or {}, ctx=ctx)
            except Exception as exc:
                _log_mcp_operation(
                    "warning",
                    "composite_call_tool_direct_route_failed",
                    exposed_tool=name,
                    executor=self._executor_debug_metadata(direct_executor).get("executor_label"),
                    error=str(exc),
                )

        _, routes = await self._resolve_routes(ctx=ctx)
        executor = routes.get(name)
        if executor is None:
            raise RuntimeError(f"Unknown tool: {name}")
        _log_mcp_operation(
            "info",
            "composite_call_tool_route",
            exposed_tool=name,
            executor=self._executor_debug_metadata(executor).get("executor_label"),
            argument_keys=sorted(str(key) for key in (arguments or {}).keys()),
        )
        return await executor.call_tool(name=name, arguments=arguments or {}, ctx=ctx)


class MultiMcpToolExecutor(ToolExecutor):
    """
    Composite MCP executor that supports:
    - multiple MCP servers loaded from a JSON config file
    - optional legacy single-server env configuration
    - optional extra/local tool executors via `KA2A_TOOL_EXECUTOR`
    """

    def __init__(
        self,
        *,
        config: MultiMcpToolExecutorConfig | None = None,
        legacy_executor: ToolExecutor | None = None,
        extra_executor: ToolExecutor | None = None,
    ) -> None:
        self._cfg = config or MultiMcpToolExecutorConfig()
        executors: list[ToolExecutor] = []
        if legacy_executor is not None:
            executors.append(legacy_executor)
        for server in self._cfg.servers:
            if not server.enabled:
                continue
            executors.append(
                _ConfiguredMcpServerExecutor(
                    config=server,
                    timeout_s=self._cfg.timeout_s,
                    tools_cache_s=self._cfg.tools_cache_s,
                    agent_name=self._cfg.agent_name,
                )
            )
        if extra_executor is not None:
            executors.append(extra_executor)
        self._composite = CompositeToolExecutor(executors=executors, skip_unavailable=True)
        _log_mcp_operation(
            "info",
            "executor_initialized",
            agent=_short_agent_name(self._cfg.agent_name),
            config_path=self._cfg.config_path,
            server_count=len(self._cfg.servers),
            servers=[f"{server.id}:{server.server_url}" for server in self._cfg.servers],
            timeout_s=self._cfg.timeout_s,
            tools_cache_s=self._cfg.tools_cache_s,
            legacy_executor=bool(legacy_executor),
            extra_executor=bool(extra_executor),
        )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        agent_name: str | None = None,
    ) -> "MultiMcpToolExecutor":
        env_map = env or os.environ
        config = MultiMcpToolExecutorConfig.from_env(env_map, agent_name=agent_name)
        extra_executor = _load_tool_executor(env_map.get("KA2A_TOOL_EXECUTOR"))
        legacy_executor: ToolExecutor | None = None
        if config.config_path is None:
            legacy_executor = McpHttpToolExecutor(config=McpHttpToolExecutorConfig.from_env(env_map))
        return cls(config=config, legacy_executor=legacy_executor, extra_executor=extra_executor)

    async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
        return await self._composite.list_tools(ctx=ctx)

    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        return await self._composite.call_tool(name=name, arguments=arguments or {}, ctx=ctx)

    def list_tool_failures(self) -> list[dict[str, Any]]:
        return self._composite.list_tool_failures()
