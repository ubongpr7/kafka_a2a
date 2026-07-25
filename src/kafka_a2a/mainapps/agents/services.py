from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from threading import RLock
from time import monotonic
from typing import Any
from uuid import uuid4

from kafka_a2a.credentials import KA2A_JWT_CLAIM_KEY
from kafka_a2a.secrets import decrypt_fernet_secret

from .bootstrap import build_seed_state
from .models import (
    AgentControlPlaneState,
    AgentSkill,
    AgentTemplate,
    AgentTemplateSkillBinding,
    AgentTemplateToolBinding,
    AgentTool,
    ModelVersionOption,
    ToolServer,
    WorkspaceAiSettings,
    WorkspaceAgent,
    WorkspaceAgentSkillBinding,
    WorkspaceAgentToolBinding,
    WorkspaceToolConnection,
    utcnow,
)
from .storage import JsonAgentControlPlaneStore


class AgentControlPlaneError(RuntimeError):
    pass


def _serialize_model_version(value: Any) -> dict[str, Any] | None:
    if value in (None, "", {}):
        return None
    if isinstance(value, dict):
        return value
    return None


def _infer_provider_from_model_name(model_name: str) -> tuple[str, str]:
    normalized = (model_name or "").strip().lower()
    if normalized.startswith("gpt"):
        return "chatgpt", "ChatGPT"
    if normalized.startswith("gemini"):
        return "gemini", "Gemini"
    if normalized.startswith("grok"):
        return "grok", "Grok"
    return "unknown", "Unknown"


def _normalize_workspace_model_version_id(version_id: str, provider: str) -> str:
    normalized = (version_id or "").strip()
    provider_lower = (provider or "").strip().lower()
    aliases = {
        "gpt-3.5-turbo": "gpt-5-mini",
        "gpt-4.1-mini": "gpt-5-mini",
        "gpt-4o-mini": "gpt-5-mini",
        "gpt-4.1": "gpt-5.4",
        "gpt-4o": "gpt-5.4",
    }
    if provider_lower in {"chatgpt", "openai"}:
        return aliases.get(normalized, normalized)
    return normalized


