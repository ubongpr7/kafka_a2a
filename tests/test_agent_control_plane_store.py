from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from kafka_a2a.core.config import A2AAppSettings
from kafka_a2a.mainapps.agents.db import DatabaseAgentControlPlaneStore
from kafka_a2a.mainapps.agents.models import AgentControlPlaneState, AgentSkill, WorkspaceAgent, WorkspaceAiSettings, WorkspaceToolConnection
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


def test_database_store_loads_a_complete_relational_snapshot(tmp_path: Path) -> None:
    store = DatabaseAgentControlPlaneStore(f"sqlite:///{tmp_path / 'control-plane.sqlite3'}")
    agent = WorkspaceAgent(
        profile="7",
        slug="ops-helper",
        name="Ops Helper",
        description="Operations agent.",
        origin="custom",
    )
    skill = AgentSkill(key="ops", name="Operations", description="Operations support.")
    store.save(AgentControlPlaneState(workspace_agents=[agent], skills=[skill]))

    loaded = store.load()

    assert [item.slug for item in loaded.workspace_agents] == ["ops-helper"]
    assert [item.key for item in loaded.skills] == ["ops"]


def test_internal_runtime_registry_reuses_its_snapshot_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    service = AgentControlPlaneService(store=JsonAgentControlPlaneStore(settings.control_plane_store_path), settings=settings)
    agent = WorkspaceAgent(profile="7", slug="ops-helper", name="Ops Helper", description="Operations agent.")
    calls: list[str] = []

    monkeypatch.setattr(service, "_list_workspace_agent_records", lambda **kwargs: [agent])

    def _runtime_payloads(received_agents):
        calls.append("payload")
        assert received_agents == [agent]
        return [{"slug": agent.slug}]

    monkeypatch.setattr(service, "_runtime_config_payloads_for_agents", _runtime_payloads)

    first = service.internal_runtime_registry()
    second = service.internal_runtime_registry()

    assert first == {"agent_count": 1, "agents": [{"slug": "ops-helper"}]}
    assert second == first
    assert calls == ["payload"]


def test_database_store_skips_postgres_schema_reflection_when_tables_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    store = DatabaseAgentControlPlaneStore("postgresql://user:password@example.test/control_plane")
    create_all_calls: list[object] = []

    class _FakeDialect:
        name = "postgresql"

    class _FakeEngine:
        dialect = _FakeDialect()

    engine = _FakeEngine()
    create_engine_kwargs: dict[str, object] = {}

    def _create_engine(*args, **kwargs):
        create_engine_kwargs.update(kwargs)
        return engine

    monkeypatch.setattr(sa, "create_engine", _create_engine)
    monkeypatch.setattr(
        store,
        "_control_plane_tables_exist",
        lambda **kwargs: True,
    )
    monkeypatch.setattr(sa.MetaData, "create_all", lambda self, received_engine: create_all_calls.append(received_engine))

    store._ensure_runtime()  # type: ignore[attr-defined]

    assert store._engine is engine  # type: ignore[attr-defined]
    assert create_all_calls == []
    assert create_engine_kwargs["use_native_hstore"] is False


def test_database_store_retries_list_records_after_retryable_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    store = DatabaseAgentControlPlaneStore("sqlite:////tmp/ignored.sqlite3")
    spec = next(item for item in store._entity_specs() if item.field_name == "workspace_ai_settings")  # type: ignore[attr-defined]
    query = object()
    payload = WorkspaceAiSettings(profile="1", name="Recovered", version="gpt-5-mini").model_dump(mode="json")
    reset_calls: list[str] = []

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _FakeConnection:
        def __init__(self) -> None:
            self._calls = 0

        def execute(self, received_query):
            assert received_query is query
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("consuming input failed: SSL error: unexpected eof while reading")
            return _FakeResult([(payload,)])

    class _FakeBegin:
        def __init__(self, connection: _FakeConnection) -> None:
            self._connection = connection

        def __enter__(self):
            return self._connection

        def __exit__(self, exc_type, exc, tb):
            return False

    class _FakeEngine:
        def __init__(self) -> None:
            self.connection = _FakeConnection()
            self.disposed = 0

        def begin(self):
            return _FakeBegin(self.connection)

        def dispose(self) -> None:
            self.disposed += 1

    engine = _FakeEngine()
    monkeypatch.setattr(store, "_ensure_runtime", lambda: setattr(store, "_engine", engine))
    monkeypatch.setattr(store, "_spec_by_field_name", lambda field_name: spec)
    monkeypatch.setattr(store, "_build_filtered_query", lambda *args, **kwargs: query)

    def _wrapped_reset() -> None:
        reset_calls.append("reset")
        store._engine = None
        store._tables = {}
        store._metadata = None
        engine.dispose()

    monkeypatch.setattr(store, "_reset_runtime", _wrapped_reset)

    loaded = store.list_records("workspace_ai_settings", filters={"profile": "1"})

    assert len(loaded) == 1
    assert loaded[0].name == "Recovered"
    assert reset_calls == ["reset"]
    assert engine.disposed == 1


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


