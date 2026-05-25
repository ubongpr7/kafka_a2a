from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from kafka_a2a.server.auth import JwtBearerConfig

from ..common.auth import build_agent_auth_context, get_bearer_token_from_request, require_permission
from .services import AgentControlPlaneError, AgentControlPlaneService, AgentRuntimeAccessContext


class WorkspaceAgentWritePayload(BaseModel):
    slug: str
    name: str
    description: str | None = None
    visibility: str | None = None
    routing_policy: str | None = None
    protocol_version: str | None = None
    preferred_transport: str | None = None
    url: str | None = None
    provider_organization: str | None = None
    provider_url: str | None = None
    version: str | None = None
    documentation_url: str | None = None
    icon_url: str | None = None
    additional_interfaces: list[dict[str, Any]] | None = None
    capabilities: dict[str, Any] | None = None
    security_schemes: dict[str, Any] | None = None
    security: list[dict[str, Any]] | None = None
    supports_authenticated_extended_card: bool | None = None
    default_input_modes: list[str] | None = None
    default_output_modes: list[str] | None = None
    system_instruction: str | None = None
    developer_instruction: str | None = None
    assistant_instruction: str | None = None
    llm_version: dict[str, Any] | None = None
    llm_temperature: float | None = None
    max_reasoning_steps: int | None = None
    metadata: dict[str, Any] | None = None
    is_enabled: bool | None = None


class InstallTemplatePayload(BaseModel):
    slug: str | None = None
    name: str | None = None
    description: str | None = None
    visibility: str | None = None
    routing_policy: str | None = None
    system_instruction: str | None = None
    developer_instruction: str | None = None
    assistant_instruction: str | None = None
    is_enabled: bool | None = None


class WorkspaceToolConnectionWritePayload(BaseModel):
    tool_server: str | None = None
    name: str | None = None
    slug: str | None = None
    connection_scope: str | None = None
    owner_user: str | None = None
    auth_type: str | None = None
    server_url_override: str | None = None
    credential_payload: dict[str, Any] | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    token_expires_at: datetime | None = None
    granted_scopes: list[str] | None = None
    resource_owner_id: str | None = None
    resource_label: str | None = None
    status: str | None = None
    last_tested_at: datetime | None = None
    last_error: str | None = None
    metadata: dict[str, Any] | None = None


class AttachToolPayload(BaseModel):
    tool_id: str
    order: int | None = None
    is_required: bool | None = None
    tool_config: dict[str, Any] | None = None


class AttachSkillPayload(BaseModel):
    skill_id: str
    order: int | None = None
    is_primary: bool | None = None
    metadata: dict[str, Any] | None = None


