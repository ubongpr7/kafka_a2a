from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kafka_a2a.mcp_tools import (
    CompositeToolExecutor,
    McpServerAuthConfig,
    McpServerConfig,
    MultiMcpToolExecutor,
    MultiMcpToolExecutorConfig,
)
from kafka_a2a.tenancy import Principal
from kafka_a2a.tools import ToolContext, ToolExecutor, ToolSpec


class _LocalExecutor(ToolExecutor):
    async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
        _ = ctx
        return [ToolSpec(name="local.transform", description="Local transform")]

    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        _ = ctx
        return {"tool": name, "arguments": arguments, "source": "local"}


def test_multi_mcp_config_resolves_agent_specific_servers(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp-tools.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "sharedServers": [
                    {
                        "id": "shared",
                        "serverUrl": "http://shared-mcp:8000/mcp",
                        "toolNamePrefix": "shared.",
                    }
                ],
                "agents": {
                    "inventory-host": {
                        "servers": [
                            {
                                "id": "inventory",
                                "serverUrl": "http://inventory-mcp:8000/mcp",
                                "toolNamePrefix": "inventory.",
                                "auth": {"mode": "forward_bearer"},
                                "tools": ["reserve_stock", "get_stock"],
                            }
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = MultiMcpToolExecutorConfig.from_env(
        {
            "KA2A_MCP_CONFIG_PATH": str(config_path),
            "KA2A_AGENT_NAME": "inventory-host",
            "KA2A_MCP_TIMEOUT_S": "12",
            "KA2A_MCP_TOOLS_CACHE_S": "90",
        }
    )

    assert cfg.agent_name == "inventory-host"
    assert cfg.timeout_s == 12.0
    assert cfg.tools_cache_s == 90.0
    assert len(cfg.servers) == 1
    assert cfg.servers[0].id == "inventory"
    assert cfg.servers[0].server_url == "http://inventory-mcp:8000/mcp"
    assert cfg.servers[0].tool_name_prefix == "inventory."
    assert cfg.servers[0].tools == ["reserve_stock", "get_stock"]


def test_multi_mcp_config_resolves_multiple_specialists_without_cross_pollution(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp-tools.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "agents": {
                    "product": {
                        "servers": [
                            {
                                "id": "products",
                                "serverUrl": "http://products-mcp:8000/mcp",
                                "toolNamePrefix": "product.",
                                "tools": ["search_products"],
                            }
                        ]
                    },
                    "inventory": {
                        "servers": [
                            {
                                "id": "inventory",
                                "serverUrl": "http://inventory-mcp:8000/mcp",
                                "toolNamePrefix": "inventory.",
                                "tools": ["search_inventories"],
                            }
                        ]
                    },
                    "pos": {
                        "servers": [
                            {
                                "id": "pos",
                                "serverUrl": "http://pos-mcp:8000/mcp",
                                "toolNamePrefix": "pos.",
                                "tools": ["get_current_pos_session"],
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    product_cfg = MultiMcpToolExecutorConfig.from_env(
        {"KA2A_MCP_CONFIG_PATH": str(config_path), "KA2A_AGENT_NAME": "product"}
    )
    inventory_cfg = MultiMcpToolExecutorConfig.from_env(
        {"KA2A_MCP_CONFIG_PATH": str(config_path), "KA2A_AGENT_NAME": "inventory"}
    )
    pos_cfg = MultiMcpToolExecutorConfig.from_env(
        {"KA2A_MCP_CONFIG_PATH": str(config_path), "KA2A_AGENT_NAME": "pos"}
    )

    assert [server.id for server in product_cfg.servers] == ["products"]
    assert [server.id for server in inventory_cfg.servers] == ["inventory"]
    assert [server.id for server in pos_cfg.servers] == ["pos"]


def test_multi_mcp_config_supports_shared_server_references(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp-tools.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "sharedServers": [
                    {
                        "id": "inventory",
                        "serverUrl": "http://inventory-mcp:8000/mcp",
                        "toolNamePrefix": "inventory.",
                        "auth": {"mode": "forward_bearer"},
                    },
                    {
                        "id": "products",
                        "serverUrl": "http://products-mcp:8000/mcp",
                        "toolNamePrefix": "product.",
                        "auth": {"mode": "forward_bearer"},
                    },
                ],
                "agents": {
                    "onboarding": {
                        "servers": [
                            {
                                "ref": "inventory",
                                "tools": ["create_inventory", "create_stock_location"],
                            },
                            {
                                "ref": "products",
                                "tools": ["create_product"],
                            },
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = MultiMcpToolExecutorConfig.from_env(
        {"KA2A_MCP_CONFIG_PATH": str(config_path), "KA2A_AGENT_NAME": "onboarding"}
    )

    assert [server.id for server in cfg.servers] == ["inventory", "products"]
    assert cfg.servers[0].server_url == "http://inventory-mcp:8000/mcp"
    assert cfg.servers[0].tool_name_prefix == "inventory."
    assert cfg.servers[0].tools == ["create_inventory", "create_stock_location"]
    assert cfg.servers[1].server_url == "http://products-mcp:8000/mcp"
    assert cfg.servers[1].tool_name_prefix == "product."
    assert cfg.servers[1].tools == ["create_product"]


def test_multi_mcp_config_does_not_apply_shared_servers_as_host_fallback(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp-tools.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "sharedServers": [
                    {
                        "id": "inventory",
                        "serverUrl": "http://inventory-mcp:8000/mcp",
                        "toolNamePrefix": "inventory.",
                    }
                ],
                "agents": {
                    "inventory": {
                        "servers": [
                            {
                                "ref": "inventory",
                                "tools": ["search_inventories"],
                            }
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = MultiMcpToolExecutorConfig.from_env(
        {"KA2A_MCP_CONFIG_PATH": str(config_path), "KA2A_AGENT_NAME": "host"}
    )

    assert cfg.servers == []


@pytest.mark.asyncio
async def test_multi_mcp_executor_routes_tools_and_forwards_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    from kafka_a2a import mcp_tools

    calls: list[dict[str, Any]] = []
    tools_by_server = {
        "http://products-mcp:8000/mcp": [{"name": "search", "description": "Search products"}],
        "http://inventory-mcp:8000/mcp": [{"name": "reserve_stock", "description": "Reserve inventory"}],
    }

    async def fake_run_mcp_session(
        *,
        server_url: str,
        headers: dict[str, str],
        timeout_s: float,
        operation: str,
        callback: Any,
        remote_tool: str | None = None,
        argument_keys: list[str] | None = None,
    ) -> Any:
        _ = operation, remote_tool, argument_keys
        call: dict[str, Any] = {
            "server_url": server_url,
            "headers": dict(headers),
            "timeout_s": timeout_s,
        }
        calls.append(call)

        class _Session:
            async def list_tools(self) -> Any:
                call["op"] = "list_tools"
                return {"tools": tools_by_server[server_url]}

            async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
                call["op"] = "call_tool"
                call["tool_name"] = name
                call["arguments"] = dict(arguments or {})
                return {"server": server_url, "tool": name, "arguments": dict(arguments or {})}

        return await callback(_Session())

    monkeypatch.setattr(mcp_tools, "_run_mcp_session", fake_run_mcp_session)

    executor = MultiMcpToolExecutor(
        config=MultiMcpToolExecutorConfig(
            timeout_s=15.0,
            tools_cache_s=120.0,
            servers=[
                McpServerConfig(
                    id="products",
                    server_url="http://products-mcp:8000/mcp",
                    tool_name_prefix="product.",
                    auth=McpServerAuthConfig(mode="none"),
                ),
                McpServerConfig(
                    id="inventory",
                    server_url="http://inventory-mcp:8000/mcp",
                    tool_name_prefix="inventory.",
                    auth=McpServerAuthConfig(mode="forward_bearer"),
                    tools=["reserve_stock"],
                ),
            ],
        )
    )

    ctx = ToolContext(
        principal=Principal(
            user_id="user-1",
            tenant_id="profile-1",
            bearer_token="jwt-abc",
            claims={"profile_id": "profile-1", "permissions": ["inventory.manage"]},
        )
    )

    tools = await executor.list_tools(ctx=ctx)
    assert [tool.name for tool in tools] == ["product.search", "inventory.reserve_stock"]

    product_list_call = next(call for call in calls if call["server_url"] == "http://products-mcp:8000/mcp")
    inventory_list_call = next(call for call in calls if call["server_url"] == "http://inventory-mcp:8000/mcp")
    assert product_list_call["headers"] == {}
    assert inventory_list_call["headers"]["authorization"] == "Bearer jwt-abc"

    result = await executor.call_tool(name="inventory.reserve_stock", arguments={"sku": "A-1"}, ctx=ctx)
    assert result == {
        "server": "http://inventory-mcp:8000/mcp",
        "tool": "reserve_stock",
        "arguments": {"sku": "A-1"},
    }

    inventory_call = calls[-1]
    assert inventory_call["op"] == "call_tool"
    assert inventory_call["tool_name"] == "reserve_stock"
    assert inventory_call["headers"]["authorization"] == "Bearer jwt-abc"


@pytest.mark.asyncio
async def test_multi_mcp_executor_composes_local_and_remote_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    from kafka_a2a import mcp_tools

    async def fake_run_mcp_session(
        *,
        server_url: str,
        headers: dict[str, str],
        timeout_s: float,
        operation: str,
        callback: Any,
        remote_tool: str | None = None,
        argument_keys: list[str] | None = None,
    ) -> Any:
        _ = headers, timeout_s, operation, remote_tool, argument_keys

        class _Session:
            async def list_tools(self) -> Any:
                return {"tools": [{"name": "search", "description": "Search products"}]}

            async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
                return {"server": server_url, "tool": name, "arguments": dict(arguments or {})}

        return await callback(_Session())

    monkeypatch.setattr(mcp_tools, "_run_mcp_session", fake_run_mcp_session)

    executor = MultiMcpToolExecutor(
        config=MultiMcpToolExecutorConfig(
            servers=[
                McpServerConfig(
                    id="products",
                    server_url="http://products-mcp:8000/mcp",
                    tool_name_prefix="product.",
                )
            ]
        ),
        extra_executor=_LocalExecutor(),
    )

    tools = await executor.list_tools(ctx=ToolContext())
    assert [tool.name for tool in tools] == ["product.search", "local.transform"]

    local_result = await executor.call_tool(name="local.transform", arguments={"value": "abc"}, ctx=ToolContext())
    assert local_result == {"tool": "local.transform", "arguments": {"value": "abc"}, "source": "local"}

    remote_result = await executor.call_tool(name="product.search", arguments={"query": "milk"}, ctx=ToolContext())
    assert remote_result == {
        "server": "http://products-mcp:8000/mcp",
        "tool": "search",
        "arguments": {"query": "milk"},
    }


@pytest.mark.asyncio
async def test_composite_executor_rejects_duplicate_tool_names() -> None:
    class _ExecA(ToolExecutor):
        async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
            _ = ctx
            return [ToolSpec(name="dup.tool")]

        async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
            _ = name, arguments, ctx
            return None

    class _ExecB(ToolExecutor):
        async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
            _ = ctx
            return [ToolSpec(name="dup.tool")]

        async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
            _ = name, arguments, ctx
            return None

    executor = CompositeToolExecutor(executors=[_ExecA(), _ExecB()])

    with pytest.raises(RuntimeError, match="Duplicate tool name"):
        await executor.list_tools(ctx=ToolContext())


@pytest.mark.asyncio
async def test_composite_executor_can_skip_unavailable_executor_and_keep_local_tools() -> None:
    class _FailingExecutor(ToolExecutor):
        async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
            _ = ctx
            raise RuntimeError("upstream MCP unavailable")

        async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
            _ = name, arguments, ctx
            raise AssertionError("call_tool should not be used for the failing executor")

    executor = CompositeToolExecutor(executors=[_FailingExecutor(), _LocalExecutor()], skip_unavailable=True)

    tools = await executor.list_tools(ctx=ToolContext())
    assert [tool.name for tool in tools] == ["local.transform"]
    failures = executor.list_tool_failures()
    assert len(failures) == 1
    assert failures[0]["executor_label"] == "_FailingExecutor"
    assert failures[0]["error"] == "upstream MCP unavailable"


@pytest.mark.asyncio
async def test_run_mcp_session_logs_and_wraps_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from kafka_a2a import mcp_tools

    class _AsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> "_AsyncClient":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            _ = exc_type, exc, tb
            return False

    class _ClientSession:
        def __init__(self, read_stream: Any, write_stream: Any) -> None:
            _ = read_stream, write_stream

        async def __aenter__(self) -> "_ClientSession":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            _ = exc_type, exc, tb
            return False

        async def initialize(self) -> None:
            return None

    @asynccontextmanager
    async def fake_streamable_http_client(server_url: str, http_client: Any = None) -> Any:
        _ = server_url, http_client
        raise RuntimeError("tls boom")
        yield None

    monkeypatch.setattr(
        mcp_tools,
        "_require_mcp",
        lambda: (SimpleNamespace(AsyncClient=_AsyncClient), _ClientSession, fake_streamable_http_client),
    )

    with caplog.at_level(logging.INFO, logger="kafka_a2a.mcp_tools"):
        with pytest.raises(RuntimeError, match="MCP list_tools failed during connect_stream"):
            await mcp_tools._run_mcp_session(
                server_url="https://inventory.mcp.example/mcp",
                headers={"authorization": "Bearer secret"},
                timeout_s=12.0,
                operation="list_tools",
                callback=lambda session: session.list_tools(),
            )

    assert "event=session_start" in caplog.text
    assert "event=session_failed" in caplog.text
    assert "operation=list_tools" in caplog.text
    assert "server_url=https://inventory.mcp.example/mcp" in caplog.text
    assert "header_names=[authorization]" in caplog.text


@pytest.mark.asyncio
async def test_multi_mcp_executor_wraps_call_tool_failure_with_remote_tool_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kafka_a2a import mcp_tools

    async def fake_run_mcp_session(
        *,
        server_url: str,
        headers: dict[str, str],
        timeout_s: float,
        operation: str,
        callback: Any,
        remote_tool: str | None = None,
        argument_keys: list[str] | None = None,
    ) -> Any:
        _ = server_url, headers, timeout_s, operation, remote_tool, argument_keys

        class _Session:
            async def list_tools(self) -> Any:
                return {"tools": [{"name": "search", "description": "Search products"}]}

            async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
                _ = name, arguments
                raise RuntimeError("remote call blew up")

        return await callback(_Session())

    monkeypatch.setattr(mcp_tools, "_run_mcp_session", fake_run_mcp_session)

    executor = MultiMcpToolExecutor(
        config=MultiMcpToolExecutorConfig(
            agent_name="onboarding",
            servers=[
                McpServerConfig(
                    id="products",
                    server_url="http://products-mcp:8000/mcp",
                    tool_name_prefix="product.",
                )
            ],
        )
    )

    with pytest.raises(
        RuntimeError,
        match=r"exposed tool 'product.search' -> remote tool 'search'",
    ):
        await executor.call_tool(name="product.search", arguments={"query": "milk"}, ctx=ToolContext())


@pytest.mark.asyncio
async def test_composite_executor_skip_warning_includes_inline_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FailingRemoteExecutor(ToolExecutor):
        def debug_metadata(self) -> dict[str, Any]:
            return {
                "executor_label": "mcp:inventory",
                "agent_name": "onboarding",
                "server_url": "https://inventory.mcp.example/mcp",
                "auth_mode": "forward_bearer",
            }

        async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
            _ = ctx
            raise RuntimeError("upstream MCP unavailable")

        async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
            _ = name, arguments, ctx
            raise AssertionError("call_tool should not be used for the failing executor")

    executor = CompositeToolExecutor(executors=[_FailingRemoteExecutor()], skip_unavailable=True)

    with caplog.at_level(logging.WARNING, logger="kafka_a2a.mcp_tools"):
        tools = await executor.list_tools(ctx=ToolContext())

    assert tools == []
    assert "skipping executor mcp:inventory on agent onboarding" in caplog.text
    assert "(https://inventory.mcp.example/mcp)" in caplog.text
    assert "auth=forward_bearer" in caplog.text