def test_workspace_ai_setup_does_not_require_optional_tavily_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = JsonAgentControlPlaneStore(settings.control_plane_store_path)
    service = AgentControlPlaneService(store=store, settings=settings)

    setup = service.save_workspace_ai_setup(
        profile_id="4",
        data={
            "name": "Workspace Assistant",
            "version": "gpt-5-mini",
            "api_key": "sk-test-openai",
        },
    )

    assert setup["configured"] is True
    assert setup["agent"]["has_api_key"] is True
    assert setup["agent"]["has_tavily_api_key"] is False


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


def test_service_sync_users_ai_settings_payload_preserves_workspace_agents(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = JsonAgentControlPlaneStore(settings.control_plane_store_path)
    service = AgentControlPlaneService(store=store, settings=settings)

    installed = service.install_template(
        profile_id="1",
        user_id="42",
        template_id=next(item for item in service.list_templates() if item["slug"] == "host")["id"],
        data={"slug": "host"},
    )

    stats = service.sync_users_ai_settings_payload(
        {
            "workspace_ai_settings": [
                {
                    "profile": "1",
                    "name": "Legacy Agent",
                    "version": "gpt-3.5-turbo",
                    "provider": "chatgpt",
                    "provider_label": "ChatGPT",
                    "provider_base_url": "https://api.openai.com",
                    "base_url": "https://api.openai.com",
                    "special_instruction": "legacy",
                    "system_instruction": "",
                    "assistant_instruction": "",
                    "api_key": "legacy-openai-key",
                    "tavily_api_key": "legacy-tavily-key",
                }
            ]
        }
    )

    assert stats["workspace_ai_settings"] == 1
    setup = service.get_workspace_ai_setup(profile_id="1")
    assert setup["configured"] is True
    assert setup["agent"]["name"] == "Legacy Agent"
    assert setup["agent"]["version"] == "gpt-5-mini"
    registry = service.runtime_registry(
        access=AgentRuntimeAccessContext(user_id="42", profile_id="1", is_owner=True, permissions=set())
    )
    assert any(item["slug"] == installed["slug"] for item in registry["agents"])


def test_workspace_ai_setup_flags_undecryptable_secrets_for_reconfiguration(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = JsonAgentControlPlaneStore(settings.control_plane_store_path)
    service = AgentControlPlaneService(store=store, settings=settings)

    service._upsert_record(  # type: ignore[attr-defined]
        "workspace_ai_settings",
        WorkspaceAiSettings(
            profile="1",
            name="Legacy Agent",
            version="gpt-5-mini",
            api_key="gAAAA-stale-openai",
            tavily_api_key="gAAAA-stale-tavily",
        ),
    )

    setup = service.get_workspace_ai_setup(profile_id="1")

    assert setup["configured"] is True
    assert setup["agent"]["has_api_key"] is False
    assert setup["agent"]["has_tavily_api_key"] is False
    assert setup["agent"]["api_key_requires_reconfiguration"] is True
    assert setup["agent"]["tavily_api_key_requires_reconfiguration"] is True


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


def test_ensure_seeded_refreshes_seed_catalog_tools(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    store = JsonAgentControlPlaneStore(settings.control_plane_store_path)
    service = AgentControlPlaneService(store=store, settings=settings)

    initial_state = service.ensure_seeded()
    initial_tool_count = len(initial_state.tools)
    assert initial_tool_count > 0
    assert all(item.key != "products.test_agentic_tool_discovery" for item in initial_state.tools)

    config_path = tmp_path / "mcp-tools-sync.json"
    config = json.loads((settings.repo_root / "kafka_a2a" / "mcp-tools.local.json").read_text(encoding="utf-8"))
    for server in config["agents"]["product_catalog_admin"]["servers"]:
        if server.get("ref") == "products":
            server["tools"].append("test_agentic_tool_discovery")
            break
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    monkeypatch.setenv("KA2A_MCP_CONFIG_PATH", str(config_path))

    refreshed_state = service.ensure_seeded()

    assert len(refreshed_state.tools) == initial_tool_count + 1
    assert any(item.key == "products.test_agentic_tool_discovery" for item in refreshed_state.tools)


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
    assert payload["ka2a"]["tavily"]["apiKey"] == {"ciphertext": "tvly-env-fallback", "alg": "plain"}


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
