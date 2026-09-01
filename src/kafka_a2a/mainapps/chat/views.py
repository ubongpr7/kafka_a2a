from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from kafka_a2a.client import Ka2aClient
from kafka_a2a.intera_coins import A2ACompletionChargeTracker, spend_intera_coins_for_a2a_completion
from kafka_a2a.memory import KA2A_CONVERSATION_HISTORY_METADATA_KEY
from kafka_a2a.mainapps.agents.services import AgentControlPlaneService
from kafka_a2a.mainapps.agents.services import AgentRuntimeAccessContext
from kafka_a2a.models import Message, TaskConfiguration, TextPart
from kafka_a2a.server.auth import JwtBearerConfig, JwtVerificationError, parse_authorization_header, verify_bearer_jwt
from kafka_a2a.tenancy import with_principal

from ..common.auth import build_agent_auth_context
from .models import ConversationMessageKind, ConversationMessageRole, ConversationStatus
from .storage import ConversationStore


class ConversationCreateRequest(BaseModel):
    agent_slug: str
    title: str | None = None
    history_length: int | None = None


class ConversationPatchRequest(BaseModel):
    title: str | None = None
    status: str | None = None
    history_length: int | None = None


@dataclass(slots=True)
class ChatRouterDependencies:
    client: Ka2aClient
    chat_store: ConversationStore
    control_plane: AgentControlPlaneService
    default_agent: str
    jwt: JwtBearerConfig | None


