from __future__ import annotations

import json
import os
from pathlib import Path

from kafka_a2a.core.config import A2AAppSettings

from .models import (
    AgentControlPlaneState,
    AgentInstructionPreset,
    AgentSkill,
    AgentTemplate,
    AgentTemplateSkillBinding,
    AgentTemplateToolBinding,
    AgentTool,
    ModelVersionOption,
    ToolServer,
)


_DEFAULT_MODEL_VERSIONS: list[ModelVersionOption] = [
    ModelVersionOption(id="gpt-5.4", provider="chatgpt", provider_label="ChatGPT", model_name="gpt-5.4", base_url="https://api.openai.com"),
    ModelVersionOption(id="gpt-5.4-pro", provider="chatgpt", provider_label="ChatGPT", model_name="gpt-5.4-pro", base_url="https://api.openai.com"),
    ModelVersionOption(id="gpt-5-mini", provider="chatgpt", provider_label="ChatGPT", model_name="gpt-5-mini", base_url="https://api.openai.com"),
    ModelVersionOption(id="gpt-5-nano", provider="chatgpt", provider_label="ChatGPT", model_name="gpt-5-nano", base_url="https://api.openai.com"),
    ModelVersionOption(id="gemini-2.5-pro", provider="gemini", provider_label="Gemini", model_name="gemini-2.5-pro", base_url=""),
    ModelVersionOption(id="gemini-2.5-flash", provider="gemini", provider_label="Gemini", model_name="gemini-2.5-flash", base_url=""),
    ModelVersionOption(id="grok-4", provider="grok", provider_label="Grok", model_name="grok-4", base_url="https://api.x.ai"),
]


_EXTERNAL_TOOL_SERVERS: tuple[dict[str, object], ...] = (
    {
        "id": "shopify_admin",
        "name": "Shopify Admin MCP",
        "description": (
            "Merchant-side Shopify MCP connection for catalog, inventory, orders, and fulfillment workflows. "
            "Use your own hosted MCP endpoint or a trusted provider endpoint."
        ),
        "transport": "mcp",
        "serverUrl": "",
        "toolNamePrefix": "shopify.",
        "auth": {
            "mode": "custom",
            "recommendedAuthType": "custom",
            "supportedAuthTypes": ["oauth_workspace", "api_key_header", "custom"],
            "notes": [
                "Shopify merchant-admin integrations usually need OAuth or an Admin API token.",
                "Use Server URL Override if your MCP endpoint is tenant-specific.",
            ],
            "credentialExample": {
                "store_domain": "demo-store.myshopify.com",
                "header_name": "X-Shopify-Access-Token",
                "api_key": "shpat_xxx",
            },
        },
        "metadata": {
            "catalogType": "external_mcp",
            "category": "commerce",
            "provider": "Shopify",
            "documentationLabel": "Shopify merchant MCP via your hosted endpoint",
            "connectionGuide": [
                "Choose this server when you want an agent to read or update Shopify merchant data.",
                "Save the store token in Credential Payload JSON or use OAuth Workspace when available.",
                "Set Server URL Override to the actual MCP endpoint if the default catalog entry is blank.",
            ],
            "suggestedCapabilities": [
                "import_products",
                "sync_inventory_levels",
                "pull_orders",
                "push_fulfillment_status",
            ],
        },
    },
    {
        "id": "notion",
        "name": "Notion MCP",
        "description": "External Notion MCP connection for workspace docs, databases, and knowledge search.",
        "transport": "mcp",
        "serverUrl": "",
        "toolNamePrefix": "notion.",
        "auth": {
            "mode": "custom",
            "recommendedAuthType": "oauth_workspace",
            "supportedAuthTypes": ["oauth_workspace", "api_key_header", "custom"],
            "credentialExample": {
                "header_name": "Authorization",
                "token": "secret_xxx",
            },
        },
        "metadata": {
            "catalogType": "external_mcp",
            "category": "knowledge",
            "provider": "Notion",
            "connectionGuide": [
                "Use this for knowledge-base lookup, page updates, or database automation.",
                "Most hosted Notion MCP setups are OAuth-oriented.",
            ],
            "suggestedCapabilities": ["search_docs", "update_pages", "query_databases"],
        },
    },
    {
        "id": "slack",
        "name": "Slack MCP",
        "description": "External Slack MCP connection for channels, threads, approvals, and notifications.",
        "transport": "mcp",
        "serverUrl": "",
        "toolNamePrefix": "slack.",
        "auth": {
            "mode": "custom",
            "recommendedAuthType": "oauth_workspace",
            "supportedAuthTypes": ["oauth_workspace", "custom"],
            "credentialExample": {
                "workspace_id": "T123456",
                "bot_token": "xoxb-xxx",
            },
        },
        "metadata": {
            "catalogType": "external_mcp",
            "category": "collaboration",
            "provider": "Slack",
            "connectionGuide": [
                "Use this for notification agents, escalation flows, or channel summaries.",
                "Slack MCP setups are usually app-backed and OAuth-based.",
            ],
            "suggestedCapabilities": ["post_messages", "read_threads", "list_channels"],
        },
    },
    {
        "id": "github",
        "name": "GitHub MCP",
        "description": "External GitHub MCP connection for repositories, issues, pull requests, and code search.",
        "transport": "mcp",
        "serverUrl": "",
        "toolNamePrefix": "github.",
        "auth": {
            "mode": "custom",
            "recommendedAuthType": "api_key_header",
            "supportedAuthTypes": ["api_key_header", "oauth_workspace", "custom"],
            "credentialExample": {
                "header_name": "Authorization",
                "token": "ghp_xxx",
            },
        },
        "metadata": {
            "catalogType": "external_mcp",
            "category": "engineering",
            "provider": "GitHub",
            "connectionGuide": [
                "Use this for repository-aware engineering or issue triage agents.",
                "A personal access token is the simplest first pass.",
            ],
            "suggestedCapabilities": ["read_repos", "search_code", "manage_issues", "review_prs"],
        },
    },
    {
        "id": "google_workspace",
        "name": "Google Workspace MCP",
        "description": "External Google Workspace MCP connection for Drive, Gmail, Calendar, and docs-oriented flows.",
        "transport": "mcp",
        "serverUrl": "",
        "toolNamePrefix": "google.",
        "auth": {
            "mode": "service_account",
            "recommendedAuthType": "service_account",
            "supportedAuthTypes": ["oauth_workspace", "service_account", "custom"],
            "credentialExample": {
                "project_id": "your-gcp-project",
                "client_email": "service-account@project.iam.gserviceaccount.com",
                "private_key": "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n",
            },
        },
        "metadata": {
            "catalogType": "external_mcp",
            "category": "workspace",
            "provider": "Google",
            "connectionGuide": [
                "Use service-account mode for server-to-server access where supported.",
                "Use OAuth Workspace when the MCP provider expects per-user authorization.",
            ],
            "suggestedCapabilities": ["search_drive", "read_mail", "calendar_lookup"],
        },
    },
)


