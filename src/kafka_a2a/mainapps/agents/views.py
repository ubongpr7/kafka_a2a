from __future__ import annotations

import asyncio
import json
import os
import secrets
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from kafka_a2a.server.auth import JwtBearerConfig
from kafka_a2a.marketplace_tools import _extract_price
from kafka_a2a.tavily import tavily_search_raw

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


class BulkPriceResearchPayload(BaseModel):
    task_id: str
    currency: str | None = None
    apply: bool = True
    max_products: int = 25
    max_results_per_variant: int = 5


def _product_service_base_url() -> str:
    return (
        os.environ.get("KA2A_PRODUCT_SERVICE_URL")
        or os.environ.get("PRODUCT_SERVICE_URL")
        or os.environ.get("PRODUCT_BACKEND_URL")
        or "http://product:7003"
    ).rstrip("/")


def _http_json_request(
    *,
    url: str,
    method: str = "GET",
    token: str,
    payload: dict[str, Any] | None = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {token}",
    }
    if body is not None:
        headers["content-type"] = "application/json"
    request = UrlRequest(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except Exception:
            detail = raw or str(exc)
        raise HTTPException(status_code=int(exc.code), detail=detail) from exc
    except (TimeoutError, URLError) as exc:
        raise HTTPException(status_code=502, detail=f"Product service request failed: {exc}") from exc


def _iter_bulk_variants(products: list[dict[str, Any]], *, max_products: int) -> list[dict[str, str]]:
    variants: list[dict[str, str]] = []
    for product in products[: max(1, min(max_products, 100))]:
        product_name = str(product.get("name") or "").strip()
        category = str(product.get("category") or "").strip()
        for variant in product.get("variants") or []:
            if not isinstance(variant, dict):
                continue
            variant_id = str(variant.get("id") or "").strip()
            if not variant_id:
                continue
            variant_name = str(variant.get("name") or product_name).strip()
            sku = str(variant.get("sku") or product.get("sku") or "").strip()
            variants.append({
                "product_id": str(product.get("id") or ""),
                "product_name": product_name,
                "variant_id": variant_id,
                "variant_name": variant_name,
                "sku": sku,
                "category": category,
            })
    return variants


def _best_price_from_tavily_response(response: dict[str, Any], *, currency: str) -> dict[str, Any] | None:
    target_currency = (currency or "").strip().upper()
    candidates: list[dict[str, Any]] = []
    for item in response.get("results") or []:
        if not isinstance(item, dict):
            continue
        text = " ".join(
            str(item.get(key) or "")
            for key in ("title", "content", "raw_content")
        )
        price_label, amount, found_currency = _extract_price(text)
        if amount is None or amount <= 0:
            continue
        candidates.append({
            "price_label": price_label,
            "amount": amount,
            "currency": found_currency,
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "score": item.get("score"),
            "matches_requested_currency": bool(found_currency and found_currency.upper() == target_currency),
        })
    requested = [item for item in candidates if item["matches_requested_currency"]]
    pool = requested or candidates
    if not pool:
        return None
    return sorted(pool, key=lambda item: (not item["matches_requested_currency"], item["amount"]))[0]


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

    @router.post("/price-research/bulk-task/")
    async def research_bulk_task_prices(body: BulkPriceResearchPayload, request: Request):
        access = _runtime_access(request)
        if not access.can_interact():
            raise HTTPException(status_code=403, detail="Missing permission: interact_with_agent")

        token = get_bearer_token_from_request(request)
        if not token:
            raise HTTPException(status_code=401, detail="Bearer token is required.")

        tavily_api_key = await run_in_threadpool(
            service.resolve_workspace_tavily_api_key,
            profile_id=access.profile_id,
        )
        if not tavily_api_key:
            raise HTTPException(status_code=400, detail="Tavily API key is not configured for this workspace.")

        currency = (body.currency or "").strip().upper() or "USD"
        task_query = urlencode({"task_id": body.task_id})
        task_url = f"{_product_service_base_url()}/product_api/products/bulk_task_status/?{task_query}"
        task_payload = await asyncio.to_thread(_http_json_request, url=task_url, token=token)
        products = task_payload.get("created_products") if isinstance(task_payload, dict) else None
        if not isinstance(products, list) or not products:
            raise HTTPException(status_code=404, detail="No created products were found for this bulk task.")

        variants = _iter_bulk_variants(products, max_products=body.max_products)
        if not variants:
            raise HTTPException(status_code=404, detail="No product variants were found for this bulk task.")

        results: list[dict[str, Any]] = []
        applied_count = 0
        for variant in variants:
            query_parts = [
                variant["variant_name"] or variant["product_name"],
                variant["category"],
                variant["sku"],
                f"current retail price in {currency}",
                "buy online",
            ]
            query = " ".join(part for part in query_parts if part).strip()
            result: dict[str, Any] = {
                **variant,
                "query": query,
                "currency": currency,
                "status": "not_found",
                "applied": False,
            }
            try:
                tavily_response = await tavily_search_raw(
                    api_key=tavily_api_key,
                    query=query,
                    max_results=max(1, min(body.max_results_per_variant, 10)),
                    search_depth="basic",
                    include_raw_content=True,
                    include_images=False,
                    timeout_s=20.0,
                )
                best_price = _best_price_from_tavily_response(tavily_response, currency=currency)
                if best_price is None:
                    results.append(result)
                    continue

                result.update({
                    "status": "suggested",
                    "suggested_price": f"{best_price['amount']:.2f}",
                    "suggested_currency": best_price.get("currency"),
                    "source_title": best_price.get("title"),
                    "source_url": best_price.get("url"),
                    "matches_requested_currency": best_price.get("matches_requested_currency", False),
                })

                if body.apply and best_price.get("matches_requested_currency"):
                    patch_url = f"{_product_service_base_url()}/product_api/variants/{variant['variant_id']}/"
                    await asyncio.to_thread(
                        _http_json_request,
                        url=patch_url,
                        method="PATCH",
                        token=token,
                        payload={"price_override": f"{best_price['amount']:.2f}"},
                    )
                    result["status"] = "applied"
                    result["applied"] = True
                    applied_count += 1
            except HTTPException:
                raise
            except Exception as exc:
                result.update({"status": "failed", "error": str(exc)})
            results.append(result)

        return {
            "task_id": body.task_id,
            "currency": currency,
            "apply": body.apply,
            "product_count": len(products),
            "variant_count": len(variants),
            "suggested_count": sum(1 for item in results if item.get("suggested_price")),
            "applied_count": applied_count,
            "skipped_count": sum(1 for item in results if item.get("status") in {"not_found", "suggested"}),
            "failed_count": sum(1 for item in results if item.get("status") == "failed"),
            "results": results,
        }

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