def build_chat_router(*, deps: ChatRouterDependencies) -> APIRouter:
    router = APIRouter(tags=["chat"])

    async def _principal_from_token(token: str, context_token: str | None = None):
        auth_context = build_agent_auth_context(token=token, jwt=deps.jwt, context_token=context_token)
        principal = verify_bearer_jwt(token=token, config=deps.jwt)  # type: ignore[arg-type]
        claims = dict(principal.claims or {})
        overrides = await run_in_threadpool(
            deps.control_plane.build_principal_claim_overrides,
            profile_id=auth_context.profile_id,
        )
        claims.update(overrides)
        principal.claims = claims
        return principal

    def _auth_context_from_authorization(authorization: str, context_token: str | None = None):
        try:
            token = parse_authorization_header(authorization)
        except JwtVerificationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return build_agent_auth_context(token=token, jwt=deps.jwt, context_token=context_token)

    def _authorization_from_request(request: Request) -> str | None:
        value = request.headers.get("authorization")
        if value and value.strip():
            return value.strip()
        return None

    def _parse_access_token_cookie(cookie_header: str | None) -> str | None:
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            return None
        for name, morsel in cookie.items():
            if name == "accessToken" or name.endswith("accessToken"):
                value = morsel.value.strip()
                if value:
                    return value
        return None

    def _token_from_websocket(websocket: WebSocket) -> str | None:
        query_token = (websocket.query_params.get("ws_ticket") or "").strip()
        if query_token:
            return query_token
        return None

    def _runtime_access_from_authorization(authorization: str):
        auth = _auth_context_from_authorization(authorization)
        return auth

    async def _workspace_registry_from_authorization(authorization: str) -> dict[str, Any]:
        auth = _runtime_access_from_authorization(authorization)
        access = AgentRuntimeAccessContext(
            user_id=auth.user_id,
            profile_id=auth.profile_id,
            is_owner=auth.is_workspace_owner,
            permissions=auth.permissions,
        )
        if not access.can_interact():
            raise HTTPException(status_code=403, detail="Missing permission: interact_with_agent")
        return await run_in_threadpool(deps.control_plane.runtime_registry, access=access)

    async def _workspace_agent_config_from_authorization(authorization: str, public_agent_name: str) -> dict[str, Any]:
        auth = _runtime_access_from_authorization(authorization)
        access = AgentRuntimeAccessContext(
            user_id=auth.user_id,
            profile_id=auth.profile_id,
            is_owner=auth.is_workspace_owner,
            permissions=auth.permissions,
        )
        if not access.can_interact():
            raise HTTPException(status_code=403, detail="Missing permission: interact_with_agent")
        try:
            return await run_in_threadpool(deps.control_plane.runtime_agent_config, access=access, slug=public_agent_name)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def _extract_text_parts(parts: Any) -> str:
        if not isinstance(parts, list):
            return ""
        text_parts: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if str(part.get("kind") or "").strip() != "text":
                continue
            text = str(part.get("text") or "").strip()
            if text:
                text_parts.append(text)
        return "\n".join(text_parts).strip()

    def _extract_primary_data(parts: Any) -> dict[str, Any] | None:
        if not isinstance(parts, list):
            return None
        for part in parts:
            if not isinstance(part, dict):
                continue
            if str(part.get("kind") or "").strip() != "data":
                continue
            data = part.get("data")
            if isinstance(data, dict) and data:
                return data
        return None

    def _extract_message_payload(parts: Any) -> tuple[str, dict[str, Any] | None]:
        text = _extract_text_parts(parts)
        structured = _extract_primary_data(parts)
        return text, structured

    def _latest_pending_interaction_payload(messages: list[Any]) -> dict[str, Any] | None:
        for message in reversed(messages):
            if str(getattr(message, "role", "") or "").strip() != ConversationMessageRole.assistant.value:
                continue
            payload = getattr(message, "structured_payload", None)
            if not isinstance(payload, dict) or not payload:
                continue
            interaction_type = str(payload.get("interaction_type") or payload.get("type") or "").strip().lower()
            if interaction_type:
                return payload
        return None

    def _looks_like_structured_interaction_response(text: str) -> bool:
        raw = str(text or "").strip()
        if not raw.startswith("{"):
            return False
        try:
            payload = json.loads(raw)
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        interaction_type = str(payload.get("interaction_type") or payload.get("type") or "").strip().lower()
        if interaction_type.endswith("_response"):
            return True
        return any(key in payload for key in ("selected", "response", "responses", "additional_input"))

    def _pending_interaction_requires_structured_reply(payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict) or not payload:
            return False
        interaction_type = str(payload.get("interaction_type") or payload.get("type") or "").strip().lower()
        if not interaction_type:
            return False
        if interaction_type == "multiple_choice":
            return not bool(payload.get("allow_input") or payload.get("allow_additional_input"))
        return True

    def _history_content_for_message(message) -> str:
        content = str(message.content or "").strip()
        if content:
            return content
        structured_payload = message.structured_payload if isinstance(message.structured_payload, dict) else {}
        if structured_payload:
            try:
                return json.dumps(structured_payload, ensure_ascii=False)
            except Exception:
                return ""
        return ""

    async def _build_history_metadata(
        *,
        conversation_id: str,
        profile_id: str,
        user_id: str,
        history_length: int | None,
    ) -> list[dict[str, Any]]:
        limit = max(0, int(history_length)) if history_length is not None else None
        detail = await deps.chat_store.get_conversation_detail(
            conversation_id=conversation_id,
            profile_id=profile_id,
            user_id=user_id,
            message_limit=limit,
        )
        if detail is None:
            return []

        history: list[dict[str, Any]] = []
        for item in detail.messages:
            role = str(item.role or "").strip().lower()
            if role not in {"user", "assistant", "system"}:
                continue
            content = _history_content_for_message(item)
            structured_payload = item.structured_payload if isinstance(item.structured_payload, dict) else None
            if not content and not structured_payload:
                continue
            entry: dict[str, Any] = {"role": role, "content": content}
            if structured_payload:
                entry["structured_payload"] = structured_payload
            history.append(entry)
        return history

    async def _append_activity(
        *,
        websocket: WebSocket | None,
        conversation,
        profile_id: str,
        user_id: str,
        kind: str,
        label: str,
        detail: str | None = None,
        state: str | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        specialist_slug: str | None = None,
    ) -> None:
        activity = await deps.chat_store.append_activity(
            conversation_id=conversation.id,
            profile_id=profile_id,
            user_id=user_id,
            kind=kind,
            label=label,
            detail=detail,
            state=state,
            task_id=task_id,
            context_id=context_id,
            specialist_slug=specialist_slug,
        )
        if websocket is not None and activity is not None:
            await websocket.send_json(
                {
                    "type": "activity.created",
                    "activity": activity.model_dump(mode="json", by_alias=True, exclude_none=True),
                }
            )

    async def _save_conversation_update(websocket: WebSocket | None, conversation) -> None:
        stored = await deps.chat_store.save_conversation(conversation)
        if websocket is not None:
            await websocket.send_json(
                {
                    "type": "conversation.updated",
                    "conversation": stored.model_dump(mode="json", by_alias=True, exclude_none=True),
                }
            )

    async def _append_assistant_message(
        *,
        websocket: WebSocket | None,
        conversation,
        profile_id: str,
        user_id: str,
        content: str,
        structured_payload: dict[str, Any] | None,
        task_id: str | None,
        context_id: str | None,
        server_message_id: str | None,
        kind: str,
    ) -> None:
        message = await deps.chat_store.append_message(
            conversation_id=conversation.id,
            profile_id=profile_id,
            user_id=user_id,
            role=ConversationMessageRole.assistant.value,
            kind=kind,
            content=content,
            structured_payload=structured_payload,
            task_id=task_id,
            context_id=context_id,
            server_message_id=server_message_id,
        )
        refreshed = await deps.chat_store.get_conversation(
            conversation_id=conversation.id,
            profile_id=profile_id,
            user_id=user_id,
        )
        if websocket is not None:
            await websocket.send_json(
                {
                    "type": "message.created",
                    "message": message.model_dump(mode="json", by_alias=True, exclude_none=True),
                }
            )
            if refreshed is not None:
                await websocket.send_json(
                    {
                        "type": "conversation.updated",
                        "conversation": refreshed.model_dump(mode="json", by_alias=True, exclude_none=True),
                    }
                )

    @router.get("/conversations")
    async def list_conversations(request: Request) -> Any:
        authorization = _authorization_from_request(request)
        if not authorization:
            raise HTTPException(status_code=401, detail="Authorization header is required.")
        auth = _auth_context_from_authorization(authorization, request.headers.get("x-intera-authorization-context"))
        status = request.query_params.get("status")
        limit_raw = request.query_params.get("limit")
        limit = int(limit_raw) if limit_raw and limit_raw.isdigit() else None
        conversations = await deps.chat_store.list_conversations(
            profile_id=auth.profile_id,
            user_id=auth.user_id,
            status=status,
            limit=limit,
        )
        return [item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in conversations]

    @router.post("/conversations")
    async def create_conversation(request: Request, body: ConversationCreateRequest) -> Any:
        authorization = _authorization_from_request(request)
        if not authorization:
            raise HTTPException(status_code=401, detail="Authorization header is required.")
        auth = _auth_context_from_authorization(authorization, request.headers.get("x-intera-authorization-context"))
        agent_config = await _workspace_agent_config_from_authorization(authorization, body.agent_slug)
        agent_slug = str(agent_config.get("slug") or body.agent_slug).strip()
        agent_name = str(agent_config.get("name") or agent_slug).strip() or agent_slug
        runtime_agent_name = str(agent_config.get("runtime_name") or agent_slug).strip() or agent_slug
        history_length = body.history_length if body.history_length is not None else 10
        conversation = await deps.chat_store.create_conversation(
            profile_id=auth.profile_id,
            user_id=auth.user_id,
            agent_slug=agent_slug,
            agent_name=agent_name,
            agent_icon_url=str(agent_config.get("icon_url") or "").strip(),
            runtime_agent_name=runtime_agent_name,
            title=(body.title or "").strip() or f"{agent_name} conversation",
            history_length=history_length,
        )
        detail = await deps.chat_store.get_conversation_detail(
            conversation_id=conversation.id,
            profile_id=auth.profile_id,
            user_id=auth.user_id,
        )
        if detail is None:
            raise HTTPException(status_code=500, detail="Conversation was created but could not be loaded.")
        return detail.model_dump(mode="json", by_alias=True, exclude_none=True)

    @router.get("/conversations/{conversation_id}")
    async def get_conversation(request: Request, conversation_id: str) -> Any:
        authorization = _authorization_from_request(request)
        if not authorization:
            raise HTTPException(status_code=401, detail="Authorization header is required.")
        auth = _auth_context_from_authorization(authorization, request.headers.get("x-intera-authorization-context"))
        detail = await deps.chat_store.get_conversation_detail(
            conversation_id=conversation_id,
            profile_id=auth.profile_id,
            user_id=auth.user_id,
        )
        if detail is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        return detail.model_dump(mode="json", by_alias=True, exclude_none=True)

    @router.patch("/conversations/{conversation_id}")
    async def update_conversation(request: Request, conversation_id: str, body: ConversationPatchRequest) -> Any:
        authorization = _authorization_from_request(request)
        if not authorization:
            raise HTTPException(status_code=401, detail="Authorization header is required.")
        auth = _auth_context_from_authorization(authorization, request.headers.get("x-intera-authorization-context"))
        conversation = await deps.chat_store.get_conversation(
            conversation_id=conversation_id,
            profile_id=auth.profile_id,
            user_id=auth.user_id,
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        if body.title is not None:
            conversation.title = body.title.strip()
        if body.status is not None:
            normalized_status = body.status.strip().lower()
            if normalized_status not in {item.value for item in ConversationStatus}:
                raise HTTPException(status_code=400, detail="Invalid conversation status.")
            conversation.status = normalized_status
        if body.history_length is not None:
            conversation.history_length = max(0, min(int(body.history_length), 100))
        updated = await deps.chat_store.save_conversation(conversation)
        return updated.model_dump(mode="json", by_alias=True, exclude_none=True)

    @router.delete("/conversations/{conversation_id}")
    async def delete_conversation(request: Request, conversation_id: str) -> Any:
        authorization = _authorization_from_request(request)
        if not authorization:
            raise HTTPException(status_code=401, detail="Authorization header is required.")
        auth = _auth_context_from_authorization(authorization, request.headers.get("x-intera-authorization-context"))
        deleted = await deps.chat_store.delete_conversation(
            conversation_id=conversation_id,
            profile_id=auth.profile_id,
            user_id=auth.user_id,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        return {"deleted": True, "conversationId": conversation_id}

    async def _run_conversation_stream(
        *,
        websocket: WebSocket,
        authorization: str,
        principal,
        profile_id: str,
        conversation,
        text: str,
        history_length: int | None = None,
    ) -> None:
        refreshed_config = await _workspace_agent_config_from_authorization(authorization, conversation.agent_slug)
        runtime_agent_name = str(refreshed_config.get("runtime_name") or conversation.runtime_agent_name).strip()
        conversation.runtime_agent_name = runtime_agent_name or conversation.runtime_agent_name
        conversation.agent_name = str(refreshed_config.get("name") or conversation.agent_name).strip() or conversation.agent_name
        conversation.agent_icon_url = str(refreshed_config.get("icon_url") or conversation.agent_icon_url).strip()
        await _save_conversation_update(websocket, conversation)
        resume_task_id = conversation.resume_task_id if conversation.awaiting_input else None
        if resume_task_id:
            detail = await deps.chat_store.get_conversation_detail(
                conversation_id=conversation.id,
                profile_id=profile_id,
                user_id=principal.user_id,
                message_limit=8,
            )
            pending_interaction = (
                _latest_pending_interaction_payload(detail.messages)
                if detail is not None
                else None
            )
            if (
                _pending_interaction_requires_structured_reply(pending_interaction)
                and not _looks_like_structured_interaction_response(text)
            ):
                resume_task_id = None
        context_id = conversation.last_context_id

        user_message = await deps.chat_store.append_message(
            conversation_id=conversation.id,
            profile_id=profile_id,
            user_id=principal.user_id,
            role=ConversationMessageRole.user.value,
            kind=ConversationMessageKind.text.value,
            content=text,
            task_id=resume_task_id,
            context_id=context_id,
        )
        conversation = await deps.chat_store.get_conversation(
            conversation_id=conversation.id,
            profile_id=profile_id,
            user_id=principal.user_id,
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        if not conversation.title:
            conversation.title = text[:80].strip()
        conversation.awaiting_input = False
        conversation.resume_task_id = None
        conversation.current_task_state = "working"
        if not resume_task_id:
            conversation.active_specialist_slug = None
        await _append_activity(
            websocket=websocket,
            conversation=conversation,
            profile_id=profile_id,
            user_id=principal.user_id,
            kind="user-message",
            label="User message submitted",
            detail=text[:180].strip(),
            state="working",
            task_id=resume_task_id,
            context_id=context_id,
        )
        await _save_conversation_update(websocket, conversation)

        await websocket.send_json(
            {
                "type": "message.created",
                "message": user_message.model_dump(mode="json", by_alias=True, exclude_none=True),
            }
        )
        await websocket.send_json({"type": "typing.started"})

        message = Message(role="user", parts=[TextPart(text=text)], context_id=context_id)
        configuration = TaskConfiguration(
            history_length=history_length if history_length is not None else conversation.history_length
        )
        metadata = with_principal({}, principal)
        metadata[KA2A_CONVERSATION_HISTORY_METADATA_KEY] = await _build_history_metadata(
            conversation_id=conversation.id,
            profile_id=profile_id,
            user_id=principal.user_id,
            history_length=configuration.history_length,
        )

        if resume_task_id:
            stream = await deps.client.continue_task_stream(
                agent_name=runtime_agent_name,
                task_id=resume_task_id,
                message=message,
                configuration=configuration,
                metadata=metadata,
            )
        else:
            stream = await deps.client.stream_message(
                agent_name=runtime_agent_name,
                message=message,
                configuration=configuration,
                metadata=metadata,
            )

        assistant_persisted = False
        charge_tracker = A2ACompletionChargeTracker(
            profile_id=profile_id,
            conversation_id=conversation.id,
            prompt_text=text,
        )
        try:
            async for event in stream:
                payload: Any = event
                if hasattr(event, "model_dump"):
                    payload = event.model_dump(mode="json", by_alias=True, exclude_none=True)
                await websocket.send_json({"type": "task.event", "event": payload})

                kind = str(getattr(event, "kind", "") or payload.get("kind") or "").strip()
                if kind == "task":
                    conversation.last_task_id = str(getattr(event, "id", "") or payload.get("id") or "").strip() or conversation.last_task_id
                    conversation.last_context_id = (
                        str(getattr(event, "context_id", "") or payload.get("contextId") or "").strip() or conversation.last_context_id
                    )
                    status_obj = getattr(event, "status", None)
                    state_value = str(getattr(status_obj, "state", "") or payload.get("status", {}).get("state") or "").strip()
                    conversation.current_task_state = state_value or conversation.current_task_state
                    await _append_activity(
                        websocket=websocket,
                        conversation=conversation,
                        profile_id=profile_id,
                        user_id=principal.user_id,
                        kind="task-created",
                        label="Task created",
                        state=state_value or conversation.current_task_state,
                        task_id=conversation.last_task_id,
                        context_id=conversation.last_context_id,
                    )
                    await _save_conversation_update(websocket, conversation)
                    continue

                if kind == "status-update":
                    task_id = str(getattr(event, "task_id", "") or payload.get("taskId") or "").strip() or None
                    context_id = str(getattr(event, "context_id", "") or payload.get("contextId") or "").strip() or None
                    status_obj = getattr(event, "status", None)
                    state_value = str(getattr(status_obj, "state", "") or payload.get("status", {}).get("state") or "").strip()
                    message_obj = getattr(status_obj, "message", None) if status_obj is not None else None
                    message_payload = payload.get("status", {}).get("message") if isinstance(payload, dict) else {}
                    if message_obj is not None and hasattr(message_obj, "model_dump"):
                        message_payload = message_obj.model_dump(mode="json", by_alias=True, exclude_none=True)
                    text_value, structured_payload = _extract_message_payload(
                        message_payload.get("parts") if isinstance(message_payload, dict) else None
                    )
                    message_role = str(message_payload.get("role") or "").strip() if isinstance(message_payload, dict) else ""
                    server_message_id = (
                        str(message_payload.get("messageId") or "").strip() if isinstance(message_payload, dict) else None
                    ) or None
                    is_final = bool(getattr(event, "final", False) or payload.get("final"))
                    conversation.last_task_id = task_id or conversation.last_task_id
                    conversation.last_context_id = context_id or conversation.last_context_id
                    conversation.current_task_state = state_value or conversation.current_task_state
                    if is_final:
                        awaiting_input = state_value in {"input-required", "auth-required"}
                        conversation.awaiting_input = awaiting_input
                        conversation.resume_task_id = task_id if awaiting_input else None
                    await _append_activity(
                        websocket=websocket,
                        conversation=conversation,
                        profile_id=profile_id,
                        user_id=principal.user_id,
                        kind="status-update",
                        label=text_value or f"Task status: {state_value or 'updated'}",
                        detail=f"State: {state_value}" if state_value and text_value else None,
                        state=state_value or conversation.current_task_state,
                        task_id=task_id,
                        context_id=context_id,
                    )
                    await _save_conversation_update(websocket, conversation)
                    if text_value and message_role != "user":
                        await websocket.send_json(
                            {
                                "type": "task.status",
                                "state": state_value,
                                "text": text_value,
                                "final": is_final,
                            }
                        )
                    if is_final and not assistant_persisted and message_role != "user" and (text_value or structured_payload):
                        await _append_assistant_message(
                            websocket=websocket,
                            conversation=conversation,
                            profile_id=profile_id,
                            user_id=principal.user_id,
                            content=text_value,
                            structured_payload=structured_payload,
                            task_id=task_id,
                            context_id=context_id,
                            server_message_id=server_message_id,
                            kind=ConversationMessageKind.structured.value if structured_payload else ConversationMessageKind.text.value,
                        )
                        assistant_persisted = True
                    pending_charge = charge_tracker.evaluate(payload if isinstance(payload, dict) else None)
                    if pending_charge is not None:
                        await run_in_threadpool(
                            spend_intera_coins_for_a2a_completion,
                            profile_id=pending_charge.profile_id,
                            task_id=pending_charge.task_id,
                            conversation_id=pending_charge.conversation_id,
                            prompt_text=pending_charge.prompt_text,
                        )
                    continue

                if kind == "artifact-update":
                    task_id = str(getattr(event, "task_id", "") or payload.get("taskId") or "").strip() or None
                    context_id = str(getattr(event, "context_id", "") or payload.get("contextId") or "").strip() or None
                    artifact = getattr(event, "artifact", None)
                    artifact_payload = payload.get("artifact") if isinstance(payload, dict) else {}
                    if artifact is not None and hasattr(artifact, "model_dump"):
                        artifact_payload = artifact.model_dump(mode="json", by_alias=True, exclude_none=True)
                    if not isinstance(artifact_payload, dict):
                        artifact_payload = {}
                    artifact_name = str(artifact_payload.get("name") or "").strip()
                    text_value, structured_payload = _extract_message_payload(artifact_payload.get("parts"))
                    if artifact_name == "delegation":
                        data_payload = structured_payload or {}
                        selected_agent = str(data_payload.get("selectedAgent") or "").strip()
                        if selected_agent:
                            conversation.active_specialist_slug = selected_agent
                            await _append_activity(
                                websocket=websocket,
                                conversation=conversation,
                                profile_id=profile_id,
                                user_id=principal.user_id,
                                kind="delegation",
                                label=f"Delegated to {selected_agent.replace('_', ' ')}",
                                detail=text_value or None,
                                task_id=task_id,
                                context_id=context_id,
                                specialist_slug=selected_agent,
                            )
                            await _save_conversation_update(websocket, conversation)
                    if artifact_name == "result" and not assistant_persisted and (text_value or structured_payload):
                        await _append_activity(
                            websocket=websocket,
                            conversation=conversation,
                            profile_id=profile_id,
                            user_id=principal.user_id,
                            kind="result",
                            label="Result ready",
                            detail=text_value[:180].strip() if text_value else None,
                            task_id=task_id,
                            context_id=context_id,
                            specialist_slug=conversation.active_specialist_slug,
                        )
                        await _save_conversation_update(websocket, conversation)
                        await _append_assistant_message(
                            websocket=websocket,
                            conversation=conversation,
                            profile_id=profile_id,
                            user_id=principal.user_id,
                            content=text_value,
                            structured_payload=structured_payload,
                            task_id=task_id,
                            context_id=context_id,
                            server_message_id=None,
                            kind=ConversationMessageKind.structured.value if structured_payload else ConversationMessageKind.text.value,
                        )
                        assistant_persisted = True
                    charge_tracker.evaluate(payload if isinstance(payload, dict) else None)
            await websocket.send_json({"type": "typing.stopped"})
        except Exception:
            await websocket.send_json({"type": "typing.stopped"})
            raise

    @router.websocket("/ws/conversations/{conversation_id}")
    async def conversation_socket(websocket: WebSocket, conversation_id: str) -> None:
        token = _token_from_websocket(websocket)
        if not token:
            await websocket.close(code=4401, reason="Missing access token.")
            return
        try:
            authorization = f"Bearer {token}"
            auth = _auth_context_from_authorization(
                authorization,
                websocket.headers.get("x-intera-authorization-context")
                or websocket.query_params.get("ws_ticket"),
            )
            detail = await deps.chat_store.get_conversation_detail(
                conversation_id=conversation_id,
                profile_id=auth.profile_id,
                user_id=auth.user_id,
            )
            if detail is None:
                await websocket.close(code=4404, reason="Conversation not found.")
                return
            await websocket.accept()
            await websocket.send_json(
                {
                    "type": "conversation.snapshot",
                    "conversation": detail.conversation.model_dump(mode="json", by_alias=True, exclude_none=True),
                    "messages": [message.model_dump(mode="json", by_alias=True, exclude_none=True) for message in detail.messages],
                    "activities": [activity.model_dump(mode="json", by_alias=True, exclude_none=True) for activity in detail.activities],
                }
            )
            send_lock = asyncio.Lock()
            while True:
                payload = await websocket.receive_json()
                if not isinstance(payload, dict):
                    await websocket.send_json({"type": "error", "message": "Invalid websocket payload."})
                    continue
                message_type = str(payload.get("type") or "").strip()
                if message_type == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue
                if message_type != "message.send":
                    await websocket.send_json({"type": "error", "message": "Unsupported websocket message type."})
                    continue
                text = str(payload.get("text") or "").strip()
                if not text:
                    await websocket.send_json({"type": "error", "message": "Message text is required."})
                    continue
                history_length = payload.get("historyLength")
                history_value = int(history_length) if isinstance(history_length, int) else None
                if send_lock.locked():
                    await websocket.send_json({"type": "error", "message": "A message is already being processed."})
                    continue
                async with send_lock:
                    conversation = await deps.chat_store.get_conversation(
                        conversation_id=conversation_id,
                        profile_id=auth.profile_id,
                        user_id=auth.user_id,
                    )
                    if conversation is None:
                        await websocket.send_json({"type": "error", "message": "Conversation not found."})
                        continue
                    principal = await _principal_from_token(
                        token,
                        websocket.headers.get("x-intera-authorization-context")
                        or websocket.query_params.get("ws_ticket"),
                    )
                    await _run_conversation_stream(
                        websocket=websocket,
                        authorization=authorization,
                        principal=principal,
                        profile_id=auth.profile_id,
                        conversation=conversation,
                        text=text,
                        history_length=history_value,
                    )
        except WebSocketDisconnect:
            return
        except HTTPException as exc:
            if websocket.client_state.name.lower() != "connected":
                await websocket.close(code=4400, reason=str(exc.detail))
            else:
                await websocket.send_json({"type": "error", "message": str(exc.detail)})
                await websocket.close(code=4400, reason=str(exc.detail))
        except Exception as exc:
            if websocket.client_state.name.lower() != "connected":
                await websocket.close(code=1011, reason=str(exc))
            else:
                await websocket.send_json({"type": "error", "message": str(exc)})
                await websocket.close(code=1011, reason="Agent chat stream failed.")

    return router
