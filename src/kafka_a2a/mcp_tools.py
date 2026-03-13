from __future__ import annotations

import importlib
import json
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kafka_a2a.tools import ToolContext, ToolExecutor, ToolSpec


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
        if normalized not in {"none", "static", "context", "forward_bearer"}:
            raise ValueError("MCP auth mode must be one of: none, static, context, forward_bearer")
        return normalized

    @field_validator("header_name", "scheme", "token", "token_env")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        return _strip(value)


class McpServerConfig(_McpToolsModel):
    id: str
    server_url: str
    tools: list[str] | None = None
    tool_name_prefix: str | None = None
    headers: dict[str, str] | None = None
    auth: McpServerAuthConfig = Field(default_factory=McpServerAuthConfig)
    enabled: bool = True

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


class McpAgentConfig(_McpToolsModel):
    servers: list[McpServerConfig] = Field(default_factory=list)


class McpAgentConfigFile(_McpToolsModel):
    version: int = 1
    servers: list[McpServerConfig] = Field(default_factory=list)
    agents: dict[str, McpAgentConfig] = Field(default_factory=dict)

    def resolve_agent(self, *, agent_name: str | None) -> McpAgentConfig:
        name = _strip(agent_name)
        if name and name in self.agents:
            return self.agents[name]
        return McpAgentConfig(servers=list(self.servers))


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
    callback: Callable[[Any], Awaitable[Any]],
) -> Any:
    httpx, ClientSession, streamable_http_client = _require_mcp()

    async with httpx.AsyncClient(
        headers=dict(headers),
        timeout=float(timeout_s),
        follow_redirects=True,
    ) as client:
        async with streamable_http_client(server_url, http_client=client) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await callback(session)


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
        return result.model_dump(by_alias=True, exclude_none=True)  # type: ignore[no-any-return]
    return result


async def _list_remote_tools(*, server_url: str, headers: Mapping[str, str], timeout_s: float) -> list[_RemoteToolSpec]:
    result = await _run_mcp_session(
        server_url=server_url,
        headers=headers,
        timeout_s=timeout_s,
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
    result = await _run_mcp_session(
        server_url=server_url,
        headers=headers,
        timeout_s=timeout_s,
        callback=lambda session: session.call_tool(name, arguments=arguments or {}),
    )
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
    def __init__(self, *, config: McpServerConfig, timeout_s: float, tools_cache_s: float) -> None:
        self._cfg = config
        self._timeout_s = float(timeout_s)
        self._tools_cache_s = float(tools_cache_s)
        self._cache: dict[tuple[tuple[str, str], ...], tuple[float, list[_ConfiguredToolSpec]]] = {}

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

    def _resolve_headers(self, *, ctx: ToolContext) -> dict[str, str]:
        headers = dict(self._cfg.headers or {})
        token = self._resolve_auth_token(ctx=ctx)
        if token is None:
            return headers
        header_name = self._cfg.auth.header_name or "authorization"
        scheme = self._cfg.auth.scheme or ""
        header_value = f"{scheme} {token}".strip() if scheme else token
        headers[header_name] = header_value
        return headers

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
        headers = self._resolve_headers(ctx=ctx)
        cache_key = _headers_cache_key(headers)
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached is not None:
            ts, tools = cached
            if self._tools_cache_s <= 0 or (now - ts) < self._tools_cache_s:
                return list(tools)

        remote_tools = await _list_remote_tools(
            server_url=self._cfg.server_url,
            headers=headers,
            timeout_s=self._timeout_s,
        )
        tools = self._filter_tools(remote_tools)
        self._cache[cache_key] = (now, list(tools))
        return tools

    async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
        return [item.to_tool_spec() for item in await self._resolved_tools(ctx=ctx)]

    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        tools = await self._resolved_tools(ctx=ctx)
        route = next((item for item in tools if item.exposed_name == name), None)
        if route is None:
            raise RuntimeError(f"Tool '{name}' is not available from MCP server '{self._cfg.id}'.")

        headers = self._resolve_headers(ctx=ctx)
        return await _call_remote_tool(
            server_url=self._cfg.server_url,
            headers=headers,
            timeout_s=self._timeout_s,
            name=route.remote_name,
            arguments=arguments or {},
        )


class CompositeToolExecutor(ToolExecutor):
    def __init__(self, *, executors: list[ToolExecutor] | None = None) -> None:
        self._executors = [executor for executor in (executors or []) if executor is not None]

    async def _resolve_routes(self, *, ctx: ToolContext) -> tuple[list[ToolSpec], dict[str, ToolExecutor]]:
        tools: list[ToolSpec] = []
        routes: dict[str, ToolExecutor] = {}
        for executor in self._executors:
            current = await executor.list_tools(ctx=ctx)
            for item in current:
                if item.name in routes:
                    raise RuntimeError(f"Duplicate tool name exposed by multiple executors: {item.name}")
                routes[item.name] = executor
                tools.append(item)
        return tools, routes

    async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
        tools, _ = await self._resolve_routes(ctx=ctx)
        return tools

    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        _, routes = await self._resolve_routes(ctx=ctx)
        executor = routes.get(name)
        if executor is None:
            raise RuntimeError(f"Unknown tool: {name}")
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
                )
            )
        if extra_executor is not None:
            executors.append(extra_executor)
        self._composite = CompositeToolExecutor(executors=executors)

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
