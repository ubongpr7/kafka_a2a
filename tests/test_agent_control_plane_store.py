from __future__ import annotations

import json
from pathlib import Path

from kafka_a2a.core.config import A2AAppSettings
from kafka_a2a.mainapps.agents.db import DatabaseAgentControlPlaneStore
from kafka_a2a.mainapps.agents.models import WorkspaceAgent, WorkspaceAiSettings, WorkspaceToolConnection
from kafka_a2a.mainapps.agents.services import AgentControlPlaneService, AgentRuntimeAccessContext
from kafka_a2a.mainapps.agents.storage import JsonAgentControlPlaneStore


def _settings(tmp_path: Path, *, database_url: str | None = None) -> A2AAppSettings:
    return A2AAppSettings(
        repo_root=Path("/Users/ubongpr7/dev/pr7/inventory"),
        data_dir=tmp_path,
        control_plane_store_path=tmp_path / "control-plane.json",
        database_url=database_url,
    )


def test_json_store_supports_record_upsert_and_delete(tmp_path: Path) -> None:
    store = JsonAgentControlPlaneStore(tmp_path / "control-plane.json")
    record = WorkspaceAiSettings(
        profile="1",
        name="Chat GPT",
        version="gpt-3.5-turbo",
        api_key="secret",
        tavily_api_key="tvly",
    )

    store.upsert_record("workspace_ai_settings", record)
    loaded = store.get_record("workspace_ai_settings", filters={"profile": "1"})

    assert loaded is not None
    assert loaded.name == "Chat GPT"

    deleted = store.delete_records("workspace_ai_settings", filters={"profile": "1"})

    assert deleted == 1
    assert store.get_record("workspace_ai_settings", filters={"profile": "1"}) is None


def test_database_store_supports_record_upsert_and_delete(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'control-plane.sqlite3'}"
    store = DatabaseAgentControlPlaneStore(database_url)
    record = WorkspaceAgent(
        profile="7",
        slug="ops-helper",
        name="Ops Helper",
        description="Operations agent.",
        origin="custom",
        created_by="9",
        updated_by="9",
    )

    store.upsert_record("workspace_agents", record)
    loaded = store.get_record("workspace_agents", filters={"profile": "7", "slug": "ops-helper"})

    assert loaded is not None
    assert loaded.slug == "ops-helper"

    deleted = store.delete_records("workspace_agents", ids=[record.id])

    assert deleted == 1
    assert store.get_record("workspace_agents", record_id=record.id) is None


def test_service_import_payload_uses_repository_writes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = JsonAgentControlPlaneStore(settings.control_plane_store_path)
    service = AgentControlPlaneService(store=store, settings=settings)
    templates = service.list_templates()
    template = templates[0]
    skill_binding = template["skill_bindings"][0]
    tool_bindings = template["tool_bindings"]

    payload = {
        "workspace_ai_settings": [
            {
                "profile": "1",
                "name": "Chat GPT",
                "version": "gpt-3.5-turbo",
                "provider": "chatgpt",
                "provider_label": "ChatGPT",
                "api_key": "sk-test-12345678",
                "tavily_api_key": "tvly-12345678",
            }
        ],
        "workspace_agents": [
            {
                "profile": "1",
                "source_template_slug": template["slug"],
                "origin": "template",
                "visibility": "workspace",
                "routing_policy": "direct",
                "slug": "workspace-host",
                "name": "Workspace Host",
                "description": "Migrated host agent.",
                "protocol_version": template["protocol_version"],
                "preferred_transport": template["preferred_transport"],
                "version": template["version"],
                "supports_authenticated_extended_card": True,
                "default_input_modes": ["text"],
                "default_output_modes": ["text"],
                "system_instruction": "",
                "developer_instruction": "",
                "assistant_instruction": "",
                "llm_temperature": 0.2,
                "max_reasoning_steps": 5,
                "metadata": {},
                "is_enabled": True,
                "created_by": "42",
                "updated_by": "42",
                "skill_bindings": [
                    {
                        "skill_key": skill_binding["skill"]["key"],
                        "order": 0,
                        "is_primary": True,
                        "metadata": {},
                    }
                ],
                "tool_bindings": [
                    {
                        "tool_key": item["tool"]["key"],
                        "order": item["order"],
                        "is_required": item["is_required"],
                        "tool_config": item["tool_config"],
                    }
                    for item in tool_bindings
                ],
            }
        ],
    }

    result = service.import_users_service_payload(payload)
    registry = service.runtime_registry(
        access=AgentRuntimeAccessContext(user_id="42", profile_id="1", is_owner=True, permissions=set())
    )

    assert result["workspace_ai_settings"] == 1
    assert result["workspace_agents"] == 1
    assert registry["agent_count"] == 1
    assert registry["agents"][0]["slug"] == "workspace-host"
    assert service.get_workspace_ai_setup(profile_id="1")["configured"] is True


