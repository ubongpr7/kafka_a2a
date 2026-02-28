from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from kafka_a2a.credentials import ResolvedMcpCredentials, SecretDecryptor, resolve_mcp_credentials_from_metadata
from kafka_a2a.tenancy import Principal, extract_principal


@dataclass(slots=True)
class ToolSpec:
    """
    Minimal tool definition (MCP-ready).

    This intentionally mirrors common "function tool" shapes (name/description/json-schema),
    without binding K-A2A to any single provider or MCP implementation.
    """

    name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None


@dataclass(slots=True)
class ToolContext:
    """
    Tool execution context derived from task metadata.

    In SaaS mode, `principal` and `mcp` are typically derived from JWT claims forwarded
    through Kafka metadata.
    """

    principal: Principal | None = None
    mcp: ResolvedMcpCredentials | None = None
    metadata: dict[str, Any] | None = None

    @classmethod
    def from_metadata(
        cls,
        *,
        metadata: dict[str, Any] | None,
        decrypt: SecretDecryptor | None = None,
        principal_metadata_key: str = "urn:ka2a:principal",
    ) -> "ToolContext":
        principal = extract_principal(metadata or {}, key=principal_metadata_key)
        mcp = None
        if decrypt is not None:
            try:
                mcp = resolve_mcp_credentials_from_metadata(
                    metadata=metadata,
                    decrypt=decrypt,
                    principal_metadata_key=principal_metadata_key,
                )
            except Exception:
                mcp = None
        return cls(principal=principal, mcp=mcp, metadata=metadata)


class ToolExecutor(Protocol):
    """
    Minimal tool executor interface.

    A future MCP integration can implement this interface and translate to/from MCP calls.
    """

    def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]: ...

    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any: ...