_TEMPLATE_RUNTIME_METADATA: dict[str, dict[str, object]] = {
    "host": {
        "processor": "langgraph-chat",
        "tool_executor": "kafka_a2a.local_tools:build_interaction_tool_executor",
        "allowed_downstream_slugs": ["onboarding", "users", "product", "inventory", "pos"],
    },
    "onboarding": {
        "processor": "langgraph-chat",
        "tool_executor": "kafka_a2a.local_tools:build_interaction_tool_executor",
        "allowed_downstream_slugs": ["host", "users", "product", "inventory"],
    },
    "product": {
        "processor": "langgraph-chat",
        "tool_executor": "kafka_a2a.local_tools:build_interaction_tool_executor",
        "allowed_downstream_slugs": [
            "host",
            "product_discovery",
            "marketplace_sourcing",
            "product_catalog_admin",
            "product_merchandising",
            "product_pricing",
        ],
    },
    "marketplace_sourcing": {
        "processor": "langgraph-chat",
        "tool_executor": "kafka_a2a.marketplace_tools:build_marketplace_sourcing_tool_executor",
    },
    "inventory": {
        "processor": "langgraph-chat",
        "tool_executor": "kafka_a2a.local_tools:build_interaction_tool_executor",
        "allowed_downstream_slugs": [
            "host",
            "inventory_visibility",
            "inventory_setup",
            "inventory_procurement",
            "inventory_fulfillment",
        ],
    },
    "pos": {
        "processor": "langgraph-chat",
        "tool_executor": "kafka_a2a.local_tools:build_interaction_tool_executor",
        "allowed_downstream_slugs": ["host", "pos_live", "pos_admin"],
    },
}