def test_service_imports_workspace_tool_connections_into_runtime_payload(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = JsonAgentControlPlaneStore(settings.control_plane_store_path)
    service = AgentControlPlaneService(store=store, settings=settings)

    template = next(item for item in service.list_templates() if item["slug"] == "inventory_setup")
    installed = service.install_template(
        profile_id="1",
        user_id="42",
        template_id=template["id"],
        data={"slug": "inventory-setup-runtime"},
    )
    inventory_server = next(item for item in service._list_tool_server_records() if item.server_id == "inventory")  # type: ignore[attr-defined]
    service._upsert_record(  # type: ignore[attr-defined]
        "workspace_tool_connections",
        WorkspaceToolConnection(
            profile="1",
            tool_server_id=inventory_server.id,
            name="Inventory Workspace Connection",
            slug="inventory-workspace",
            connection_scope="workspace",
            auth_type="api_key_header",
            credential_payload_encrypted='{"header_name":"x-api-key","api_key":"workspace-secret"}',
            status="connected",
        ),
    )

    runtime = service.runtime_agent_config(
        access=AgentRuntimeAccessContext(user_id="42", profile_id="1", is_owner=True, permissions=set()),
        slug=installed["slug"],
    )

    inventory_bindings = [
        item
        for item in runtime["tool_bindings"]
        if ((item.get("tool") or {}).get("tool_server") or {}).get("server_id") == "inventory"
    ]

    assert inventory_bindings
    runtime_connections = inventory_bindings[0]["tool"]["tool_server"]["runtime_connections"]
    assert len(runtime_connections) == 1
    assert runtime_connections[0]["connection_scope"] == "workspace"
    assert runtime_connections[0]["owner_user_id"] is None
    assert runtime_connections[0]["auth_type"] == "api_key_header"
    assert runtime_connections[0]["status"] == "connected"
    assert runtime_connections[0]["server_url_override"] is None
    assert runtime_connections[0]["headers"] == {"x-api-key": "workspace-secret"}
    assert runtime_connections[0]["granted_scopes"] == []
    assert runtime_connections[0]["token_expires_at"] is None
    assert runtime_connections[0]["resource_owner_id"] is None
    assert runtime_connections[0]["resource_label"] is None
    assert runtime_connections[0]["metadata"] == {}


def test_service_lists_seeded_external_mcp_tool_servers(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = JsonAgentControlPlaneStore(settings.control_plane_store_path)
    service = AgentControlPlaneService(store=store, settings=settings)

    servers = service.list_tool_servers(profile_id="1")

    shopify = next(item for item in servers if item["server_id"] == "shopify_admin")
    google = next(item for item in servers if item["server_id"] == "google_workspace")

    assert shopify["metadata"]["catalogType"] == "external_mcp"
    assert shopify["auth_config"]["supportedAuthTypes"] == ["oauth_workspace", "api_key_header", "custom"]
    assert google["auth_mode"] == "service_account"
    assert google["metadata"]["provider"] == "Google"


def test_service_sync_local_agent_transports_updates_seeded_templates_and_workspace_agents(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = JsonAgentControlPlaneStore(settings.control_plane_store_path)
    service = AgentControlPlaneService(store=store, settings=settings)

    templates = service._list_template_records()  # type: ignore[attr-defined]
    template = templates[0]
    template.preferred_transport = "kafka"
    template.url = f"kafka://{template.slug}"
    service._upsert_record("templates", template)  # type: ignore[attr-defined]

    agent = WorkspaceAgent(
        profile="1",
        slug="inventory_setup",
        name="Inventory Setup",
        description="Setup agent.",
        preferred_transport="kafka",
        url="kafka://inventory_setup",
        origin="template",
        created_by="1",
        updated_by="1",
    )
    service._upsert_record("workspace_agents", agent)  # type: ignore[attr-defined]

    result = service.sync_local_agent_transports()

    updated_template = service._list_template_records(ids=[template.id])[0]  # type: ignore[attr-defined]
    updated_agent = service._get_workspace_agent_record(profile_id="1", agent_id=agent.id)  # type: ignore[attr-defined]

    assert result["updated"] >= 2
    assert updated_template.preferred_transport == "local"
    assert updated_template.url == f"local://{template.slug}"
    assert updated_agent is not None
    assert updated_agent.preferred_transport == "local"
    assert updated_agent.url == "local://inventory_setup"


def test_build_principal_claim_overrides_falls_back_to_env_key_when_saved_secret_is_stale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("KA2A_FERNET_KEY", raising=False)
    monkeypatch.delenv("FERNET_KEY", raising=False)
    monkeypatch.delenv("KA2A_LLM_API_KEY", raising=False)
    monkeypatch.delenv("KA2A_LLM_API_KEY_ENV", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KA2A_TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("KA2A_TAVILY_API_KEY_ENV", raising=False)
    monkeypatch.setenv("GPT_KEY", "sk-env-fallback")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-env-fallback")

    settings = _settings(tmp_path)
    store = JsonAgentControlPlaneStore(settings.control_plane_store_path)
    service = AgentControlPlaneService(store=store, settings=settings)
    service.import_users_service_payload(
        {
            "workspace_ai_settings": [
                {
                    "profile": "1",
                    "name": "Chat GPT",
                    "version": "gpt-3.5-turbo",
                    "provider": "chatgpt",
                    "provider_label": "ChatGPT",
                    "api_key": "gAAAA-stale-ciphertext",
                    "tavily_api_key": "gAAAA-stale-tavily",
                }
            ]
        }
    )

    payload = service.build_principal_claim_overrides(profile_id="1")

    assert payload["ka2a"]["llm"]["apiKey"] == {"ciphertext": "sk-env-fallback", "alg": "plain"}
    assert "tavily" not in payload["ka2a"]


def test_sync_seed_catalog_updates_template_runtime_and_adds_missing_template_bindings_to_installed_agents(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    store = JsonAgentControlPlaneStore(settings.control_plane_store_path)
    service = AgentControlPlaneService(store=store, settings=settings)

    inventory_template = next(item for item in service._list_template_records() if item.slug == "inventory_fulfillment")  # type: ignore[attr-defined]
    installed = service.install_template(
        profile_id="1",
        user_id="7",
        template_id=inventory_template.id,
        data={"slug": "inventory-fulfillment-local"},
    )

    product_template = next(item for item in service._list_template_records() if item.slug == "product")  # type: ignore[attr-defined]
    product_template.metadata = {"runtime": {"processor": "langgraph-chat", "allowed_downstream_slugs": ["product_catalog_admin"]}}
    service._upsert_record("templates", product_template)  # type: ignore[attr-defined]

    config_path = tmp_path / "mcp-tools-sync.json"
    config = json.loads((settings.repo_root / "kafka_a2a" / "mcp-tools.local.json").read_text(encoding="utf-8"))
    for server in config["agents"]["inventory_fulfillment"]["servers"]:
        if server["ref"] == "inventory" and "list_inventory_items" not in server["tools"]:
            server["tools"].append("list_inventory_items")
            break
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=True), encoding="utf-8")
    monkeypatch.setenv("KA2A_MCP_CONFIG_PATH", str(config_path))

    result = service.sync_seed_catalog_from_seed()

    synced_product = next(item for item in service._list_template_records(ids=[product_template.id]) if item.id == product_template.id)  # type: ignore[attr-defined]
    runtime_metadata = synced_product.metadata.get("runtime") or {}
    assert "host" in runtime_metadata.get("allowed_downstream_slugs", [])

    inventory_tools = service._list_tool_records()  # type: ignore[attr-defined]
    list_inventory_items_tool = next(item for item in inventory_tools if item.key == "inventory.list_inventory_items")
    workspace_agent = service._get_workspace_agent_record(profile_id="1", agent_id=installed["id"])  # type: ignore[attr-defined]
    assert workspace_agent is not None
    workspace_bindings = service._list_workspace_tool_binding_records(agent_ids=[workspace_agent.id])  # type: ignore[attr-defined]
    assert any(item.tool_id == list_inventory_items_tool.id for item in workspace_bindings)
    assert result["updated"] >= 2
