from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

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


@dataclass(slots=True)
class McpHttpToolExecutorConfig:
    server_url: str | None = None
    token: str | None = None
    timeout_s: float = 30.0
    tools_cache_s: float = 60.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "McpHttpToolExecutorConfig":
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


class McpHttpToolExecutor(ToolExecutor):
    """
    MCP ToolExecutor backed by the official `mcp` Python SDK (HTTP transport).

    This expects an MCP server that supports Streamable HTTP.
    """

    def __init__(self, *, config: McpHttpToolExecutorConfig | None = None) -> None:
        self._cfg = config or McpHttpToolExecutorConfig.from_env()
        self._cache: dict[tuple[str, str | None], tuple[float, list[ToolSpec]]] = {}

    @classmethod
    def from_env(cls) -> "McpHttpToolExecutor":
        return cls(config=McpHttpToolExecutorConfig.from_env())

    def _resolve_server_url(self, *, ctx: ToolContext) -> str | None:
        return _strip((ctx.mcp.server_url if ctx.mcp else None) or self._cfg.server_url)

    def _resolve_token(self, *, ctx: ToolContext) -> str | None:
        return _strip((ctx.mcp.token if ctx.mcp else None) or self._cfg.token)

    async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
        server_url = self._resolve_server_url(ctx=ctx)
        if not server_url:
            return []
        token = self._resolve_token(ctx=ctx)
        cache_key = (server_url, token)

        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached is not None:
            ts, tools = cached
            if self._cfg.tools_cache_s <= 0 or (now - ts) < self._cfg.tools_cache_s:
                return list(tools)

        httpx, ClientSession, streamable_http_client = _require_mcp()

        headers: dict[str, str] = {}
        if token:
            headers["authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(headers=headers, timeout=float(self._cfg.timeout_s), follow_redirects=True) as client:
            async with streamable_http_client(server_url, http_client=client) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()

        raw_tools = getattr(result, "tools", result)
        out: list[ToolSpec] = []
        if isinstance(raw_tools, list):
            for t in raw_tools:
                name = getattr(t, "name", None) if not isinstance(t, dict) else t.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                desc = getattr(t, "description", None) if not isinstance(t, dict) else t.get("description")
                schema = (
                    (t.get("inputSchema") or t.get("input_schema")) if isinstance(t, dict)
                    else (getattr(t, "inputSchema", None) or getattr(t, "input_schema", None))
                )
                out.append(
                    ToolSpec(
                        name=name.strip(),
                        description=str(desc).strip() if isinstance(desc, str) and desc.strip() else "",
                        input_schema=schema if isinstance(schema, dict) else None,
                    )
                )

        self._cache[cache_key] = (now, list(out))
        return out

    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        server_url = self._resolve_server_url(ctx=ctx)
        if not server_url:
            raise RuntimeError("MCP server URL is not configured (set KA2A_MCP_SERVER_URL or ka2a.mcp.serverUrl).")
        token = self._resolve_token(ctx=ctx)

        httpx, ClientSession, streamable_http_client = _require_mcp()

        headers: dict[str, str] = {}
        if token:
            headers["authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(headers=headers, timeout=float(self._cfg.timeout_s), follow_redirects=True) as client:
            async with streamable_http_client(server_url, http_client=client) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments=arguments or {})

        if hasattr(result, "model_dump"):
            return result.model_dump(by_alias=True, exclude_none=True)  # type: ignore[no-any-return]
        return result