def _encrypt_secret(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        from cryptography.fernet import Fernet

        key = (__import__("os").environ.get("KA2A_FERNET_KEY") or __import__("os").environ.get("FERNET_KEY") or "").strip()
        if not key:
            return raw
        return Fernet(key).encrypt(raw.encode()).decode()
    except Exception:
        return raw


def _encrypt_json_payload(value: object | None) -> str:
    if value in (None, "", {}, []):
        return ""
    return _encrypt_secret(json.dumps(value, separators=(",", ":"), ensure_ascii=True))


def _secret_for_claim(value: str) -> dict[str, str]:
    encrypted = _encrypt_secret(value)
    if encrypted.startswith("gAAAA"):
        return {"ciphertext": encrypted, "alg": "fernet"}
    return {"ciphertext": encrypted, "alg": "plain"}


def _decrypt_secret(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith("gAAAA"):
        try:
            return decrypt_fernet_secret(type("EncryptedSecretLike", (), {"ciphertext": raw, "alg": "fernet"})())  # type: ignore[arg-type]
        except Exception:
            return raw
    return raw


def _has_usable_secret(value: str) -> bool:
    decrypted = _decrypt_secret(value)
    return bool(decrypted and not decrypted.startswith("gAAAA"))


def _decrypt_json_secret(value: str) -> dict[str, Any]:
    raw = _decrypt_secret(value)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_env_secret(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _resolve_env_llm_api_key(provider: str) -> str:
    direct = _resolve_env_secret("KA2A_LLM_API_KEY")
    if direct:
        return direct
    configured_env_name = (os.environ.get("KA2A_LLM_API_KEY_ENV") or "").strip()
    if configured_env_name:
        configured = _resolve_env_secret(configured_env_name)
        if configured:
            return configured

    provider_lower = (provider or "").strip().lower()
    if provider_lower in {"chatgpt", "openai", "openai_compat", "openai-compatible", "openai-compatible-api"}:
        return _resolve_env_secret("OPENAI_API_KEY", "GPT_KEY")
    if provider_lower in {"gemini", "google", "google_genai", "google-genai"}:
        return _resolve_env_secret("GOOGLE_API_KEY", "GEMINI_API_KEY")
    if provider_lower in {"grok", "xai"}:
        return _resolve_env_secret("XAI_API_KEY")
    return ""

def _connection_runtime_headers(connection: WorkspaceToolConnection) -> dict[str, str]:
    payload = _decrypt_json_secret(connection.credential_payload_encrypted)
    headers: dict[str, str] = {}

    raw_headers = payload.get("headers")
    if isinstance(raw_headers, dict):
        for key, value in raw_headers.items():
            header_name = str(key or "").strip()
            header_value = str(value or "").strip()
            if header_name and header_value:
                headers[header_name] = header_value

    token = _decrypt_secret(connection.access_token_encrypted)
    if not token:
        token = str(
            payload.get("api_key")
            or payload.get("apiKey")
            or payload.get("token")
            or payload.get("value")
            or payload.get("secret")
            or ""
        ).strip()
    if token:
        header_name = str(payload.get("header_name") or payload.get("headerName") or "authorization").strip() or "authorization"
        default_scheme = "Bearer" if header_name.lower() == "authorization" else ""
        scheme = str(payload.get("scheme") or default_scheme).strip()
        headers[header_name] = f"{scheme} {token}".strip() if scheme else token

    return headers


def _require_mcp():
    try:
        import httpx  # type: ignore
        from mcp import ClientSession  # type: ignore
        from mcp.client.streamable_http import streamable_http_client  # type: ignore
    except Exception as exc:
        raise RuntimeError("MCP client extras are not installed in agent service.") from exc
    return httpx, ClientSession, streamable_http_client


async def _probe_mcp_server(*, server_url: str, headers: dict[str, str], timeout_s: float = 15.0) -> dict[str, object]:
    httpx, ClientSession, streamable_http_client = _require_mcp()

    async with httpx.AsyncClient(
        headers=headers,
        timeout=float(timeout_s),
        follow_redirects=True,
    ) as client:
        async with streamable_http_client(server_url, http_client=client) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()

    raw_tools = result.get("tools", result) if isinstance(result, dict) else getattr(result, "tools", result)
    tool_names: list[str] = []
    if isinstance(raw_tools, list):
        for item in raw_tools:
            name = getattr(item, "name", None) if not isinstance(item, dict) else item.get("name")
            if isinstance(name, str) and name.strip():
                tool_names.append(name.strip())

    return {
        "tool_count": len(tool_names),
        "sample_tools": tool_names[:10],
    }


@dataclass(slots=True)
class AgentRuntimeAccessContext:
    user_id: str
    profile_id: str
    is_owner: bool
    permissions: set[str]

    def can_manage_setup(self) -> bool:
        return self.is_owner or "manage_agent_settings" in self.permissions

    def can_interact(self) -> bool:
        return self.is_owner or "interact_with_agent" in self.permissions


class AgentControlPlaneService:
    def __init__(self, *, store: JsonAgentControlPlaneStore, settings) -> None:
        self._store = store
        self._settings = settings
        self._cache_ttl_s = 30.0
        self._cache_lock = RLock()
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}

    def _cache_get(self, key: tuple[Any, ...]) -> Any | None:
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= monotonic():
                self._cache.pop(key, None)
                return None
            return deepcopy(value)

    def _cache_set(self, key: tuple[Any, ...], value: Any) -> Any:
        stored = deepcopy(value)
        with self._cache_lock:
            self._cache[key] = (monotonic() + self._cache_ttl_s, stored)
        return deepcopy(stored)

    def _cache_clear(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    @staticmethod
    def _tool_server_signature(server: ToolServer) -> tuple[Any, ...]:
        return (
            server.server_id,
            server.name,
            server.description,
            server.transport,
            server.server_url,
            server.tool_name_prefix,
            server.auth_mode,
            deepcopy(server.auth_config),
            deepcopy(server.metadata),
            server.is_active,
            server.scope,
            server.profile,
        )

    def ensure_seeded(self) -> AgentControlPlaneState:
        state = self._store.load()
        if state.templates:
            seeded_state = build_seed_state(self._settings)
            current_servers = {item.server_id: item for item in state.tool_servers}
            seeded_servers = {item.server_id: item for item in seeded_state.tool_servers}
            refreshed = False

            updated_servers: list[ToolServer] = []
            for server_id, seeded_server in seeded_servers.items():
                current_server = current_servers.get(server_id)
                if current_server is None:
                    updated_servers.append(seeded_server)
                    refreshed = True
                    continue
                if self._tool_server_signature(current_server) != self._tool_server_signature(seeded_server):
                    replacement = current_server.model_copy(deep=True)
                    replacement.name = seeded_server.name
                    replacement.description = seeded_server.description
                    replacement.transport = seeded_server.transport
                    replacement.server_url = seeded_server.server_url
                    replacement.tool_name_prefix = seeded_server.tool_name_prefix
                    replacement.auth_mode = seeded_server.auth_mode
                    replacement.auth_config = deepcopy(seeded_server.auth_config)
                    replacement.metadata = deepcopy(seeded_server.metadata)
                    replacement.is_active = seeded_server.is_active
                    replacement.scope = seeded_server.scope
                    replacement.profile = seeded_server.profile
                    updated_servers.append(replacement)
                    refreshed = True
                    continue
                updated_servers.append(current_server)

            if refreshed:
                state.tool_servers = updated_servers
                self._cache_clear()
                state = self._store.save(state)

            seed_sync = self.sync_seed_catalog_from_seed()
            if seed_sync.get("updated", 0) > 0:
                self._cache_clear()
                return self._store.load()
            return state
        state = build_seed_state(self._settings)
        return self._store.save(state)

    def sync_tool_servers_from_seed(self) -> dict[str, Any]:
        state = self._store.load()
        seeded_state = build_seed_state(self._settings)
        current_servers = {item.server_id: item for item in state.tool_servers}
        seeded_servers = {item.server_id: item for item in seeded_state.tool_servers}

        updated_servers: list[ToolServer] = []
        changes: list[dict[str, str]] = []

        for server_id, seeded_server in seeded_servers.items():
            current_server = current_servers.get(server_id)
            if current_server is None:
                updated_servers.append(seeded_server)
                changes.append(
                    {
                        "server_id": server_id,
                        "old_url": "",
                        "new_url": seeded_server.server_url,
                        "action": "created",
                    }
                )
                continue

            replacement = current_server.model_copy(deep=True)
            replacement.name = seeded_server.name
            replacement.description = seeded_server.description
            replacement.transport = seeded_server.transport
            replacement.server_url = seeded_server.server_url
            replacement.tool_name_prefix = seeded_server.tool_name_prefix
            replacement.auth_mode = seeded_server.auth_mode
            replacement.auth_config = deepcopy(seeded_server.auth_config)
            replacement.metadata = deepcopy(seeded_server.metadata)
            replacement.is_active = seeded_server.is_active
            replacement.scope = seeded_server.scope
            replacement.profile = seeded_server.profile
            if self._tool_server_signature(current_server) != self._tool_server_signature(replacement):
                changes.append(
                    {
                        "server_id": server_id,
                        "old_url": current_server.server_url,
                        "new_url": replacement.server_url,
                        "action": "updated",
                    }
                )
            updated_servers.append(replacement)

        if not changes:
            return {
                "updated": 0,
                "created": 0,
                "changes": [],
            }

        state.tool_servers = updated_servers
        self._cache_clear()
        self._store.save(state)
        return {
            "updated": sum(1 for item in changes if item["action"] == "updated"),
            "created": sum(1 for item in changes if item["action"] == "created"),
            "changes": changes,
        }

    def sync_local_agent_transports(self) -> dict[str, Any]:
        state = self._store.load()
        changes: list[dict[str, str]] = []

        for template in state.templates:
            old_transport = str(template.preferred_transport or "").strip() or "kafka"
            old_url = str(template.url or "").strip()
            new_url = old_url
            changed = False
            if old_transport != "local":
                template.preferred_transport = "local"
                changed = True
            if old_url.startswith("kafka://"):
                new_url = f"local://{template.slug}"
                template.url = new_url
                changed = True
            if changed:
                changes.append(
                    {
                        "kind": "template",
                        "slug": template.slug,
                        "old_transport": old_transport,
                        "new_transport": "local",
                        "old_url": old_url,
                        "new_url": new_url,
                    }
                )

        for agent in state.workspace_agents:
            old_transport = str(agent.preferred_transport or "").strip() or "kafka"
            old_url = str(agent.url or "").strip()
            new_url = old_url
            changed = False
            if old_transport != "local":
                agent.preferred_transport = "local"
                changed = True
            if old_url.startswith("kafka://"):
                new_url = f"local://{agent.slug}"
                agent.url = new_url
                changed = True
            if changed:
                changes.append(
                    {
                        "kind": "workspace_agent",
                        "slug": agent.slug,
                        "old_transport": old_transport,
                        "new_transport": "local",
                        "old_url": old_url,
                        "new_url": new_url,
                    }
                )

        if not changes:
            return {"updated": 0, "changes": []}

        self._cache_clear()
        self._store.save(state)
        return {"updated": len(changes), "changes": changes}

    @staticmethod
    def _merge_seeded_record(current: object, seeded: object) -> object:
        payload = seeded.model_dump(mode="python")
        payload["id"] = getattr(current, "id")
        payload["created_at"] = getattr(current, "created_at")
        payload["updated_at"] = utcnow()
        return type(current).model_validate(payload)

    @staticmethod
    def _record_changed(current: object, replacement: object) -> bool:
        return current.model_dump(mode="json", exclude={"updated_at"}) != replacement.model_dump(
            mode="json",
            exclude={"updated_at"},
        )

    @staticmethod
    def _workspace_seed_template_slugs(templates: list[AgentTemplate]) -> set[str]:
        templates_by_slug = {
            template.slug: template
            for template in templates
            if template.allow_workspace_installs
        }
        required = {
            template.slug
            for template in templates
            if template.is_featured and template.allow_workspace_installs
        }
        pending = list(required)

        while pending:
            slug = pending.pop()
            template = templates_by_slug.get(slug)
            if template is None:
                continue
            runtime_metadata = ((template.metadata or {}).get("runtime") or {})
            for downstream_slug in runtime_metadata.get("allowed_downstream_slugs") or []:
                if not isinstance(downstream_slug, str):
                    continue
                normalized = downstream_slug.strip()
                if not normalized or normalized in required or normalized not in templates_by_slug:
                    continue
                required.add(normalized)
                pending.append(normalized)

        return required

    def sync_seed_catalog_from_seed(self) -> dict[str, Any]:
        state = self._store.load()
        seeded_state = build_seed_state(self._settings)
        changes: list[dict[str, str]] = []
        seeded_workspace_template_slugs = self._workspace_seed_template_slugs(seeded_state.templates)

        current_presets_by_key = {item.key: item for item in state.instruction_presets}
        updated_presets: list[object] = []
        seeded_preset_keys = {item.key for item in seeded_state.instruction_presets}
        for preset in seeded_state.instruction_presets:
            current = current_presets_by_key.get(preset.key)
            if current is None:
                updated_presets.append(preset)
                changes.append({"kind": "instruction_preset", "key": preset.key, "action": "created"})
                continue
            merged = self._merge_seeded_record(current, preset)
            if self._record_changed(current, merged):
                changes.append({"kind": "instruction_preset", "key": preset.key, "action": "updated"})
            updated_presets.append(merged)
        updated_presets.extend(item for item in state.instruction_presets if item.key not in seeded_preset_keys)

        current_skills_by_key = {item.key: item for item in state.skills}
        updated_skills: list[AgentSkill] = []
        skill_id_by_seed_id: dict[str, str] = {}
        seeded_skill_keys = {item.key for item in seeded_state.skills}
        for skill in seeded_state.skills:
            current = current_skills_by_key.get(skill.key)
            if current is None:
                updated_skills.append(skill)
                skill_id_by_seed_id[skill.id] = skill.id
                changes.append({"kind": "skill", "key": skill.key, "action": "created"})
                continue
            merged = self._merge_seeded_record(current, skill)
            if self._record_changed(current, merged):
                changes.append({"kind": "skill", "key": skill.key, "action": "updated"})
            updated_skills.append(merged)
            skill_id_by_seed_id[skill.id] = merged.id
        updated_skills.extend(item for item in state.skills if item.key not in seeded_skill_keys)

        current_templates_by_slug = {item.slug: item for item in state.templates}
        updated_templates: list[AgentTemplate] = []
        template_id_by_seed_id: dict[str, str] = {}
        seeded_template_slugs = {item.slug for item in seeded_state.templates}
        for template in seeded_state.templates:
            current = current_templates_by_slug.get(template.slug)
            if current is None:
                updated_templates.append(template)
                template_id_by_seed_id[template.id] = template.id
                changes.append({"kind": "template", "key": template.slug, "action": "created"})
                continue
            merged = self._merge_seeded_record(current, template)
            if self._record_changed(current, merged):
                changes.append({"kind": "template", "key": template.slug, "action": "updated"})
            updated_templates.append(merged)
            template_id_by_seed_id[template.id] = merged.id
        updated_templates.extend(item for item in state.templates if item.slug not in seeded_template_slugs)

        updated_workspace_agents = list(state.workspace_agents)
        profiles_to_seed = {
            *(agent.profile for agent in state.workspace_agents),
            *(item.profile for item in state.workspace_ai_settings),
        }
        for profile_id in sorted(profiles_to_seed):
            profile_agents = {
                agent.slug: agent
                for agent in updated_workspace_agents
                if agent.profile == profile_id
            }
            for template in updated_templates:
                if template.slug not in seeded_workspace_template_slugs:
                    continue
                if profile_agents.get(template.slug) is not None:
                    continue
                agent = WorkspaceAgent(
                    profile=profile_id,
                    source_template_id=template.id,
                    origin="template",
                    visibility="workspace",
                    routing_policy="direct",
                    slug=template.slug,
                    name=template.name,
                    description=template.description,
                    protocol_version=template.protocol_version,
                    preferred_transport=template.preferred_transport,
                    url=template.url,
                    provider_organization=template.provider_organization,
                    provider_url=template.provider_url,
                    version=template.version,
                    documentation_url=template.documentation_url,
                    icon_url=template.icon_url,
                    additional_interfaces=deepcopy(template.additional_interfaces),
                    capabilities=deepcopy(template.capabilities),
                    security_schemes=deepcopy(template.security_schemes),
                    security=deepcopy(template.security),
                    supports_authenticated_extended_card=template.supports_authenticated_extended_card,
                    default_input_modes=deepcopy(template.default_input_modes),
                    default_output_modes=deepcopy(template.default_output_modes),
                    system_instruction=template.system_instruction,
                    developer_instruction=template.developer_instruction,
                    assistant_instruction=template.assistant_instruction,
                    llm_version=deepcopy(template.llm_version),
                    llm_temperature=template.llm_temperature,
                    max_reasoning_steps=template.max_reasoning_steps,
                    metadata=deepcopy(template.metadata),
                    is_enabled=True,
                    template_version_snapshot=template.version,
                )
                updated_workspace_agents.append(agent)
                profile_agents[agent.slug] = agent
                changes.append(
                    {
                        "kind": "workspace_agent",
                        "key": f"{profile_id}:{agent.slug}",
                        "action": "created",
                    }
                )

        current_tools_by_key = {item.key: item for item in state.tools}
        current_servers_by_server_id = {item.server_id: item for item in state.tool_servers}
        updated_tools: list[AgentTool] = []
        tool_id_by_seed_id: dict[str, str] = {}
        seeded_tool_keys = {item.key for item in seeded_state.tools}
        for tool in seeded_state.tools:
            translated_tool = tool.model_copy(deep=True)
            seeded_server = next((item for item in seeded_state.tool_servers if item.id == tool.tool_server_id), None)
            if seeded_server is not None:
                current_server = current_servers_by_server_id.get(seeded_server.server_id)
                if current_server is not None:
                    translated_tool.tool_server_id = current_server.id
            current = current_tools_by_key.get(tool.key)
            if current is None:
                updated_tools.append(translated_tool)
                tool_id_by_seed_id[tool.id] = translated_tool.id
                changes.append({"kind": "tool", "key": tool.key, "action": "created"})
                continue
            merged = self._merge_seeded_record(current, translated_tool)
            if self._record_changed(current, merged):
                changes.append({"kind": "tool", "key": tool.key, "action": "updated"})
            updated_tools.append(merged)
            tool_id_by_seed_id[tool.id] = merged.id
        updated_tools.extend(item for item in state.tools if item.key not in seeded_tool_keys)

        existing_template_skill_bindings = {
            (item.template_id, item.skill_id): item
            for item in state.template_skill_bindings
        }
        existing_template_tool_bindings = {
            (item.template_id, item.tool_id): item
            for item in state.template_tool_bindings
        }

        updated_template_skill_bindings = [
            item for item in state.template_skill_bindings if item.template_id not in set(template_id_by_seed_id.values())
        ]
        for binding in seeded_state.template_skill_bindings:
            template_id = template_id_by_seed_id.get(binding.template_id)
            skill_id = skill_id_by_seed_id.get(binding.skill_id)
            if not template_id or not skill_id:
                continue
            current = existing_template_skill_bindings.get((template_id, skill_id))
            replacement = AgentTemplateSkillBinding(
                id=current.id if current is not None else binding.id,
                template_id=template_id,
                skill_id=skill_id,
                order=binding.order,
                is_primary=binding.is_primary,
                metadata=deepcopy(binding.metadata),
                created_at=current.created_at if current is not None else binding.created_at,
                updated_at=utcnow(),
            )
            if current is None:
                changes.append({"kind": "template_skill_binding", "key": f"{template_id}:{skill_id}", "action": "created"})
            elif self._record_changed(current, replacement):
                changes.append({"kind": "template_skill_binding", "key": f"{template_id}:{skill_id}", "action": "updated"})
            updated_template_skill_bindings.append(replacement)

        updated_template_tool_bindings = [
            item for item in state.template_tool_bindings if item.template_id not in set(template_id_by_seed_id.values())
        ]
        template_tool_ids_by_template_id: dict[str, set[str]] = {}
        for binding in seeded_state.template_tool_bindings:
            template_id = template_id_by_seed_id.get(binding.template_id)
            tool_id = tool_id_by_seed_id.get(binding.tool_id)
            if not template_id or not tool_id:
                continue
            template_tool_ids_by_template_id.setdefault(template_id, set()).add(tool_id)
            current = existing_template_tool_bindings.get((template_id, tool_id))
            replacement = AgentTemplateToolBinding(
                id=current.id if current is not None else binding.id,
                template_id=template_id,
                tool_id=tool_id,
                order=binding.order,
                is_required=binding.is_required,
                tool_config=deepcopy(binding.tool_config),
                created_at=current.created_at if current is not None else binding.created_at,
                updated_at=utcnow(),
            )
            if current is None:
                changes.append({"kind": "template_tool_binding", "key": f"{template_id}:{tool_id}", "action": "created"})
            elif self._record_changed(current, replacement):
                changes.append({"kind": "template_tool_binding", "key": f"{template_id}:{tool_id}", "action": "updated"})
            updated_template_tool_bindings.append(replacement)

        workspace_agents_to_sync = [
            agent
            for agent in updated_workspace_agents
            if agent.origin == "template" and agent.source_template_id in set(template_id_by_seed_id.values())
        ]
        existing_workspace_skill_bindings = {
            (item.agent_id, item.skill_id): item
            for item in state.workspace_skill_bindings
        }
        existing_workspace_tool_bindings = {
            (item.agent_id, item.tool_id): item
            for item in state.workspace_tool_bindings
        }
        updated_workspace_skill_bindings = list(state.workspace_skill_bindings)
        updated_workspace_tool_bindings = list(state.workspace_tool_bindings)
        workspace_skill_index = {item.id: idx for idx, item in enumerate(updated_workspace_skill_bindings)}
        workspace_tool_index = {item.id: idx for idx, item in enumerate(updated_workspace_tool_bindings)}

        template_skill_bindings_by_template_id: dict[str, list[AgentTemplateSkillBinding]] = {}
        for binding in updated_template_skill_bindings:
            template_skill_bindings_by_template_id.setdefault(binding.template_id, []).append(binding)
        template_tool_bindings_by_template_id: dict[str, list[AgentTemplateToolBinding]] = {}
        for binding in updated_template_tool_bindings:
            template_tool_bindings_by_template_id.setdefault(binding.template_id, []).append(binding)

        for agent in workspace_agents_to_sync:
            for binding in template_skill_bindings_by_template_id.get(agent.source_template_id or "", []):
                current = existing_workspace_skill_bindings.get((agent.id, binding.skill_id))
                replacement = WorkspaceAgentSkillBinding(
                    id=current.id if current is not None else str(uuid4()),
                    agent_id=agent.id,
                    skill_id=binding.skill_id,
                    order=binding.order,
                    is_primary=binding.is_primary,
                    metadata=deepcopy(binding.metadata),
                    created_at=current.created_at if current is not None else utcnow(),
                    updated_at=utcnow(),
                )
                if current is None:
                    workspace_skill_index[replacement.id] = len(updated_workspace_skill_bindings)
                    updated_workspace_skill_bindings.append(replacement)
                    changes.append({"kind": "workspace_skill_binding", "key": f"{agent.slug}:{binding.skill_id}", "action": "created"})
                else:
                    if self._record_changed(current, replacement):
                        updated_workspace_skill_bindings[workspace_skill_index[current.id]] = replacement
                        changes.append({"kind": "workspace_skill_binding", "key": f"{agent.slug}:{binding.skill_id}", "action": "updated"})
            for binding in template_tool_bindings_by_template_id.get(agent.source_template_id or "", []):
                current = existing_workspace_tool_bindings.get((agent.id, binding.tool_id))
                replacement = WorkspaceAgentToolBinding(
                    id=current.id if current is not None else str(uuid4()),
                    agent_id=agent.id,
                    tool_id=binding.tool_id,
                    order=binding.order,
                    is_required=binding.is_required,
                    tool_config=deepcopy(binding.tool_config),
                    created_at=current.created_at if current is not None else utcnow(),
                    updated_at=utcnow(),
                )
                if current is None:
                    workspace_tool_index[replacement.id] = len(updated_workspace_tool_bindings)
                    updated_workspace_tool_bindings.append(replacement)
                    changes.append({"kind": "workspace_tool_binding", "key": f"{agent.slug}:{binding.tool_id}", "action": "created"})
                else:
                    if self._record_changed(current, replacement):
                        updated_workspace_tool_bindings[workspace_tool_index[current.id]] = replacement
                        changes.append({"kind": "workspace_tool_binding", "key": f"{agent.slug}:{binding.tool_id}", "action": "updated"})

        if not changes:
            return {"updated": 0, "changes": []}

        state.instruction_presets = updated_presets
        state.skills = updated_skills
        state.templates = updated_templates
        state.tools = updated_tools
        state.workspace_agents = updated_workspace_agents
        state.template_skill_bindings = updated_template_skill_bindings
        state.template_tool_bindings = updated_template_tool_bindings
        state.workspace_skill_bindings = updated_workspace_skill_bindings
        state.workspace_tool_bindings = updated_workspace_tool_bindings
        self._cache_clear()
        self._store.save(state)
        return {"updated": len(changes), "changes": changes}

    def _load_state(self) -> AgentControlPlaneState:
        return self.ensure_seeded()

    def _save_state(self, state: AgentControlPlaneState) -> AgentControlPlaneState:
        self._cache_clear()
        return self._store.save(state)

    def replace_state(self, state: AgentControlPlaneState) -> AgentControlPlaneState:
        self._cache_clear()
        return self._save_state(state)

    def _list_records(
        self,
        field_name: str,
        *,
        ids: list[str] | None = None,
        filters: dict[str, object] | None = None,
    ) -> list[object]:
        records = self._store.list_records(field_name, ids=ids, filters=filters)
        if field_name == "templates" and not records:
            self.ensure_seeded()
            records = self._store.list_records(field_name, ids=ids, filters=filters)
        return records

    def _get_record(
        self,
        field_name: str,
        *,
        record_id: str | None = None,
        filters: dict[str, object] | None = None,
    ) -> object | None:
        record = self._store.get_record(field_name, record_id=record_id, filters=filters)
        if field_name == "templates" and record is None:
            self.ensure_seeded()
            record = self._store.get_record(field_name, record_id=record_id, filters=filters)
        return record

    def _upsert_record(self, field_name: str, record: object) -> object:
        return self._store.upsert_record(field_name, record)

    def _upsert_records(self, field_name: str, records: list[object]) -> list[object]:
        return self._store.upsert_records(field_name, records)

    def _delete_records(
        self,
        field_name: str,
        *,
        ids: list[str] | None = None,
        filters: dict[str, object] | None = None,
    ) -> int:
        return self._store.delete_records(field_name, ids=ids, filters=filters)

    def _list_model_version_records(self) -> list[ModelVersionOption]:
        return list(self._list_records("model_versions"))  # type: ignore[return-value]

    def _list_tool_server_records(self, *, ids: list[str] | None = None) -> list[ToolServer]:
        return list(self._list_records("tool_servers", ids=ids))  # type: ignore[return-value]

    def _list_tool_records(self, *, ids: list[str] | None = None) -> list[AgentTool]:
        return list(self._list_records("tools", ids=ids))  # type: ignore[return-value]

    def _list_skill_records(self, *, ids: list[str] | None = None) -> list[AgentSkill]:
        return list(self._list_records("skills", ids=ids))  # type: ignore[return-value]

    def _list_workspace_tool_connection_records(
        self,
        *,
        ids: list[str] | None = None,
        profile_id: str | None = None,
        tool_server_ids: list[str] | None = None,
    ) -> list[WorkspaceToolConnection]:
        filters: dict[str, Any] = {}
        if profile_id is not None:
            filters["profile"] = profile_id
        if tool_server_ids is not None:
            filters["tool_server_id"] = tool_server_ids
        return list(self._list_records("workspace_tool_connections", ids=ids, filters=filters or None))  # type: ignore[return-value]

    def _list_instruction_preset_records(self) -> list[object]:
        return self._list_records("instruction_presets")

    def _list_template_records(
        self,
        *,
        ids: list[str] | None = None,
        filters: dict[str, object] | None = None,
    ) -> list[AgentTemplate]:
        return list(self._list_records("templates", ids=ids, filters=filters))  # type: ignore[return-value]

    def _get_workspace_ai_settings_record(self, *, profile_id: str) -> WorkspaceAiSettings | None:
        return self._get_record("workspace_ai_settings", filters={"profile": profile_id})  # type: ignore[return-value]

    def _list_workspace_agent_records(
        self,
        *,
        profile_id: str | None = None,
        ids: list[str] | None = None,
        enabled_only: bool | None = None,
    ) -> list[WorkspaceAgent]:
        filters: dict[str, object] = {}
        if profile_id is not None:
            filters["profile"] = profile_id
        if enabled_only is not None:
            filters["is_enabled"] = enabled_only
        return list(self._list_records("workspace_agents", ids=ids, filters=filters or None))  # type: ignore[return-value]

    def _get_workspace_agent_record(
        self,
        *,
        profile_id: str,
        slug: str | None = None,
        agent_id: str | None = None,
        enabled_only: bool | None = None,
    ) -> WorkspaceAgent | None:
        filters: dict[str, object] = {"profile": profile_id}
        if slug is not None:
            filters["slug"] = slug
        if enabled_only is not None:
            filters["is_enabled"] = enabled_only
        return self._get_record("workspace_agents", record_id=agent_id, filters=filters)  # type: ignore[return-value]

    def _list_template_skill_binding_records(
        self,
        *,
        template_ids: list[str] | None = None,
    ) -> list[AgentTemplateSkillBinding]:
        filters = {"template_id": template_ids} if template_ids is not None else None
        return list(self._list_records("template_skill_bindings", filters=filters))  # type: ignore[return-value]

    def _list_template_tool_binding_records(
        self,
        *,
        template_ids: list[str] | None = None,
    ) -> list[AgentTemplateToolBinding]:
        filters = {"template_id": template_ids} if template_ids is not None else None
        return list(self._list_records("template_tool_bindings", filters=filters))  # type: ignore[return-value]

    def _list_workspace_skill_binding_records(
        self,
        *,
        agent_ids: list[str] | None = None,
    ) -> list[WorkspaceAgentSkillBinding]:
        filters = {"agent_id": agent_ids} if agent_ids is not None else None
        return list(self._list_records("workspace_skill_bindings", filters=filters))  # type: ignore[return-value]

    def _list_workspace_tool_binding_records(
        self,
        *,
        agent_ids: list[str] | None = None,
    ) -> list[WorkspaceAgentToolBinding]:
        filters = {"agent_id": agent_ids} if agent_ids is not None else None
        return list(self._list_records("workspace_tool_bindings", filters=filters))  # type: ignore[return-value]

    def _tool_servers_by_id(self, state: AgentControlPlaneState) -> dict[str, ToolServer]:
        return {item.id: item for item in state.tool_servers}

    def _tools_by_id(self, state: AgentControlPlaneState) -> dict[str, AgentTool]:
        return {item.id: item for item in state.tools}

    def _skills_by_id(self, state: AgentControlPlaneState) -> dict[str, AgentSkill]:
        return {item.id: item for item in state.skills}

    def _templates_by_id(self, state: AgentControlPlaneState) -> dict[str, AgentTemplate]:
        return {item.id: item for item in state.templates}

    def _agents_by_id(self, state: AgentControlPlaneState) -> dict[str, WorkspaceAgent]:
        return {item.id: item for item in state.workspace_agents}

    def _workspace_agents(self, state: AgentControlPlaneState, *, profile_id: str) -> list[WorkspaceAgent]:
        return [item for item in state.workspace_agents if item.profile == profile_id]

    def _workspace_tool_connections(self, state: AgentControlPlaneState, *, profile_id: str) -> list[WorkspaceToolConnection]:
        return [item for item in state.workspace_tool_connections if item.profile == profile_id]

    def _workspace_ai_settings(self, state: AgentControlPlaneState, *, profile_id: str) -> WorkspaceAiSettings | None:
        return next((item for item in state.workspace_ai_settings if item.profile == profile_id), None)

    def _template_skill_bindings(self, state: AgentControlPlaneState, template_id: str) -> list[AgentTemplateSkillBinding]:
        return sorted(
            [item for item in state.template_skill_bindings if item.template_id == template_id],
            key=lambda item: (item.order, item.created_at),
        )

    def _template_tool_bindings(self, state: AgentControlPlaneState, template_id: str) -> list[AgentTemplateToolBinding]:
        return sorted(
            [item for item in state.template_tool_bindings if item.template_id == template_id],
            key=lambda item: (item.order, item.created_at),
        )

    def _workspace_skill_bindings(self, state: AgentControlPlaneState, agent_id: str) -> list[WorkspaceAgentSkillBinding]:
        return sorted(
            [item for item in state.workspace_skill_bindings if item.agent_id == agent_id],
            key=lambda item: (item.order, item.created_at),
        )

    def _workspace_tool_bindings(self, state: AgentControlPlaneState, agent_id: str) -> list[WorkspaceAgentToolBinding]:
        return sorted(
            [item for item in state.workspace_tool_bindings if item.agent_id == agent_id],
            key=lambda item: (item.order, item.created_at),
        )

    def _tool_payload(self, tool: AgentTool, tool_server: ToolServer | None, *, is_required: bool) -> dict[str, Any]:
        return {
            "key": tool.key,
            "name": tool.full_tool_name(tool_server),
            "displayName": tool.display_name,
            "description": tool.description,
            "required": is_required,
            "toolServerId": tool_server.server_id if tool_server else None,
        }

    def _group_bindings_by(self, items: list[object], attr_name: str) -> dict[str, list[object]]:
        grouped: dict[str, list[object]] = {}
        for item in items:
            grouped.setdefault(getattr(item, attr_name), []).append(item)
        return grouped

    def _template_to_read_from_records(
        self,
        template: AgentTemplate,
        *,
        skills_by_id: dict[str, AgentSkill],
        tools_by_id: dict[str, AgentTool],
        servers_by_id: dict[str, ToolServer],
        skill_bindings_by_template_id: dict[str, list[AgentTemplateSkillBinding]],
        tool_bindings_by_template_id: dict[str, list[AgentTemplateToolBinding]],
    ) -> dict[str, Any]:
        skill_bindings = []
        tool_bindings = []
        ordered_skills: list[AgentSkill] = []
        tool_payload: list[dict[str, Any]] = []

        for binding in sorted(skill_bindings_by_template_id.get(template.id, []), key=lambda item: (item.order, item.created_at)):
            skill = skills_by_id.get(binding.skill_id)
            if skill is None:
                continue
            ordered_skills.append(skill)
            skill_bindings.append(
                {
                    "id": binding.id,
                    "order": binding.order,
                    "is_primary": binding.is_primary,
                    "metadata": deepcopy(binding.metadata),
                    "skill": skill.model_dump(mode="json"),
                }
            )

        for binding in sorted(tool_bindings_by_template_id.get(template.id, []), key=lambda item: (item.order, item.created_at)):
            tool = tools_by_id.get(binding.tool_id)
            if tool is None:
                continue
            server = servers_by_id.get(tool.tool_server_id or "")
            full_tool_name = tool.full_tool_name(server)
            tool_payload.append(self._tool_payload(tool, server, is_required=binding.is_required))
            tool_bindings.append(
                {
                    "id": binding.id,
                    "order": binding.order,
                    "is_required": binding.is_required,
                    "tool_config": deepcopy(binding.tool_config),
                    "tool": {
                        **tool.model_dump(mode="json"),
                        "full_tool_name": full_tool_name,
                        "tool_server": server.model_dump(mode="json") if server else None,
                    },
                }
            )

        payload = template.model_dump(mode="json")
        payload["skill_bindings"] = skill_bindings
        payload["tool_bindings"] = tool_bindings
        payload["card_payload"] = template.build_agent_card_payload(
            skills=ordered_skills,
            tool_payload=tool_payload,
        )
        return payload

    def _workspace_agent_to_read_from_records(
        self,
        agent: WorkspaceAgent,
        *,
        skills_by_id: dict[str, AgentSkill],
        tools_by_id: dict[str, AgentTool],
        servers_by_id: dict[str, ToolServer],
        templates_by_id: dict[str, AgentTemplate],
        template_skill_bindings_by_template_id: dict[str, list[AgentTemplateSkillBinding]],
        template_tool_bindings_by_template_id: dict[str, list[AgentTemplateToolBinding]],
        skill_bindings_by_agent_id: dict[str, list[WorkspaceAgentSkillBinding]],
        tool_bindings_by_agent_id: dict[str, list[WorkspaceAgentToolBinding]],
    ) -> dict[str, Any]:
        skill_bindings = []
        tool_bindings = []
        ordered_skills: list[AgentSkill] = []
        tool_payload: list[dict[str, Any]] = []

        for binding in sorted(skill_bindings_by_agent_id.get(agent.id, []), key=lambda item: (item.order, item.created_at)):
            skill = skills_by_id.get(binding.skill_id)
            if skill is None:
                continue
            ordered_skills.append(skill)
            skill_bindings.append(
                {
                    "id": binding.id,
                    "order": binding.order,
                    "is_primary": binding.is_primary,
                    "metadata": deepcopy(binding.metadata),
                    "skill": skill.model_dump(mode="json"),
                }
            )

        for binding in sorted(tool_bindings_by_agent_id.get(agent.id, []), key=lambda item: (item.order, item.created_at)):
            tool = tools_by_id.get(binding.tool_id)
            if tool is None:
                continue
            server = servers_by_id.get(tool.tool_server_id or "")
            full_tool_name = tool.full_tool_name(server)
            tool_payload.append(self._tool_payload(tool, server, is_required=binding.is_required))
            tool_bindings.append(
                {
                    "id": binding.id,
                    "order": binding.order,
                    "is_required": binding.is_required,
                    "tool_config": deepcopy(binding.tool_config),
                    "tool": {
                        **tool.model_dump(mode="json"),
                        "full_tool_name": full_tool_name,
                        "tool_server": server.model_dump(mode="json") if server else None,
                    },
                }
            )

        payload = agent.model_dump(mode="json")
        source_template = templates_by_id.get(agent.source_template_id or "")
        payload["source_template"] = (
            self._template_to_read_from_records(
                source_template,
                skills_by_id=skills_by_id,
                tools_by_id=tools_by_id,
                servers_by_id=servers_by_id,
                skill_bindings_by_template_id=template_skill_bindings_by_template_id,
                tool_bindings_by_template_id=template_tool_bindings_by_template_id,
            )
            if source_template is not None
            else None
        )
        payload["llm_version"] = _serialize_model_version(agent.llm_version)
        payload["skill_bindings"] = skill_bindings
        payload["tool_bindings"] = tool_bindings
        payload["card_payload"] = agent.build_agent_card_payload(
            skills=ordered_skills,
            tool_payload=tool_payload,
        )
        return payload

    def _template_to_read(self, state: AgentControlPlaneState, template: AgentTemplate) -> dict[str, Any]:
        tools_by_id = self._tools_by_id(state)
        skills_by_id = self._skills_by_id(state)
        servers_by_id = self._tool_servers_by_id(state)
        skill_bindings = []
        tool_bindings = []
        ordered_skills: list[AgentSkill] = []
        tool_payload: list[dict[str, Any]] = []

        for binding in self._template_skill_bindings(state, template.id):
            skill = skills_by_id.get(binding.skill_id)
            if skill is None:
                continue
            ordered_skills.append(skill)
            skill_bindings.append(
                {
                    "id": binding.id,
                    "order": binding.order,
                    "is_primary": binding.is_primary,
                    "metadata": deepcopy(binding.metadata),
                    "skill": skill.model_dump(mode="json"),
                }
            )

        for binding in self._template_tool_bindings(state, template.id):
            tool = tools_by_id.get(binding.tool_id)
            if tool is None:
                continue
            server = servers_by_id.get(tool.tool_server_id or "")
            full_tool_name = tool.full_tool_name(server)
            tool_payload.append(self._tool_payload(tool, server, is_required=binding.is_required))
            tool_bindings.append(
                {
                    "id": binding.id,
                    "order": binding.order,
                    "is_required": binding.is_required,
                    "tool_config": deepcopy(binding.tool_config),
                    "tool": {
                        **tool.model_dump(mode="json"),
                        "full_tool_name": full_tool_name,
                        "tool_server": server.model_dump(mode="json") if server else None,
                    },
                }
            )

        payload = template.model_dump(mode="json")
        payload["skill_bindings"] = skill_bindings
        payload["tool_bindings"] = tool_bindings
        payload["card_payload"] = template.build_agent_card_payload(
            skills=ordered_skills,
            tool_payload=tool_payload,
        )
        return payload

    def _workspace_agent_to_read(self, state: AgentControlPlaneState, agent: WorkspaceAgent) -> dict[str, Any]:
        tools_by_id = self._tools_by_id(state)
        skills_by_id = self._skills_by_id(state)
        servers_by_id = self._tool_servers_by_id(state)
        templates_by_id = self._templates_by_id(state)
        skill_bindings = []
        tool_bindings = []
        ordered_skills: list[AgentSkill] = []
        tool_payload: list[dict[str, Any]] = []

        for binding in self._workspace_skill_bindings(state, agent.id):
            skill = skills_by_id.get(binding.skill_id)
            if skill is None:
                continue
            ordered_skills.append(skill)
            skill_bindings.append(
                {
                    "id": binding.id,
                    "order": binding.order,
                    "is_primary": binding.is_primary,
                    "metadata": deepcopy(binding.metadata),
                    "skill": skill.model_dump(mode="json"),
                }
            )

        for binding in self._workspace_tool_bindings(state, agent.id):
            tool = tools_by_id.get(binding.tool_id)
            if tool is None:
                continue
            server = servers_by_id.get(tool.tool_server_id or "")
            full_tool_name = tool.full_tool_name(server)
            tool_payload.append(self._tool_payload(tool, server, is_required=binding.is_required))
            tool_bindings.append(
                {
                    "id": binding.id,
                    "order": binding.order,
                    "is_required": binding.is_required,
                    "tool_config": deepcopy(binding.tool_config),
                    "tool": {
                        **tool.model_dump(mode="json"),
                        "full_tool_name": full_tool_name,
                        "tool_server": server.model_dump(mode="json") if server else None,
                    },
                }
            )

        payload = agent.model_dump(mode="json")
        payload["source_template"] = (
            self._template_to_read(state, templates_by_id[agent.source_template_id])
            if agent.source_template_id and agent.source_template_id in templates_by_id
            else None
        )
        payload["llm_version"] = _serialize_model_version(agent.llm_version)
        payload["skill_bindings"] = skill_bindings
        payload["tool_bindings"] = tool_bindings
        payload["card_payload"] = agent.build_agent_card_payload(
            skills=ordered_skills,
            tool_payload=tool_payload,
        )
        return payload

    def list_templates(self) -> list[dict[str, Any]]:
        templates = [
            template
            for template in self._list_template_records(filters={"is_active": True, "allow_workspace_installs": True})
        ]
        templates = sorted(templates, key=lambda item: (item.sort_order, item.name.lower()))
        template_ids = [item.id for item in templates]
        skill_bindings = self._list_template_skill_binding_records(template_ids=template_ids)
        tool_bindings = self._list_template_tool_binding_records(template_ids=template_ids)
        skill_ids = sorted({item.skill_id for item in skill_bindings})
        tool_ids = sorted({item.tool_id for item in tool_bindings})
        tools = self._list_tool_records(ids=tool_ids)
        tool_servers = self._list_tool_server_records(ids=sorted({item.tool_server_id for item in tools if item.tool_server_id}))
        skills = self._list_skill_records(ids=skill_ids)
        return [
            self._template_to_read_from_records(
                template,
                skills_by_id={item.id: item for item in skills},
                tools_by_id={item.id: item for item in tools},
                servers_by_id={item.id: item for item in tool_servers},
                skill_bindings_by_template_id=self._group_bindings_by(skill_bindings, "template_id"),  # type: ignore[arg-type]
                tool_bindings_by_template_id=self._group_bindings_by(tool_bindings, "template_id"),  # type: ignore[arg-type]
            )
            for template in templates
        ]

    def list_tools(self) -> list[dict[str, Any]]:
        tools = sorted(self._list_tool_records(), key=lambda item: item.display_name.lower())
        servers_by_id = {item.id: item for item in self._list_tool_server_records(ids=sorted({item.tool_server_id for item in tools if item.tool_server_id}))}
        payload = []
        for tool in tools:
            server = servers_by_id.get(tool.tool_server_id or "")
            payload.append(
                {
                    **tool.model_dump(mode="json"),
                    "full_tool_name": tool.full_tool_name(server),
                    "tool_server": server.model_dump(mode="json") if server else None,
                }
            )
        return payload

    def list_skills(self) -> list[dict[str, Any]]:
        return [skill.model_dump(mode="json") for skill in sorted(self._list_skill_records(), key=lambda item: item.name.lower())]

    def list_instruction_presets(self) -> list[dict[str, Any]]:
        presets = list(self._list_instruction_preset_records())
        return [item.model_dump(mode="json") for item in sorted(presets, key=lambda item: item.title.lower())]

    def list_model_versions(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self._list_model_version_records()]

    def _visible_tool_server_records(self, *, profile_id: str) -> list[ToolServer]:
        return [
            item
            for item in self._list_tool_server_records()
            if item.is_active and (item.scope == "platform" or (item.scope == "workspace" and item.profile == profile_id))
        ]

    def _get_visible_tool_server_record(self, *, profile_id: str, tool_server_id: str) -> ToolServer | None:
        return next((item for item in self._visible_tool_server_records(profile_id=profile_id) if item.id == tool_server_id), None)

    def _workspace_tool_connection_to_read(
        self,
        connection: WorkspaceToolConnection,
        *,
        servers_by_id: dict[str, ToolServer],
    ) -> dict[str, Any]:
        payload = connection.model_dump(mode="json")
        payload["tool_server"] = None
        server = servers_by_id.get(connection.tool_server_id)
        if server is not None:
            payload["tool_server"] = server.model_dump(mode="json")
        payload["owner_user_id"] = connection.owner_user
        payload["has_credential_payload"] = bool(connection.credential_payload_encrypted)
        payload["has_access_token"] = bool(connection.access_token_encrypted)
        payload["has_refresh_token"] = bool(connection.refresh_token_encrypted)
        for key in (
            "tool_server_id",
            "owner_user",
            "credential_payload_encrypted",
            "access_token_encrypted",
            "refresh_token_encrypted",
            "created_by",
            "updated_by",
        ):
            payload.pop(key, None)
        return payload

    def _assert_unique_workspace_tool_connection_slug(
        self,
        *,
        profile_id: str,
        slug: str,
        exclude_connection_id: str | None = None,
    ) -> None:
        for connection in self._list_workspace_tool_connection_records(profile_id=profile_id):
            if connection.slug == slug and connection.id != exclude_connection_id:
                raise AgentControlPlaneError("A workspace tool connection with this slug already exists.")

    def _validate_workspace_tool_connection(self, connection: WorkspaceToolConnection) -> None:
        if connection.connection_scope == "user" and not connection.owner_user:
            raise AgentControlPlaneError("User-scoped tool connections require an owner user.")
        if connection.connection_scope == "workspace" and connection.owner_user:
            raise AgentControlPlaneError("Workspace-scoped tool connections cannot have an owner user.")

    def list_tool_servers(self, *, profile_id: str) -> list[dict[str, Any]]:
        self.ensure_seeded()
        cache_key = ("list_tool_servers", profile_id)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        servers = sorted(self._visible_tool_server_records(profile_id=profile_id), key=lambda item: (item.scope, item.name.lower()))
        return self._cache_set(cache_key, [item.model_dump(mode="json") for item in servers])

    def list_workspace_tool_connections(self, *, profile_id: str) -> list[dict[str, Any]]:
        cache_key = ("list_workspace_tool_connections", profile_id)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        connections = sorted(
            self._list_workspace_tool_connection_records(profile_id=profile_id),
            key=lambda item: item.name.lower(),
        )
        servers_by_id = {
            item.id: item
            for item in self._visible_tool_server_records(profile_id=profile_id)
        }
        return self._cache_set(
            cache_key,
            [self._workspace_tool_connection_to_read(item, servers_by_id=servers_by_id) for item in connections],
        )

    def create_workspace_tool_connection(self, *, profile_id: str, user_id: str, data: dict[str, Any]) -> dict[str, Any]:
        tool_server_id = str(data.get("tool_server") or "").strip()
        name = str(data.get("name") or "").strip()
        slug = str(data.get("slug") or "").strip()
        if not tool_server_id:
            raise AgentControlPlaneError("tool_server is required.")
        if not name:
            raise AgentControlPlaneError("name is required.")
        if not slug:
            raise AgentControlPlaneError("slug is required.")
        tool_server = self._get_visible_tool_server_record(profile_id=profile_id, tool_server_id=tool_server_id)
        if tool_server is None:
            raise AgentControlPlaneError("Tool server was not found.")
        self._assert_unique_workspace_tool_connection_slug(profile_id=profile_id, slug=slug)

        connection = WorkspaceToolConnection(
            profile=profile_id,
            tool_server_id=tool_server.id,
            name=name,
            slug=slug,
            connection_scope=str(data.get("connection_scope") or "workspace"),
            owner_user=str(data.get("owner_user") or "").strip() or None,
            auth_type=str(data.get("auth_type") or "").strip(),
            server_url_override=str(data.get("server_url_override") or "").strip(),
            credential_payload_encrypted=_encrypt_json_payload(data.get("credential_payload")),
            access_token_encrypted=_encrypt_secret(str(data.get("access_token") or "")),
            refresh_token_encrypted=_encrypt_secret(str(data.get("refresh_token") or "")),
            token_expires_at=data.get("token_expires_at"),
            granted_scopes=deepcopy(data.get("granted_scopes") or []),
            resource_owner_id=str(data.get("resource_owner_id") or "").strip(),
            resource_label=str(data.get("resource_label") or "").strip(),
            status=str(data.get("status") or "pending"),
            last_tested_at=data.get("last_tested_at"),
            last_error=str(data.get("last_error") or ""),
            metadata=deepcopy(data.get("metadata") or {}),
            created_by=user_id,
            updated_by=user_id,
        )
        self._validate_workspace_tool_connection(connection)
        self._upsert_record("workspace_tool_connections", connection)
        self._cache_clear()
        return self._workspace_tool_connection_to_read(connection, servers_by_id={tool_server.id: tool_server})

    def update_workspace_tool_connection(
        self,
        *,
        profile_id: str,
        connection_id: str,
        user_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        connection = next(
            (item for item in self._list_workspace_tool_connection_records(profile_id=profile_id, ids=[connection_id]) if item.id == connection_id),
            None,
        )
        if connection is None:
            raise AgentControlPlaneError("Workspace tool connection was not found.")

        if "tool_server" in data:
            tool_server_id = str(data.get("tool_server") or "").strip()
            tool_server = self._get_visible_tool_server_record(profile_id=profile_id, tool_server_id=tool_server_id)
            if tool_server is None:
                raise AgentControlPlaneError("Tool server was not found.")
            connection.tool_server_id = tool_server.id
        if "name" in data:
            connection.name = str(data.get("name") or "").strip()
            if not connection.name:
                raise AgentControlPlaneError("name cannot be blank.")
        if "slug" in data:
            connection.slug = str(data.get("slug") or "").strip()
            if not connection.slug:
                raise AgentControlPlaneError("slug cannot be blank.")
            self._assert_unique_workspace_tool_connection_slug(
                profile_id=profile_id,
                slug=connection.slug,
                exclude_connection_id=connection.id,
            )
        if "connection_scope" in data:
            connection.connection_scope = str(data.get("connection_scope") or "workspace")
        if "owner_user" in data:
            connection.owner_user = str(data.get("owner_user") or "").strip() or None
        if "auth_type" in data:
            connection.auth_type = str(data.get("auth_type") or "").strip()
        if "server_url_override" in data:
            connection.server_url_override = str(data.get("server_url_override") or "").strip()
        if "credential_payload" in data:
            connection.credential_payload_encrypted = _encrypt_json_payload(data.get("credential_payload"))
        if "access_token" in data:
            connection.access_token_encrypted = _encrypt_secret(str(data.get("access_token") or ""))
        if "refresh_token" in data:
            connection.refresh_token_encrypted = _encrypt_secret(str(data.get("refresh_token") or ""))
        if "token_expires_at" in data:
            connection.token_expires_at = data.get("token_expires_at")
        if "granted_scopes" in data:
            connection.granted_scopes = deepcopy(data.get("granted_scopes") or [])
        if "resource_owner_id" in data:
            connection.resource_owner_id = str(data.get("resource_owner_id") or "").strip()
        if "resource_label" in data:
            connection.resource_label = str(data.get("resource_label") or "").strip()
        if "status" in data:
            connection.status = str(data.get("status") or "pending")
        if "last_tested_at" in data:
            connection.last_tested_at = data.get("last_tested_at")
        if "last_error" in data:
            connection.last_error = str(data.get("last_error") or "")
        if "metadata" in data:
            connection.metadata = deepcopy(data.get("metadata") or {})
        connection.updated_by = user_id
        connection.updated_at = utcnow()
        self._validate_workspace_tool_connection(connection)
        self._upsert_record("workspace_tool_connections", connection)
        self._cache_clear()
        visible_servers = self._visible_tool_server_records(profile_id=profile_id)
        servers_by_id = {item.id: item for item in visible_servers}
        return self._workspace_tool_connection_to_read(connection, servers_by_id=servers_by_id)

    def delete_workspace_tool_connection(self, *, profile_id: str, connection_id: str) -> None:
        connection = next(
            (item for item in self._list_workspace_tool_connection_records(profile_id=profile_id, ids=[connection_id]) if item.id == connection_id),
            None,
        )
        if connection is None:
            raise AgentControlPlaneError("Workspace tool connection was not found.")
        self._delete_records("workspace_tool_connections", ids=[connection_id], filters={"profile": profile_id})
        self._cache_clear()

    def test_workspace_tool_connection(self, *, profile_id: str, connection_id: str, user_id: str) -> dict[str, Any]:
        connection = next(
            (item for item in self._list_workspace_tool_connection_records(profile_id=profile_id, ids=[connection_id]) if item.id == connection_id),
            None,
        )
        if connection is None:
            raise AgentControlPlaneError("Workspace tool connection was not found.")
        tool_server = self._get_visible_tool_server_record(profile_id=profile_id, tool_server_id=connection.tool_server_id)
        if tool_server is None:
            raise AgentControlPlaneError("Tool server was not found.")
        server_url = str(connection.server_url_override or tool_server.server_url or "").strip()
        if not server_url:
            raise AgentControlPlaneError("No MCP server URL is configured for this connection.")

        headers = _connection_runtime_headers(connection)
        try:
            result = asyncio.run(
                _probe_mcp_server(
                    server_url=server_url,
                    headers=headers,
                    timeout_s=15.0,
                )
            )
        except Exception as exc:
            connection.status = "error"
            connection.last_tested_at = utcnow()
            connection.last_error = str(exc)
            connection.updated_by = user_id
            connection.updated_at = utcnow()
            self._upsert_record("workspace_tool_connections", connection)
            self._cache_clear()
            return {
                "ok": False,
                "server_url": server_url,
                "header_names": sorted(headers.keys()),
                "detail": str(exc),
            }

        connection.status = "connected"
        connection.last_tested_at = utcnow()
        connection.last_error = ""
        connection.updated_by = user_id
        connection.updated_at = utcnow()
        self._upsert_record("workspace_tool_connections", connection)
        self._cache_clear()
        return {
            "ok": True,
            "server_url": server_url,
            "header_names": sorted(headers.keys()),
            **result,
        }

    def get_workspace_ai_setup(self, *, profile_id: str) -> dict[str, Any]:
        ai = self._get_workspace_ai_settings_record(profile_id=profile_id)
        model_versions = self.list_model_versions()
        if ai is None:
            return {
                "configured": False,
                "agent": None,
                "available_versions": model_versions,
            }
        version = next((item for item in self._list_model_version_records() if item.id == ai.version), None)
        has_api_key = _has_usable_secret(ai.api_key)
        has_tavily_api_key = _has_usable_secret(ai.tavily_api_key)
        return {
            "configured": True,
            "agent": {
                "id": ai.id,
                "profile": ai.profile,
                "name": ai.name,
                "version": ai.version,
                "provider": version.provider if version else "",
                "provider_label": version.provider_label if version else "",
                "model_name": version.model_name if version else "",
                "provider_base_url": version.base_url if version else None,
                "effective_base_url": ai.base_url or (version.base_url if version else None),
                "special_instruction": ai.special_instruction,
                "system_instruction": ai.system_instruction,
                "assistant_instruction": ai.assistant_instruction,
                "has_api_key": has_api_key,
                "has_tavily_api_key": has_tavily_api_key,
                "api_key_masked": self._mask_secret(_decrypt_secret(ai.api_key)) if has_api_key else "",
                "tavily_api_key_masked": self._mask_secret(_decrypt_secret(ai.tavily_api_key)) if has_tavily_api_key else "",
                "api_key_requires_reconfiguration": bool(ai.api_key) and not has_api_key,
                "tavily_api_key_requires_reconfiguration": bool(ai.tavily_api_key) and not has_tavily_api_key,
            },
            "available_versions": model_versions,
        }

    def get_workspace_ai_runtime_setup(self, *, profile_id: str) -> dict[str, Any]:
        cache_key = ("get_workspace_ai_runtime_setup", profile_id)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        ai = self._get_workspace_ai_settings_record(profile_id=profile_id)
        if ai is None:
            return self._cache_set(cache_key, {
                "configured": False,
                "agent": None,
                "available_versions": [],
            })

        version = next((item for item in self._list_model_version_records() if item.id == ai.version), None)
        provider = version.provider if version else ""
        provider_label = version.provider_label if version else ""
        model_name = version.model_name if version else ai.version
        effective_base_url = ai.base_url or (version.base_url if version else "")
        api_key = _decrypt_secret(ai.api_key)
        if not api_key or api_key.startswith("gAAAA"):
            api_key = ""
        tavily_api_key = _decrypt_secret(ai.tavily_api_key)
        if not tavily_api_key or tavily_api_key.startswith("gAAAA"):
            tavily_api_key = ""
        host_agent = self._get_workspace_agent_record(profile_id=profile_id, slug="host", enabled_only=True)
        host_runtime_name = host_agent.build_runtime_name() if host_agent is not None else ""

        return self._cache_set(cache_key, {
            "configured": True,
            "agent": {
                "id": ai.id,
                "profile": ai.profile,
                "name": ai.name,
                "version": ai.version,
                "provider": provider,
                "provider_label": provider_label,
                "model_name": model_name,
                "provider_base_url": version.base_url if version else None,
                "effective_base_url": effective_base_url or None,
                "special_instruction": ai.special_instruction,
                "system_instruction": ai.system_instruction,
                "assistant_instruction": ai.assistant_instruction,
                "host_runtime_name": host_runtime_name,
                "api_key": api_key,
                "tavily_api_key": tavily_api_key,
                "has_api_key": bool(api_key),
                "has_tavily_api_key": bool(tavily_api_key),
            },
            "available_versions": [],
        })

    def warm_workspace_ai_runtime_setup_cache(self, *, profile_ids: list[str]) -> dict[str, int]:
        warmed = 0
        failed = 0
        for profile_id in profile_ids:
            normalized_profile_id = str(profile_id or "").strip()
            if not normalized_profile_id:
                continue
            try:
                self.get_workspace_ai_runtime_setup(profile_id=normalized_profile_id)
                warmed += 1
            except Exception:
                failed += 1
        return {"warmed": warmed, "failed": failed}

    def save_workspace_ai_setup(self, *, profile_id: str, data: dict[str, Any]) -> dict[str, Any]:
        ai = self._get_workspace_ai_settings_record(profile_id=profile_id)
        if ai is None:
            required = [name for name in ("name", "version", "api_key", "tavily_api_key") if not str(data.get(name) or "").strip()]
            if required:
                raise AgentControlPlaneError(", ".join(required) + " is required.")
            ai = WorkspaceAiSettings(
                profile=profile_id,
                name=str(data.get("name") or "").strip(),
                version=str(data.get("version") or "").strip(),
                special_instruction=str(data.get("special_instruction") or ""),
                system_instruction=str(data.get("system_instruction") or ""),
                assistant_instruction=str(data.get("assistant_instruction") or ""),
                api_key=_encrypt_secret(str(data.get("api_key") or "").strip()),
                tavily_api_key=_encrypt_secret(str(data.get("tavily_api_key") or "").strip()),
            )
        else:
            if "name" in data and str(data.get("name") or "").strip():
                ai.name = str(data.get("name") or "").strip()
            if "version" in data and str(data.get("version") or "").strip():
                ai.version = str(data.get("version") or "").strip()
            if "special_instruction" in data:
                ai.special_instruction = str(data.get("special_instruction") or "")
            if "system_instruction" in data:
                ai.system_instruction = str(data.get("system_instruction") or "")
            if "assistant_instruction" in data:
                ai.assistant_instruction = str(data.get("assistant_instruction") or "")
            if str(data.get("api_key") or "").strip():
                ai.api_key = _encrypt_secret(str(data.get("api_key") or "").strip())
            if str(data.get("tavily_api_key") or "").strip():
                ai.tavily_api_key = _encrypt_secret(str(data.get("tavily_api_key") or "").strip())
        self._upsert_record("workspace_ai_settings", ai)
        self._cache_clear()
        # A newly configured workspace should immediately receive the default
        # featured agents that the runtime expects to expose.
        self.sync_seed_catalog_from_seed()
        return self.get_workspace_ai_setup(profile_id=profile_id)

    def _parse_users_service_ai_settings_payload(
        self,
        payload: dict[str, Any],
        *,
        existing_model_versions: list[ModelVersionOption] | None = None,
    ) -> tuple[list[ModelVersionOption], list[WorkspaceAiSettings]]:
        model_versions = list(existing_model_versions or self._list_model_version_records())
        model_versions_by_id = {item.id: item for item in model_versions}
        imported_ai_settings: list[WorkspaceAiSettings] = []
        ai_settings_rows = payload.get("workspace_ai_settings") or []
        for row in ai_settings_rows:
            if not isinstance(row, dict):
                continue
            version_id = str(row.get("version") or "").strip()
            provider = str(row.get("provider") or "").strip()
            provider_label = str(row.get("provider_label") or "").strip()
            if not provider and version_id:
                provider, provider_label = _infer_provider_from_model_name(version_id)
            version_id = _normalize_workspace_model_version_id(version_id, provider)
            existing_model = model_versions_by_id.get(version_id) if version_id else None
            if version_id and existing_model is None:
                model = ModelVersionOption(
                    id=version_id,
                    provider=provider,
                    provider_label=provider_label or provider.title(),
                    model_name=version_id,
                    base_url=str(row.get("provider_base_url") or row.get("base_url") or ""),
                )
                model_versions.append(model)
                model_versions_by_id[model.id] = model
            elif existing_model is not None and existing_model.provider == "unknown" and provider != "unknown":
                existing_model.provider = provider
                existing_model.provider_label = provider_label or provider.title()
                if not existing_model.base_url:
                    existing_model.base_url = str(row.get("provider_base_url") or row.get("base_url") or "")
            imported_ai_settings.append(
                WorkspaceAiSettings(
                    profile=str(row.get("profile") or "").strip(),
                    name=str(row.get("name") or "").strip(),
                    version=version_id,
                    base_url=str(row.get("base_url") or ""),
                    special_instruction=str(row.get("special_instruction") or ""),
                    system_instruction=str(row.get("system_instruction") or ""),
                    assistant_instruction=str(row.get("assistant_instruction") or ""),
                    api_key=str(row.get("api_key") or ""),
                    tavily_api_key=str(row.get("tavily_api_key") or ""),
                )
            )
        return model_versions, imported_ai_settings

    def sync_users_ai_settings_payload(self, payload: dict[str, Any]) -> dict[str, int]:
        self._cache_clear()
        self.ensure_seeded()
        model_versions, imported_ai_settings = self._parse_users_service_ai_settings_payload(payload)
        self._upsert_records("model_versions", model_versions)
        self._upsert_records("workspace_ai_settings", imported_ai_settings)
        self._cache_clear()
        return {
            "workspace_ai_settings": len(imported_ai_settings),
            "model_versions": len(model_versions),
        }

    def import_users_service_payload(self, payload: dict[str, Any]) -> dict[str, int]:
        self._cache_clear()
        self.ensure_seeded()
        templates_by_slug = {item.slug: item for item in self._list_template_records()}
        skills_by_key = {item.key: item for item in self._list_skill_records()}
        tools_by_key = {item.key: item for item in self._list_tool_records()}
        model_versions = self._list_model_version_records()

        self._delete_records("workspace_ai_settings")
        self._delete_records("workspace_tool_connections")
        self._delete_records("workspace_skill_bindings")
        self._delete_records("workspace_tool_bindings")
        self._delete_records("workspace_agents")

        model_versions, imported_ai_settings = self._parse_users_service_ai_settings_payload(
            payload,
            existing_model_versions=model_versions,
        )
        imported_tool_connections: list[WorkspaceToolConnection] = []

        tool_servers_by_server_id = {item.server_id: item for item in self._list_tool_server_records()}
        tool_connections_rows = payload.get("workspace_tool_connections") or []
        for row in tool_connections_rows:
            if not isinstance(row, dict):
                continue
            profile_id = str(row.get("profile") or "").strip()
            tool_server_server_id = str(row.get("tool_server_server_id") or "").strip()
            tool_server = tool_servers_by_server_id.get(tool_server_server_id)
            if not profile_id or tool_server is None:
                continue
            imported_tool_connections.append(
                WorkspaceToolConnection(
                    profile=profile_id,
                    tool_server_id=tool_server.id,
                    name=str(row.get("name") or "").strip(),
                    slug=str(row.get("slug") or "").strip(),
                    connection_scope=str(row.get("connection_scope") or "workspace"),
                    owner_user=str(row.get("owner_user") or "") or None,
                    auth_type=str(row.get("auth_type") or "").strip(),
                    server_url_override=str(row.get("server_url_override") or ""),
                    credential_payload_encrypted=str(row.get("credential_payload_encrypted") or ""),
                    access_token_encrypted=str(row.get("access_token_encrypted") or ""),
                    refresh_token_encrypted=str(row.get("refresh_token_encrypted") or ""),
                    token_expires_at=row.get("token_expires_at"),
                    granted_scopes=deepcopy(row.get("granted_scopes") or []),
                    resource_owner_id=str(row.get("resource_owner_id") or ""),
                    resource_label=str(row.get("resource_label") or ""),
                    status=str(row.get("status") or "pending"),
                    last_tested_at=row.get("last_tested_at"),
                    last_error=str(row.get("last_error") or ""),
                    metadata=deepcopy(row.get("metadata") or {}),
                    created_by=str(row.get("created_by") or "") or None,
                    updated_by=str(row.get("updated_by") or "") or None,
                )
            )

        imported_agents: list[WorkspaceAgent] = []
        imported_skill_bindings: list[WorkspaceAgentSkillBinding] = []
        imported_tool_bindings: list[WorkspaceAgentToolBinding] = []
        workspace_agents_rows = payload.get("workspace_agents") or []
        for row in workspace_agents_rows:
            if not isinstance(row, dict):
                continue
            profile_id = str(row.get("profile") or "").strip()
            if not profile_id:
                continue
            source_template_slug = str(row.get("source_template_slug") or "").strip()
            source_template = templates_by_slug.get(source_template_slug) if source_template_slug else None
            llm_version_payload = row.get("llm_version")
            llm_version = llm_version_payload if isinstance(llm_version_payload, dict) else None

            agent = WorkspaceAgent(
                profile=profile_id,
                source_template_id=source_template.id if source_template else None,
                origin=str(row.get("origin") or "custom"),
                visibility=str(row.get("visibility") or "workspace"),
                routing_policy=str(row.get("routing_policy") or "direct"),
                slug=str(row.get("slug") or "").strip(),
                name=str(row.get("name") or "").strip(),
                description=str(row.get("description") or ""),
                protocol_version=str(row.get("protocol_version") or "0.3.0"),
                preferred_transport=str(row.get("preferred_transport") or "local"),
                url=str(row.get("url") or ""),
                provider_organization=str(row.get("provider_organization") or ""),
                provider_url=str(row.get("provider_url") or ""),
                version=str(row.get("version") or "0.1.0"),
                documentation_url=str(row.get("documentation_url") or ""),
                icon_url=str(row.get("icon_url") or ""),
                additional_interfaces=deepcopy(row.get("additional_interfaces") or []),
                capabilities=deepcopy(row.get("capabilities") or {}),
                security_schemes=deepcopy(row.get("security_schemes") or {}),
                security=deepcopy(row.get("security") or []),
                supports_authenticated_extended_card=bool(row.get("supports_authenticated_extended_card", True)),
                default_input_modes=deepcopy(row.get("default_input_modes") or ["text"]),
                default_output_modes=deepcopy(row.get("default_output_modes") or ["text"]),
                system_instruction=str(row.get("system_instruction") or ""),
                developer_instruction=str(row.get("developer_instruction") or ""),
                assistant_instruction=str(row.get("assistant_instruction") or ""),
                llm_version=llm_version,
                llm_temperature=float(row.get("llm_temperature", 0.2)),
                max_reasoning_steps=int(row.get("max_reasoning_steps", 5)),
                metadata=deepcopy(row.get("metadata") or {}),
                is_enabled=bool(row.get("is_enabled", True)),
                template_version_snapshot=str(row.get("template_version_snapshot") or ""),
                created_by=str(row.get("created_by") or "") or None,
                updated_by=str(row.get("updated_by") or "") or None,
            )
            imported_agents.append(agent)

            for skill_binding in row.get("skill_bindings") or []:
                if not isinstance(skill_binding, dict):
                    continue
                skill_key = str(skill_binding.get("skill_key") or "").strip()
                skill = skills_by_key.get(skill_key)
                if skill is None:
                    continue
                imported_skill_bindings.append(
                    WorkspaceAgentSkillBinding(
                        agent_id=agent.id,
                        skill_id=skill.id,
                        order=int(skill_binding.get("order", 0)),
                        is_primary=bool(skill_binding.get("is_primary", False)),
                        metadata=deepcopy(skill_binding.get("metadata") or {}),
                    )
                )

            for tool_binding in row.get("tool_bindings") or []:
                if not isinstance(tool_binding, dict):
                    continue
                tool_key = str(tool_binding.get("tool_key") or "").strip()
                tool = tools_by_key.get(tool_key)
                if tool is None:
                    continue
                imported_tool_bindings.append(
                    WorkspaceAgentToolBinding(
                        agent_id=agent.id,
                        tool_id=tool.id,
                        order=int(tool_binding.get("order", 0)),
                        is_required=bool(tool_binding.get("is_required", False)),
                        tool_config=deepcopy(tool_binding.get("tool_config") or {}),
                    )
                )

        self._upsert_records("model_versions", model_versions)
        self._upsert_records("workspace_ai_settings", imported_ai_settings)
        self._upsert_records("workspace_tool_connections", imported_tool_connections)
        self._upsert_records("workspace_agents", imported_agents)
        self._upsert_records("workspace_skill_bindings", imported_skill_bindings)
        self._upsert_records("workspace_tool_bindings", imported_tool_bindings)
        return {
            "workspace_ai_settings": len(imported_ai_settings),
            "workspace_tool_connections": len(imported_tool_connections),
            "workspace_agents": len(imported_agents),
            "workspace_skill_bindings": len(imported_skill_bindings),
            "workspace_tool_bindings": len(imported_tool_bindings),
        }

    def _mask_secret(self, value: str) -> str:
        if not value:
            return ""
        if len(value) <= 8:
            return "*" * len(value)
        return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"

    def build_principal_claim_overrides(self, *, profile_id: str) -> dict[str, Any]:
        cache_key = ("build_principal_claim_overrides", profile_id)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        ai = self._get_workspace_ai_settings_record(profile_id=profile_id)
        if ai is None:
            return {}
        version = next((item for item in self._list_model_version_records() if item.id == ai.version), None)
        if version is None:
            return {}
        llm_claim: dict[str, Any] = {
            "provider": version.provider,
            "model": version.model_name,
        }
        base_url = ai.base_url or (version.base_url or "")
        if base_url:
            llm_claim["baseUrl"] = base_url
        api_key = _decrypt_secret(ai.api_key)
        if not api_key or api_key.startswith("gAAAA"):
            api_key = ""
        if api_key:
            llm_claim["apiKey"] = _secret_for_claim(api_key)
        payload: dict[str, Any] = {
            KA2A_JWT_CLAIM_KEY: {
                "v": 1,
                "llm": llm_claim,
            }
        }
        tavily_api_key = _decrypt_secret(ai.tavily_api_key)
        if tavily_api_key and not tavily_api_key.startswith("gAAAA"):
            payload[KA2A_JWT_CLAIM_KEY]["tavily"] = {
                "apiKey": _secret_for_claim(tavily_api_key)
            }
        return self._cache_set(cache_key, payload)

    def resolve_workspace_tavily_api_key(self, *, profile_id: str) -> str:
        ai = self._get_workspace_ai_settings_record(profile_id=profile_id)
        if ai is not None:
            api_key = _decrypt_secret(ai.tavily_api_key)
            if api_key and not api_key.startswith("gAAAA"):
                return api_key
        return ""

    def _workspace_agent_payloads(self, agents: list[WorkspaceAgent]) -> list[dict[str, Any]]:
        if not agents:
            return []
        agent_ids = [item.id for item in agents]
        template_ids = sorted({item.source_template_id for item in agents if item.source_template_id})
        skill_bindings = self._list_workspace_skill_binding_records(agent_ids=agent_ids)
        tool_bindings = self._list_workspace_tool_binding_records(agent_ids=agent_ids)
        template_skill_bindings = self._list_template_skill_binding_records(template_ids=template_ids)
        template_tool_bindings = self._list_template_tool_binding_records(template_ids=template_ids)
        skills = self._list_skill_records(ids=sorted({item.skill_id for item in skill_bindings} | {item.skill_id for item in template_skill_bindings}))
        tools = self._list_tool_records(ids=sorted({item.tool_id for item in tool_bindings} | {item.tool_id for item in template_tool_bindings}))
        servers = self._list_tool_server_records(ids=sorted({item.tool_server_id for item in tools if item.tool_server_id}))
        templates = self._list_template_records(ids=template_ids) if template_ids else []
        skills_by_id = {item.id: item for item in skills}
        tools_by_id = {item.id: item for item in tools}
        servers_by_id = {item.id: item for item in servers}
        templates_by_id = {item.id: item for item in templates}
        template_skill_bindings_by_template_id = self._group_bindings_by(template_skill_bindings, "template_id")  # type: ignore[arg-type]
        template_tool_bindings_by_template_id = self._group_bindings_by(template_tool_bindings, "template_id")  # type: ignore[arg-type]
        skill_bindings_by_agent_id = self._group_bindings_by(skill_bindings, "agent_id")  # type: ignore[arg-type]
        tool_bindings_by_agent_id = self._group_bindings_by(tool_bindings, "agent_id")  # type: ignore[arg-type]
        return [
            self._workspace_agent_to_read_from_records(
                agent,
                skills_by_id=skills_by_id,
                tools_by_id=tools_by_id,
                servers_by_id=servers_by_id,
                templates_by_id=templates_by_id,
                template_skill_bindings_by_template_id=template_skill_bindings_by_template_id,
                template_tool_bindings_by_template_id=template_tool_bindings_by_template_id,
                skill_bindings_by_agent_id=skill_bindings_by_agent_id,
                tool_bindings_by_agent_id=tool_bindings_by_agent_id,
            )
            for agent in agents
        ]

    def list_workspace_agents(self, *, profile_id: str) -> list[dict[str, Any]]:
        cache_key = ("list_workspace_agents", profile_id)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        agents = sorted(self._list_workspace_agent_records(profile_id=profile_id), key=lambda item: item.name.lower())
        return self._cache_set(cache_key, self._workspace_agent_payloads(agents))

    def _assert_unique_slug(self, *, profile_id: str, slug: str, exclude_agent_id: str | None = None) -> None:
        for agent in self._list_workspace_agent_records(profile_id=profile_id):
            if agent.slug == slug and agent.id != exclude_agent_id:
                raise AgentControlPlaneError("A workspace agent with this slug already exists.")

    def create_workspace_agent(self, *, profile_id: str, user_id: str, data: dict[str, Any]) -> dict[str, Any]:
        slug = str(data.get("slug") or "").strip()
        if not slug:
            raise AgentControlPlaneError("slug is required.")
        self._assert_unique_slug(profile_id=profile_id, slug=slug)
        agent = WorkspaceAgent(
            profile=profile_id,
            origin="custom",
            slug=slug,
            name=str(data.get("name") or "").strip() or slug.replace("-", " ").title(),
            description=str(data.get("description") or ""),
            visibility=data.get("visibility") or "workspace",
            routing_policy=data.get("routing_policy") or "direct",
            protocol_version=data.get("protocol_version") or "0.3.0",
            preferred_transport=data.get("preferred_transport") or "local",
            url=str(data.get("url") or ""),
            provider_organization=str(data.get("provider_organization") or ""),
            provider_url=str(data.get("provider_url") or ""),
            version=str(data.get("version") or "0.1.0"),
            documentation_url=str(data.get("documentation_url") or ""),
            icon_url=str(data.get("icon_url") or ""),
            additional_interfaces=deepcopy(data.get("additional_interfaces") or []),
            capabilities=deepcopy(data.get("capabilities") or {}),
            security_schemes=deepcopy(data.get("security_schemes") or {}),
            security=deepcopy(data.get("security") or []),
            supports_authenticated_extended_card=bool(data.get("supports_authenticated_extended_card", True)),
            default_input_modes=deepcopy(data.get("default_input_modes") or ["text"]),
            default_output_modes=deepcopy(data.get("default_output_modes") or ["text"]),
            system_instruction=str(data.get("system_instruction") or ""),
            developer_instruction=str(data.get("developer_instruction") or ""),
            assistant_instruction=str(data.get("assistant_instruction") or ""),
            llm_version=_serialize_model_version(data.get("llm_version")),
            llm_temperature=float(data.get("llm_temperature", 0.2)),
            max_reasoning_steps=int(data.get("max_reasoning_steps", 5)),
            metadata=deepcopy(data.get("metadata") or {}),
            is_enabled=bool(data.get("is_enabled", True)),
            created_by=user_id,
            updated_by=user_id,
        )
        self._upsert_record("workspace_agents", agent)
        self._cache_clear()
        payloads = self._workspace_agent_payloads([agent])
        return payloads[0] if payloads else agent.model_dump(mode="json")

    def update_workspace_agent(self, *, profile_id: str, agent_id: str, user_id: str, data: dict[str, Any]) -> dict[str, Any]:
        agent = self._get_workspace_agent_record(profile_id=profile_id, agent_id=agent_id)
        if agent is None:
            raise AgentControlPlaneError("Workspace agent was not found.")
        if "slug" in data:
            slug = str(data.get("slug") or "").strip()
            if not slug:
                raise AgentControlPlaneError("slug cannot be blank.")
            self._assert_unique_slug(profile_id=profile_id, slug=slug, exclude_agent_id=agent.id)
            agent.slug = slug
        for field_name in [
            "name",
            "description",
            "visibility",
            "routing_policy",
            "protocol_version",
            "preferred_transport",
            "url",
            "provider_organization",
            "provider_url",
            "version",
            "documentation_url",
            "icon_url",
            "system_instruction",
            "developer_instruction",
            "assistant_instruction",
        ]:
            if field_name in data:
                setattr(agent, field_name, data.get(field_name) or "")
        for field_name in ["additional_interfaces", "capabilities", "security_schemes", "security", "default_input_modes", "default_output_modes", "metadata"]:
            if field_name in data:
                setattr(agent, field_name, deepcopy(data.get(field_name) or ([] if field_name in {"additional_interfaces", "security", "default_input_modes", "default_output_modes"} else {})))
        if "supports_authenticated_extended_card" in data:
            agent.supports_authenticated_extended_card = bool(data["supports_authenticated_extended_card"])
        if "llm_version" in data:
            agent.llm_version = _serialize_model_version(data["llm_version"])
        if "llm_temperature" in data:
            agent.llm_temperature = float(data["llm_temperature"])
        if "max_reasoning_steps" in data:
            agent.max_reasoning_steps = int(data["max_reasoning_steps"])
        if "is_enabled" in data:
            agent.is_enabled = bool(data["is_enabled"])
        agent.updated_by = user_id
        from .models import utcnow
        agent.updated_at = utcnow()
        self._upsert_record("workspace_agents", agent)
        self._cache_clear()
        payloads = self._workspace_agent_payloads([agent])
        return payloads[0] if payloads else agent.model_dump(mode="json")

    def delete_workspace_agent(self, *, profile_id: str, agent_id: str) -> None:
        agent = self._get_workspace_agent_record(profile_id=profile_id, agent_id=agent_id)
        if agent is None:
            raise AgentControlPlaneError("Workspace agent was not found.")
        self._delete_records("workspace_skill_bindings", filters={"agent_id": agent_id})
        self._delete_records("workspace_tool_bindings", filters={"agent_id": agent_id})
        self._delete_records("workspace_agents", ids=[agent_id])
        self._cache_clear()

    def install_template(self, *, profile_id: str, user_id: str, template_id: str, data: dict[str, Any]) -> dict[str, Any]:
        template = next(
            (
                item
                for item in self._list_template_records(ids=[template_id], filters={"is_active": True, "allow_workspace_installs": True})
                if item.id == template_id
            ),
            None,
        )
        if template is None:
            raise AgentControlPlaneError("Agent template was not found.")
        slug = str(data.get("slug") or template.slug).strip()
        if not slug:
            raise AgentControlPlaneError("slug is required.")
        self._assert_unique_slug(profile_id=profile_id, slug=slug)
        agent = WorkspaceAgent(
            profile=profile_id,
            source_template_id=template.id,
            origin="template",
            visibility=data.get("visibility") or "workspace",
            routing_policy=data.get("routing_policy") or "direct",
            slug=slug,
            name=str(data.get("name") or template.name).strip() or template.name,
            description=str(data.get("description", template.description) or ""),
            protocol_version=template.protocol_version,
            preferred_transport=template.preferred_transport,
            url=template.url,
            provider_organization=template.provider_organization,
            provider_url=template.provider_url,
            version=template.version,
            documentation_url=template.documentation_url,
            icon_url=template.icon_url,
            additional_interfaces=deepcopy(template.additional_interfaces),
            capabilities=deepcopy(template.capabilities),
            security_schemes=deepcopy(template.security_schemes),
            security=deepcopy(template.security),
            supports_authenticated_extended_card=template.supports_authenticated_extended_card,
            default_input_modes=deepcopy(template.default_input_modes),
            default_output_modes=deepcopy(template.default_output_modes),
            system_instruction=str(data.get("system_instruction", template.system_instruction) or ""),
            developer_instruction=str(data.get("developer_instruction", template.developer_instruction) or ""),
            assistant_instruction=str(data.get("assistant_instruction", template.assistant_instruction) or ""),
            llm_version=deepcopy(template.llm_version),
            llm_temperature=template.llm_temperature,
            max_reasoning_steps=template.max_reasoning_steps,
            metadata=deepcopy(template.metadata),
            is_enabled=bool(data.get("is_enabled", True)),
            template_version_snapshot=template.version,
            created_by=user_id,
            updated_by=user_id,
        )
        self._upsert_record("workspace_agents", agent)
        skill_bindings = [
            WorkspaceAgentSkillBinding(
                agent_id=agent.id,
                skill_id=binding.skill_id,
                order=binding.order,
                is_primary=binding.is_primary,
                metadata=deepcopy(binding.metadata),
            )
            for binding in self._list_template_skill_binding_records(template_ids=[template.id])
        ]
        tool_bindings = [
            WorkspaceAgentToolBinding(
                agent_id=agent.id,
                tool_id=binding.tool_id,
                order=binding.order,
                is_required=binding.is_required,
                tool_config=deepcopy(binding.tool_config),
            )
            for binding in self._list_template_tool_binding_records(template_ids=[template.id])
        ]
        self._upsert_records("workspace_skill_bindings", skill_bindings)
        self._upsert_records("workspace_tool_bindings", tool_bindings)
        self._cache_clear()
        payloads = self._workspace_agent_payloads([agent])
        return payloads[0] if payloads else agent.model_dump(mode="json")

    def attach_tool(self, *, profile_id: str, agent_id: str, tool_id: str, body: dict[str, Any]) -> dict[str, Any]:
        agent = self._get_workspace_agent_record(profile_id=profile_id, agent_id=agent_id)
        tool = next((item for item in self._list_tool_records(ids=[tool_id]) if item.id == tool_id), None)
        if agent is None or tool is None:
            raise AgentControlPlaneError("Workspace agent or tool was not found.")
        binding = next(
            (
                item
                for item in self._list_workspace_tool_binding_records(agent_ids=[agent.id])
                if item.tool_id == tool.id
            ),
            None,
        )
        if binding is None:
            binding = WorkspaceAgentToolBinding(
                agent_id=agent.id,
                tool_id=tool.id,
            )
        binding.order = int(body.get("order", len(self._list_workspace_tool_binding_records(agent_ids=[agent.id]))))
        binding.is_required = bool(body.get("is_required", False))
        binding.tool_config = deepcopy(body.get("tool_config") or {})
        self._upsert_record("workspace_tool_bindings", binding)
        self._cache_clear()
        payloads = self._workspace_agent_payloads([agent])
        return payloads[0] if payloads else agent.model_dump(mode="json")

    def detach_tool(self, *, profile_id: str, agent_id: str, tool_id: str) -> dict[str, Any]:
        agent = self._get_workspace_agent_record(profile_id=profile_id, agent_id=agent_id)
        if agent is None:
            raise AgentControlPlaneError("Workspace agent was not found.")
        self._delete_records("workspace_tool_bindings", filters={"agent_id": agent_id, "tool_id": tool_id})
        self._cache_clear()
        payloads = self._workspace_agent_payloads([agent])
        return payloads[0] if payloads else agent.model_dump(mode="json")

    def attach_skill(self, *, profile_id: str, agent_id: str, skill_id: str, body: dict[str, Any]) -> dict[str, Any]:
        agent = self._get_workspace_agent_record(profile_id=profile_id, agent_id=agent_id)
        skill = next((item for item in self._list_skill_records(ids=[skill_id]) if item.id == skill_id), None)
        if agent is None or skill is None:
            raise AgentControlPlaneError("Workspace agent or skill was not found.")
        binding = next(
            (
                item
                for item in self._list_workspace_skill_binding_records(agent_ids=[agent.id])
                if item.skill_id == skill.id
            ),
            None,
        )
        if binding is None:
            binding = WorkspaceAgentSkillBinding(agent_id=agent.id, skill_id=skill.id)
        binding.order = int(body.get("order", len(self._list_workspace_skill_binding_records(agent_ids=[agent.id]))))
        binding.is_primary = bool(body.get("is_primary", False))
        binding.metadata = deepcopy(body.get("metadata") or {})
        self._upsert_record("workspace_skill_bindings", binding)
        self._cache_clear()
        payloads = self._workspace_agent_payloads([agent])
        return payloads[0] if payloads else agent.model_dump(mode="json")

    def detach_skill(self, *, profile_id: str, agent_id: str, skill_id: str) -> dict[str, Any]:
        agent = self._get_workspace_agent_record(profile_id=profile_id, agent_id=agent_id)
        if agent is None:
            raise AgentControlPlaneError("Workspace agent was not found.")
        self._delete_records("workspace_skill_bindings", filters={"agent_id": agent_id, "skill_id": skill_id})
        self._cache_clear()
        payloads = self._workspace_agent_payloads([agent])
        return payloads[0] if payloads else agent.model_dump(mode="json")

    def _runtime_connection_payload(self, connection: WorkspaceToolConnection) -> dict[str, Any]:
        return {
            "id": connection.id,
            "connection_scope": connection.connection_scope,
            "owner_user_id": connection.owner_user,
            "auth_type": connection.auth_type,
            "status": connection.status,
            "server_url_override": connection.server_url_override or None,
            "headers": _connection_runtime_headers(connection),
            "granted_scopes": deepcopy(connection.granted_scopes),
            "token_expires_at": connection.token_expires_at.isoformat() if connection.token_expires_at else None,
            "resource_owner_id": connection.resource_owner_id or None,
            "resource_label": connection.resource_label or None,
            "metadata": deepcopy(connection.metadata),
        }

    def _runtime_config_payloads_for_agents(self, agents: list[WorkspaceAgent]) -> list[dict[str, Any]]:
        if not agents:
            return []
        payloads = self._workspace_agent_payloads(agents)
        agents_by_id = {agent.id: agent for agent in agents}
        connections_by_profile_and_server: dict[tuple[str, str], list[WorkspaceToolConnection]] = {}
        profile_ids = sorted({agent.profile for agent in agents})
        if profile_ids:
            for connection in self._list_workspace_tool_connection_records():
                if connection.profile not in profile_ids:
                    continue
                key = (connection.profile, connection.tool_server_id)
                connections_by_profile_and_server.setdefault(key, []).append(connection)
        template_ids = sorted({agent.source_template_id for agent in agents if agent.source_template_id})
        template_runtime_by_id: dict[str, dict[str, Any]] = {}
        if template_ids:
            for template in self._list_template_records(ids=template_ids):
                if isinstance(template.metadata, dict):
                    template_runtime_by_id[template.id] = deepcopy(template.metadata.get("runtime") or {})
        for read_payload in payloads:
            agent_id = str(read_payload.get("id") or "").strip()
            agent = agents_by_id.get(agent_id)
            if agent is None:
                continue
            template_runtime = template_runtime_by_id.get(agent.source_template_id or "", {})
            for item in read_payload.get("tool_bindings") or []:
                tool = item.get("tool") if isinstance(item, dict) else None
                server = tool.get("tool_server") if isinstance(tool, dict) else None
                server_id = str(server.get("id") or "").strip() if isinstance(server, dict) else ""
                if not server_id:
                    continue
                server["runtime_connections"] = [
                    self._runtime_connection_payload(connection)
                    for connection in connections_by_profile_and_server.get((agent.profile, server_id), [])
                    if connection.status in {"connected", "pending", "expired", "error"}
                ]
            read_payload["runtime_name"] = agent.build_runtime_name()
            read_payload["source_template_slug"] = (
                read_payload["source_template"]["slug"] if read_payload.get("source_template") else None
            )
            read_payload["runtime_config"] = agent.build_runtime_config(template_runtime=template_runtime)
            read_payload["runtime_card_payload"] = agent.build_runtime_card_payload(
                skills=[AgentSkill.model_validate(item["skill"]) for item in read_payload["skill_bindings"]],
                tool_payload=[
                    {
                        "key": item["tool"]["key"],
                        "name": item["tool"]["full_tool_name"],
                        "displayName": item["tool"]["display_name"],
                        "description": item["tool"]["description"],
                        "required": item["is_required"],
                        "toolServerId": item["tool"]["tool_server"]["server_id"] if item["tool"]["tool_server"] else None,
                    }
                    for item in read_payload["tool_bindings"]
                ],
            )
        return payloads

    def runtime_registry(self, *, access: AgentRuntimeAccessContext) -> dict[str, Any]:
        cache_key = (
            "runtime_registry",
            access.profile_id,
            access.user_id,
            access.is_owner,
            tuple(sorted(access.permissions)),
        )
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        agents = []
        visible_agents = sorted(self._list_workspace_agent_records(profile_id=access.profile_id, enabled_only=True), key=lambda item: item.name.lower())
        if not visible_agents:
            return self._cache_set(cache_key, {
                "profile_id": access.profile_id,
                "workspace_name": f"Workspace {access.profile_id}",
                "agent_count": 0,
                "agents": [],
            })
        runtime_payloads = self.list_workspace_agents(profile_id=access.profile_id)
        payload_by_id = {item["id"]: item for item in runtime_payloads}
        for agent in visible_agents:
            if not agent.is_enabled:
                continue
            if agent.visibility == "private" and agent.created_by != access.user_id:
                continue
            read_payload = payload_by_id.get(agent.id)
            if read_payload is None:
                continue
            agents.append(
                {
                    "id": read_payload["id"],
                    "slug": read_payload["slug"],
                    "name": read_payload["name"],
                    "description": read_payload["description"],
                    "origin": read_payload["origin"],
                    "visibility": read_payload["visibility"],
                    "routing_policy": read_payload["routing_policy"],
                    "preferred_transport": read_payload["preferred_transport"],
                    "version": read_payload["version"],
                    "icon_url": read_payload["icon_url"],
                    "documentation_url": read_payload["documentation_url"],
                    "source_template_slug": (
                        read_payload["source_template"]["slug"] if read_payload.get("source_template") else None
                    ),
                    "llm_version": read_payload["llm_version"],
                    "capabilities": read_payload["capabilities"],
                    "default_input_modes": read_payload["default_input_modes"],
                    "default_output_modes": read_payload["default_output_modes"],
                    "supports_authenticated_extended_card": read_payload["supports_authenticated_extended_card"],
                    "metadata": read_payload["metadata"],
                    "tool_count": len(read_payload["tool_bindings"]),
                    "skill_count": len(read_payload["skill_bindings"]),
                    "card_payload": read_payload["card_payload"],
                }
            )
        return self._cache_set(cache_key, {
            "profile_id": access.profile_id,
            "workspace_name": f"Workspace {access.profile_id}",
            "agent_count": len(agents),
            "agents": agents,
        })

    def runtime_agent_card(self, *, access: AgentRuntimeAccessContext, slug: str) -> dict[str, Any]:
        return self.runtime_agent_config(access=access, slug=slug)["card_payload"]

    def runtime_agent_config(self, *, access: AgentRuntimeAccessContext, slug: str) -> dict[str, Any]:
        cache_key = (
            "runtime_agent_config",
            access.profile_id,
            access.user_id,
            access.is_owner,
            tuple(sorted(access.permissions)),
            slug,
        )
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        agent = self._get_workspace_agent_record(profile_id=access.profile_id, slug=slug, enabled_only=True)
        if agent is None:
            raise AgentControlPlaneError("Runtime agent was not found.")
        if agent.visibility == "private" and agent.created_by != access.user_id:
            raise AgentControlPlaneError("Runtime agent is not visible to this user.")
        payloads = self._runtime_config_payloads_for_agents([agent])
        read_payload = payloads[0] if payloads else None
        if read_payload is None:
            raise AgentControlPlaneError("Runtime agent was not found.")
        return self._cache_set(cache_key, read_payload)

    def internal_runtime_registry(self) -> dict[str, Any]:
        agents = self._runtime_config_payloads_for_agents(
            sorted(self._list_workspace_agent_records(enabled_only=True), key=lambda item: (item.profile, item.name.lower()))
        )
        return {
            "agent_count": len(agents),
            "agents": agents,
        }
