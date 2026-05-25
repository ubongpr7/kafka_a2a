from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


Scope = Literal["platform", "workspace"]
ToolTransport = Literal["mcp", "native", "webhook", "manual"]
ToolAuthMode = Literal["none", "forward_bearer", "static", "context", "service_account", "custom"]
ToolConnectionScope = Literal["workspace", "user"]
ToolConnectionStatus = Literal["pending", "connected", "expired", "error", "revoked"]
CatalogHealth = Literal["unknown", "healthy", "degraded", "unavailable"]
AgentOrigin = Literal["template", "custom"]
AgentVisibility = Literal["workspace", "private"]
AgentRoutingPolicy = Literal["direct", "orchestrated", "specialist_only"]
InstructionType = Literal["system", "developer", "assistant"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_text_modes() -> list[str]:
    return ["text"]


def default_capabilities() -> dict[str, Any]:
    return {
        "streaming": True,
        "pushNotifications": False,
        "stateTransitionHistory": True,
    }


class TimestampedRecord(BaseModel):
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ToolServer(TimestampedRecord):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: Scope = "platform"
    profile: str | None = None
    is_active: bool = True
    server_id: str
    name: str
    description: str = ""
    transport: ToolTransport = "mcp"
    server_url: str = ""
    tool_name_prefix: str = ""
    auth_mode: ToolAuthMode = "none"
    auth_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    health_status: CatalogHealth = "unknown"
    last_synced_at: datetime | None = None


class AgentTool(TimestampedRecord):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: Scope = "platform"
    profile: str | None = None
    is_active: bool = True
    key: str
    display_name: str
    description: str = ""
    tool_server_id: str | None = None
    remote_tool_name: str
    auth_mode: ToolAuthMode = "none"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    is_discoverable: bool = True
    health_status: CatalogHealth = "unknown"
    last_synced_at: datetime | None = None

    def full_tool_name(self, tool_server: ToolServer | None) -> str:
        prefix = (tool_server.tool_name_prefix or "").strip() if tool_server else ""
        if prefix and not self.remote_tool_name.startswith(prefix):
            return f"{prefix}{self.remote_tool_name}"
        return self.remote_tool_name


class AgentSkill(TimestampedRecord):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: Scope = "platform"
    profile: str | None = None
    is_active: bool = True
    key: str
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    input_modes: list[str] = Field(default_factory=default_text_modes)
    output_modes: list[str] = Field(default_factory=default_text_modes)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentInstructionPreset(TimestampedRecord):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: Scope = "platform"
    profile: str | None = None
    is_active: bool = True
    key: str
    title: str
    description: str = ""
    instruction_type: InstructionType = "system"
    body: str
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class ModelVersionOption(BaseModel):
    id: str
    provider: str
    provider_label: str
    model_name: str
    base_url: str | None = None


class WorkspaceAiSettings(TimestampedRecord):
    id: str = Field(default_factory=lambda: str(uuid4()))
    profile: str
    name: str
    version: str
    base_url: str = ""
    special_instruction: str = ""
    system_instruction: str = ""
    assistant_instruction: str = ""
    api_key: str = ""
    tavily_api_key: str = ""


class WorkspaceToolConnection(TimestampedRecord):
    id: str = Field(default_factory=lambda: str(uuid4()))
    profile: str
    tool_server_id: str
    name: str
    slug: str
    connection_scope: ToolConnectionScope = "workspace"
    owner_user: str | None = None
    auth_type: str = ""
    server_url_override: str = ""
    credential_payload_encrypted: str = ""
    access_token_encrypted: str = ""
    refresh_token_encrypted: str = ""
    token_expires_at: datetime | None = None
    granted_scopes: list[str] = Field(default_factory=list)
    resource_owner_id: str = ""
    resource_label: str = ""
    status: ToolConnectionStatus = "pending"
    last_tested_at: datetime | None = None
    last_error: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = None
    updated_by: str | None = None


class AgentTemplateSkillBinding(TimestampedRecord):
    id: str = Field(default_factory=lambda: str(uuid4()))
    template_id: str
    skill_id: str
    order: int = 0
    is_primary: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentTemplateToolBinding(TimestampedRecord):
    id: str = Field(default_factory=lambda: str(uuid4()))
    template_id: str
    tool_id: str
    order: int = 0
    is_required: bool = False
    tool_config: dict[str, Any] = Field(default_factory=dict)


class A2AAgentDefinition(TimestampedRecord):
    protocol_version: str = "0.3.0"
    slug: str
    name: str
    description: str = ""
    url: str = ""
    preferred_transport: str = "local"
    provider_organization: str = ""
    provider_url: str = ""
    version: str = "0.1.0"
    documentation_url: str = ""
    icon_url: str = ""
    additional_interfaces: list[dict[str, Any]] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=default_capabilities)
    security_schemes: dict[str, Any] = Field(default_factory=dict)
    security: list[dict[str, Any]] = Field(default_factory=list)
    supports_authenticated_extended_card: bool = True
    default_input_modes: list[str] = Field(default_factory=default_text_modes)
    default_output_modes: list[str] = Field(default_factory=default_text_modes)
    system_instruction: str = ""
    developer_instruction: str = ""
    assistant_instruction: str = ""
    llm_version: dict[str, Any] | None = None
    llm_temperature: float = 0.2
    max_reasoning_steps: int = 5
    metadata: dict[str, Any] = Field(default_factory=dict)

    def build_agent_card_payload(
        self,
        *,
        card_name: str | None = None,
        card_url: str | None = None,
        skills: list[AgentSkill] | None = None,
        tool_payload: list[dict[str, Any]] | None = None,
        metadata_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_name = (card_name or self.slug).strip()
        resolved_url = (card_url or self.url or f"{self.preferred_transport}://{resolved_name}").strip()
        payload: dict[str, Any] = {
            "protocolVersion": self.protocol_version,
            "name": resolved_name,
            "description": self.description,
            "url": resolved_url,
            "preferredTransport": self.preferred_transport,
            "version": self.version,
            "capabilities": deepcopy(self.capabilities) or {},
            "defaultInputModes": deepcopy(self.default_input_modes) or ["text"],
            "defaultOutputModes": deepcopy(self.default_output_modes) or ["text"],
        }
        if self.provider_organization or self.provider_url:
            payload["provider"] = {
                "organization": self.provider_organization or None,
                "url": self.provider_url or None,
            }
        if self.documentation_url:
            payload["documentationUrl"] = self.documentation_url
        if self.icon_url:
            payload["iconUrl"] = self.icon_url
        if self.additional_interfaces:
            payload["additionalInterfaces"] = deepcopy(self.additional_interfaces)
        if self.security_schemes:
            payload["securitySchemes"] = deepcopy(self.security_schemes)
        if self.security:
            payload["security"] = deepcopy(self.security)
        if self.supports_authenticated_extended_card:
            payload["supportsAuthenticatedExtendedCard"] = True
        if skills:
            payload["skills"] = [
                {
                    "id": skill.key,
                    "name": skill.name,
                    "description": skill.description,
                    "tags": deepcopy(skill.tags),
                    "examples": deepcopy(skill.examples),
                    "inputModes": deepcopy(skill.input_modes) or ["text"],
                    "outputModes": deepcopy(skill.output_modes) or ["text"],
                }
                for skill in skills
            ]
        if tool_payload:
            payload.setdefault("metadata", {})
            payload["metadata"]["tools"] = deepcopy(tool_payload)
        if metadata_overrides:
            payload.setdefault("metadata", {})
            payload["metadata"].update(deepcopy(metadata_overrides))
        return payload


class AgentTemplate(A2AAgentDefinition):
    id: str = Field(default_factory=lambda: str(uuid4()))
    is_active: bool = True
    is_featured: bool = False
    allow_workspace_installs: bool = True
    sort_order: int = 0


class WorkspaceAgentSkillBinding(TimestampedRecord):
    id: str = Field(default_factory=lambda: str(uuid4()))
    agent_id: str
    skill_id: str
    order: int = 0
    is_primary: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspaceAgentToolBinding(TimestampedRecord):
    id: str = Field(default_factory=lambda: str(uuid4()))
    agent_id: str
    tool_id: str
    order: int = 0
    is_required: bool = False
    tool_config: dict[str, Any] = Field(default_factory=dict)


class WorkspaceAgent(A2AAgentDefinition):
    id: str = Field(default_factory=lambda: str(uuid4()))
    profile: str
    source_template_id: str | None = None
    origin: AgentOrigin = "custom"
    visibility: AgentVisibility = "workspace"
    routing_policy: AgentRoutingPolicy = "direct"
    is_enabled: bool = True
    template_version_snapshot: str = ""
    created_by: str | None = None
    updated_by: str | None = None

    def build_runtime_name(self) -> str:
        compact = self.id.replace("-", "")[:12]
        return f"wa-p{self.profile}-{self.slug}-{compact}"

    def build_runtime_metadata(self) -> dict[str, Any]:
        return {
            "ka2aRuntime": {
                "runtimeName": self.build_runtime_name(),
                "publicSlug": self.slug,
                "workspaceAgentId": self.id,
                "profileId": self.profile,
                "visibility": self.visibility,
            }
        }

    def build_runtime_card_payload(
        self,
        *,
        skills: list[AgentSkill] | None = None,
        tool_payload: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        runtime_name = self.build_runtime_name()
        return self.build_agent_card_payload(
            card_name=runtime_name,
            card_url=self.url or f"{self.preferred_transport}://{runtime_name}",
            skills=skills,
            tool_payload=tool_payload,
            metadata_overrides=self.build_runtime_metadata(),
        )

    def build_runtime_config(self, *, template_runtime: dict[str, Any] | None = None) -> dict[str, Any]:
        runtime_config = deepcopy(template_runtime or {})
        runtime_config.update(deepcopy((self.metadata or {}).get("runtime") or {}))
        runtime_config.setdefault("processor", "langgraph-chat")
        runtime_config.setdefault("runtimeName", self.build_runtime_name())
        runtime_config.setdefault("publicSlug", self.slug)
        runtime_config.setdefault("profileId", self.profile)
        runtime_config.setdefault("workspaceAgentId", self.id)
        runtime_config.setdefault("visibility", self.visibility)
        return runtime_config


class AgentControlPlaneState(BaseModel):
    model_versions: list[ModelVersionOption] = Field(default_factory=list)
    tool_servers: list[ToolServer] = Field(default_factory=list)
    tools: list[AgentTool] = Field(default_factory=list)
    skills: list[AgentSkill] = Field(default_factory=list)
    instruction_presets: list[AgentInstructionPreset] = Field(default_factory=list)
    templates: list[AgentTemplate] = Field(default_factory=list)
    template_skill_bindings: list[AgentTemplateSkillBinding] = Field(default_factory=list)
    template_tool_bindings: list[AgentTemplateToolBinding] = Field(default_factory=list)
    workspace_ai_settings: list[WorkspaceAiSettings] = Field(default_factory=list)
    workspace_tool_connections: list[WorkspaceToolConnection] = Field(default_factory=list)
    workspace_agents: list[WorkspaceAgent] = Field(default_factory=list)
    workspace_skill_bindings: list[WorkspaceAgentSkillBinding] = Field(default_factory=list)
    workspace_tool_bindings: list[WorkspaceAgentToolBinding] = Field(default_factory=list)