def build_agents_router(
    *,
    service: AgentControlPlaneService,
    jwt: JwtBearerConfig | None,
    runtime_shared_token: str | None,
) -> APIRouter:
    router = APIRouter(prefix="/agent_api", tags=["agents"])

    def _auth_context(request: Request):
        token = get_bearer_token_from_request(request)
        return build_agent_auth_context(token=token, jwt=jwt)

    def _runtime_access(request: Request) -> AgentRuntimeAccessContext:
        auth = _auth_context(request)
        return AgentRuntimeAccessContext(
            user_id=auth.user_id,
            profile_id=auth.profile_id,
            is_owner=auth.is_workspace_owner,
            permissions=auth.permissions,
        )

    def _require_runtime_token(request: Request) -> None:
        expected = (runtime_shared_token or "").strip()
        provided = (request.headers.get("X-KA2A-Runtime-Token") or "").strip()
        if not expected or not provided or not secrets.compare_digest(expected, provided):
            raise HTTPException(status_code=403, detail="Runtime sync token is invalid.")

    @router.get("/templates/")
    async def list_templates(request: Request):
        require_permission(_auth_context(request), "manage_agent_settings")
        return await run_in_threadpool(service.list_templates)

    @router.get("/tool-servers/")
    async def list_tool_servers(request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        return await run_in_threadpool(service.list_tool_servers, profile_id=auth.profile_id)

    @router.get("/tool-connections/")
    async def list_tool_connections(request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        return await run_in_threadpool(service.list_workspace_tool_connections, profile_id=auth.profile_id)

    @router.post("/tool-connections/", status_code=201)
    async def create_tool_connection(body: WorkspaceToolConnectionWritePayload, request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        try:
            return await run_in_threadpool(
                service.create_workspace_tool_connection,
                profile_id=auth.profile_id,
                user_id=auth.user_id,
                data=body.model_dump(exclude_unset=True),
            )
        except AgentControlPlaneError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/tool-connections/{connection_id}/")
    async def update_tool_connection(connection_id: str, body: WorkspaceToolConnectionWritePayload, request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        try:
            return await run_in_threadpool(
                service.update_workspace_tool_connection,
                profile_id=auth.profile_id,
                connection_id=connection_id,
                user_id=auth.user_id,
                data=body.model_dump(exclude_unset=True),
            )
        except AgentControlPlaneError as exc:
            status_code = 404 if "not found" in str(exc).lower() else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @router.delete("/tool-connections/{connection_id}/", status_code=204)
    async def delete_tool_connection(connection_id: str, request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        try:
            await run_in_threadpool(
                service.delete_workspace_tool_connection,
                profile_id=auth.profile_id,
                connection_id=connection_id,
            )
        except AgentControlPlaneError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(status_code=204)

    @router.post("/tool-connections/{connection_id}/test_connection/")
    async def test_tool_connection(connection_id: str, request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        try:
            payload = await run_in_threadpool(
                service.test_workspace_tool_connection,
                profile_id=auth.profile_id,
                connection_id=connection_id,
                user_id=auth.user_id,
            )
        except AgentControlPlaneError as exc:
            status_code = 404 if "not found" in str(exc).lower() else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        if payload.get("ok"):
            return payload
        return JSONResponse(status_code=400, content=payload)

    @router.get("/tools/")
    async def list_tools(request: Request):
        require_permission(_auth_context(request), "manage_agent_settings")
        return await run_in_threadpool(service.list_tools)

    @router.get("/skills/")
    async def list_skills(request: Request):
        require_permission(_auth_context(request), "manage_agent_settings")
        return await run_in_threadpool(service.list_skills)

    @router.get("/instruction-presets/")
    async def list_instruction_presets(request: Request):
        require_permission(_auth_context(request), "manage_agent_settings")
        return await run_in_threadpool(service.list_instruction_presets)

    @router.get("/management/agent-setup/")
    async def get_workspace_ai_setup(request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        return await run_in_threadpool(service.get_workspace_ai_setup, profile_id=auth.profile_id)

    @router.post("/management/agent-setup/")
    async def save_workspace_ai_setup(body: dict[str, Any], request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        try:
            return await run_in_threadpool(service.save_workspace_ai_setup, profile_id=auth.profile_id, data=body)
        except AgentControlPlaneError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/management/agent-setup/")
    async def patch_workspace_ai_setup(body: dict[str, Any], request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        try:
            return await run_in_threadpool(service.save_workspace_ai_setup, profile_id=auth.profile_id, data=body)
        except AgentControlPlaneError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/templates/{template_id}/install/")
    async def install_template(template_id: str, body: InstallTemplatePayload, request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        try:
            return await run_in_threadpool(
                service.install_template,
                profile_id=auth.profile_id,
                user_id=auth.user_id,
                template_id=template_id,
                data=body.model_dump(exclude_none=True),
            )
        except AgentControlPlaneError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/workspace-agents/")
    async def list_workspace_agents(request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        return await run_in_threadpool(service.list_workspace_agents, profile_id=auth.profile_id)

    @router.post("/workspace-agents/", status_code=201)
    async def create_workspace_agent(body: WorkspaceAgentWritePayload, request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        try:
            return await run_in_threadpool(
                service.create_workspace_agent,
                profile_id=auth.profile_id,
                user_id=auth.user_id,
                data=body.model_dump(exclude_none=True),
            )
        except AgentControlPlaneError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/workspace-agents/{agent_id}/")
    async def get_workspace_agent(agent_id: str, request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        agents = await run_in_threadpool(service.list_workspace_agents, profile_id=auth.profile_id)
        agent = next((item for item in agents if item["id"] == agent_id), None)
        if agent is None:
            raise HTTPException(status_code=404, detail="Workspace agent was not found.")
        return agent

    @router.patch("/workspace-agents/{agent_id}/")
    async def update_workspace_agent(agent_id: str, body: dict[str, Any], request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        try:
            return await run_in_threadpool(
                service.update_workspace_agent,
                profile_id=auth.profile_id,
                agent_id=agent_id,
                user_id=auth.user_id,
                data=body,
            )
        except AgentControlPlaneError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/workspace-agents/{agent_id}/", status_code=204)
    async def delete_workspace_agent(agent_id: str, request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        try:
            await run_in_threadpool(service.delete_workspace_agent, profile_id=auth.profile_id, agent_id=agent_id)
        except AgentControlPlaneError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(status_code=204)

    @router.post("/workspace-agents/{agent_id}/attach_tool/")
    async def attach_workspace_agent_tool(agent_id: str, body: AttachToolPayload, request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        try:
            return await run_in_threadpool(
                service.attach_tool,
                profile_id=auth.profile_id,
                agent_id=agent_id,
                tool_id=body.tool_id,
                body=body.model_dump(exclude_none=True),
            )
        except AgentControlPlaneError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/workspace-agents/{agent_id}/detach_tool/")
    async def detach_workspace_agent_tool(agent_id: str, body: AttachToolPayload, request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        try:
            return await run_in_threadpool(
                service.detach_tool,
                profile_id=auth.profile_id,
                agent_id=agent_id,
                tool_id=body.tool_id,
            )
        except AgentControlPlaneError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/workspace-agents/{agent_id}/attach_skill/")
    async def attach_workspace_agent_skill(agent_id: str, body: AttachSkillPayload, request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        try:
            return await run_in_threadpool(
                service.attach_skill,
                profile_id=auth.profile_id,
                agent_id=agent_id,
                skill_id=body.skill_id,
                body=body.model_dump(exclude_none=True),
            )
        except AgentControlPlaneError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/workspace-agents/{agent_id}/detach_skill/")
    async def detach_workspace_agent_skill(agent_id: str, body: AttachSkillPayload, request: Request):
        auth = _auth_context(request)
        require_permission(auth, "manage_agent_settings")
        try:
            return await run_in_threadpool(
                service.detach_skill,
                profile_id=auth.profile_id,
                agent_id=agent_id,
                skill_id=body.skill_id,
            )
        except AgentControlPlaneError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/runtime/agents/registry/")
    async def runtime_registry(request: Request):
        access = _runtime_access(request)
        if not access.can_interact():
            raise HTTPException(status_code=403, detail="Missing permission: interact_with_agent")
        return await run_in_threadpool(service.runtime_registry, access=access)

    @router.get("/runtime/agents/{slug}/card/")
    async def runtime_agent_card(slug: str, request: Request):
        access = _runtime_access(request)
        if not access.can_interact():
            raise HTTPException(status_code=403, detail="Missing permission: interact_with_agent")
        try:
            return await run_in_threadpool(service.runtime_agent_card, access=access, slug=slug)
        except AgentControlPlaneError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/runtime/agents/{slug}/config/")
    async def runtime_agent_config(slug: str, request: Request):
        access = _runtime_access(request)
        if not access.can_interact():
            raise HTTPException(status_code=403, detail="Missing permission: interact_with_agent")
        try:
            return await run_in_threadpool(service.runtime_agent_config, access=access, slug=slug)
        except AgentControlPlaneError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/runtime/internal/registry/")
    async def runtime_internal_registry(request: Request):
        _require_runtime_token(request)
        return await run_in_threadpool(service.internal_runtime_registry)

    return router