def _humanize(value: str) -> str:
    return value.replace(".", " ").replace("_", " ").strip().title()


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _seeded_from(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _resolve_mcp_config_path(settings: A2AAppSettings, ka2a_root: Path) -> Path:
    configured = (os.getenv("KA2A_MCP_CONFIG_PATH") or "").strip()
    candidates: list[Path] = []
    if configured:
        configured_path = Path(configured)
        if not configured_path.is_absolute():
            candidates.append(settings.repo_root / configured_path)
            candidates.append(ka2a_root / configured_path)
        candidates.append(configured_path)
    candidates.extend(
        [
            ka2a_root / "mcp-tools.local.json",
            ka2a_root / "mcp-tools.dev.json",
            ka2a_root / "mcp-tools.prod.json",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return ka2a_root / "mcp-tools.prod.json"


def build_seed_state(settings: A2AAppSettings) -> AgentControlPlaneState:
    state = AgentControlPlaneState()
    state.model_versions = list(_DEFAULT_MODEL_VERSIONS)
    ka2a_root = settings.repo_root
    if not (ka2a_root / "agent_cards").exists():
        ka2a_root = settings.repo_root / "kafka_a2a"
    agent_card_dir = ka2a_root / "agent_cards"
    prompt_dir = ka2a_root / "prompts"
    mcp_config_path = _resolve_mcp_config_path(settings, ka2a_root)

    config = _load_json(mcp_config_path)
    server_map: dict[str, ToolServer] = {}

    server_catalog = list(config.get("sharedServers") or [])
    server_catalog.extend(_EXTERNAL_TOOL_SERVERS)

    for server_data in server_catalog:
        server = ToolServer(
            server_id=server_data["id"],
            name=str(server_data.get("name") or _humanize(server_data["id"])),
            description=str(server_data.get("description") or f"MCP server for {server_data['id']} tools."),
            transport="mcp",
            server_url=server_data.get("serverUrl", ""),
            tool_name_prefix=server_data.get("toolNamePrefix", ""),
            auth_mode=((server_data.get("auth") or {}).get("mode") or "none").strip().lower() or "none",
            auth_config=server_data.get("auth") or {},
            metadata=server_data.get("metadata") or server_data,
        )
        state.tool_servers.append(server)
        server_map[server.server_id] = server

    template_map: dict[str, AgentTemplate] = {}
    skill_map: dict[str, AgentSkill] = {}
    tool_map: dict[str, AgentTool] = {}

    for card_path in sorted(agent_card_dir.glob("*.agent-card.json")):
        card = _load_json(card_path)
        slug = card["name"]
        prompt_path = prompt_dir / f"{slug}_agent.txt"
        prompt_text = _load_text(prompt_path) if prompt_path.exists() else ""

        template = AgentTemplate(
            slug=slug,
            name=_humanize(slug),
            description=card.get("description", ""),
            protocol_version=card.get("protocolVersion", "0.3.0"),
            url=card.get("url", ""),
            preferred_transport=card.get("preferredTransport", "local"),
            version=card.get("version", "0.1.0"),
            capabilities=card.get("capabilities") or {},
            default_input_modes=card.get("defaultInputModes") or ["text"],
            default_output_modes=card.get("defaultOutputModes") or ["text"],
            system_instruction=prompt_text,
            metadata={
                "seededFrom": str(card_path.relative_to(settings.repo_root)),
                "runtime": {
                    "processor": "langgraph-chat",
                    **_TEMPLATE_RUNTIME_METADATA.get(slug, {}),
                },
            },
            is_featured=slug in {"host", "onboarding", "users", "product", "inventory", "pos"},
        )
        state.templates.append(template)
        template_map[slug] = template

        state.instruction_presets.append(
            AgentInstructionPreset(
                key=f"{slug}.system",
                title=f"{_humanize(slug)} System Instruction",
                description=card.get("description", ""),
                instruction_type="system",
                body=prompt_text,
                tags=[slug],
                metadata={"seededFrom": str(prompt_path.relative_to(settings.repo_root)) if prompt_path.exists() else None},
                is_default=slug in {"host", "onboarding", "users", "product", "inventory", "pos"},
            )
        )

        for order, skill_data in enumerate(card.get("skills") or []):
            skill = skill_map.get(skill_data["id"])
            if skill is None:
                skill = AgentSkill(
                    key=skill_data["id"],
                    name=skill_data.get("name") or _humanize(skill_data["id"]),
                    description=skill_data.get("description", ""),
                    tags=skill_data.get("tags") or [],
                    examples=skill_data.get("examples") or [],
                    input_modes=skill_data.get("inputModes") or ["text"],
                    output_modes=skill_data.get("outputModes") or ["text"],
                    metadata={"seededFrom": str(card_path.relative_to(settings.repo_root))},
                )
                state.skills.append(skill)
                skill_map[skill.key] = skill
            state.template_skill_bindings.append(
                AgentTemplateSkillBinding(
                    template_id=template.id,
                    skill_id=skill.id,
                    order=order,
                    is_primary=order == 0,
                )
            )

    for agent_name, agent_data in (config.get("agents") or {}).items():
        template = template_map.get(agent_name)
        if template is None:
            continue
        tool_order = 0
        for server_data in agent_data.get("servers") or []:
            server = server_map.get(server_data.get("ref", ""))
            if server is None:
                continue
            for tool_name in server_data.get("tools") or []:
                tool_key = f"{server.server_id}.{tool_name}"
                tool = tool_map.get(tool_key)
                if tool is None:
                    tool = AgentTool(
                        key=tool_key,
                        display_name=_humanize(tool_name),
                        description="",
                        tool_server_id=server.id,
                        remote_tool_name=tool_name,
                        auth_mode=server.auth_mode,
                        metadata={"serverRef": server.server_id, "seededFrom": _seeded_from(mcp_config_path, settings.repo_root)},
                    )
                    state.tools.append(tool)
                    tool_map[tool.key] = tool
                state.template_tool_bindings.append(
                    AgentTemplateToolBinding(
                        template_id=template.id,
                        tool_id=tool.id,
                        order=tool_order,
                        is_required=False,
                    )
                )
                tool_order += 1
    return state
