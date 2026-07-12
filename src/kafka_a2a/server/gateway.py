import base64
import json
import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from typing import NoReturn
from uuid import uuid4

from kafka_a2a.agent_filter import filter_agent_cards
from kafka_a2a.client import Ka2aClient, Ka2aClientConfig
from kafka_a2a.core.config import A2AAppSettings
from kafka_a2a.errors import A2AError, A2AErrorCode
from kafka_a2a.intera_coins import A2ACompletionChargeTracker, spend_intera_coins_for_a2a_completion
from kafka_a2a.mainapps.agents.storage import build_agent_control_plane_store
from kafka_a2a.mainapps.agents.services import AgentControlPlaneService, AgentRuntimeAccessContext
from kafka_a2a.mainapps.chat.storage import build_conversation_store
from kafka_a2a.mainapps.agents.urls import build_urlpatterns as build_agent_urlpatterns
from kafka_a2a.mainapps.chat.urls import build_urlpatterns as build_chat_urlpatterns
from kafka_a2a.mainapps.chat.views import ChatRouterDependencies
from kafka_a2a.mainapps.common.auth import build_agent_auth_context
from kafka_a2a.memory import KA2A_CONVERSATION_HISTORY_METADATA_KEY
from kafka_a2a.models import FilePart, FileWithBytes, Ka2aModel, Message, TaskConfiguration, TextPart
from kafka_a2a.ops import ensure_trace_metadata, metrics_enabled, metrics_snapshot
from kafka_a2a.protocol import METHOD_TASKS_LIST, TaskListParams, TaskListResult
from kafka_a2a.registry.directory import KafkaAgentDirectory, KafkaAgentDirectoryConfig
from kafka_a2a.registry.kafka_registry import KafkaAgentRegistry
from kafka_a2a.server.auth import JwtBearerConfig, JwtVerificationError, parse_authorization_header, verify_bearer_jwt
from kafka_a2a.tenancy import with_principal
from kafka_a2a.transport.kafka import KafkaConfig, KafkaTransport


def _require_fastapi() -> Any:
    try:
        from fastapi import FastAPI  # noqa: F401
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "FastAPI server extras not installed. Install the `server` extra (e.g. `uv sync --extra server`)."
        ) from exc
    from fastapi import FastAPI

    return FastAPI


@dataclass(slots=True)
class GatewayConfig:
    bootstrap_servers: str
    default_agent: str
    client_id: str | None = None
    request_timeout_s: float | None = None
    stream_request_timeout_s: float | None = 120.0
    jwt: JwtBearerConfig | None = None


_DEFAULT_CORS_ALLOW_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
_LOGGER = logging.getLogger(__name__)


def _parse_csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def create_gateway_app(config: GatewayConfig):
    FastAPI = _require_fastapi()
    from fastapi import File, Form, HTTPException, Query, Request, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, StreamingResponse
    from starlette.concurrency import run_in_threadpool

    transport = KafkaTransport(
        KafkaConfig.from_env(bootstrap_servers=config.bootstrap_servers, client_id=config.client_id)
    )
    client = Ka2aClient(
        transport=transport,
        config=Ka2aClientConfig(client_id=config.client_id, request_timeout_s=config.request_timeout_s),
    )
    registry = KafkaAgentRegistry(transport=transport, sender=config.client_id or "gateway")

    directory = KafkaAgentDirectory(
        registry=registry,
        config=KafkaAgentDirectoryConfig(
            group_id=f"ka2a.gateway.directory.{uuid4()}",
            auto_offset_reset="earliest",
            entry_ttl_s=float(os.getenv("KA2A_DIRECTORY_ENTRY_TTL_S") or "300") or None,
        ),
    )
    app_settings = A2AAppSettings.from_env()
    app_settings.ensure_dirs()
    agent_control_plane = AgentControlPlaneService(
        store=build_agent_control_plane_store(app_settings),
        settings=app_settings,
    )
    agent_control_plane.ensure_seeded()
    chat_store = build_conversation_store(app_settings)

    @asynccontextmanager
    async def _lifespan(_app):
        await client.start()
        await directory.start()
        try:
            yield
        finally:
            await directory.stop()
            await client.stop()
            await chat_store.aclose()

    app = FastAPI(title="K-A2A Gateway", version="0.1.0", lifespan=_lifespan)
    app.include_router(
        build_agent_urlpatterns(
            service=agent_control_plane,
            jwt=config.jwt,
            runtime_shared_token=os.getenv("KA2A_RUNTIME_SHARED_TOKEN"),
        )
    )
    app.include_router(
        build_chat_urlpatterns(
            deps=ChatRouterDependencies(
                client=client,
                chat_store=chat_store,
                control_plane=agent_control_plane,
                default_agent=config.default_agent,
                jwt=config.jwt,
            )
        )
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_parse_csv_env("KA2A_CORS_ALLOW_ORIGINS"),
        allow_origin_regex=os.environ.get("KA2A_CORS_ALLOW_ORIGIN_REGEX", _DEFAULT_CORS_ALLOW_ORIGIN_REGEX),
        allow_credentials=_parse_bool_env("KA2A_CORS_ALLOW_CREDENTIALS", default=False),
        allow_methods=_parse_csv_env("KA2A_CORS_ALLOW_METHODS") or ["GET", "POST", "OPTIONS"],
        allow_headers=_parse_csv_env("KA2A_CORS_ALLOW_HEADERS")
        or ["Authorization", "Content-Type", "X-Requested-With", "X-Profile-ID", "X-Company-Code"],
    )
    class ChatRequest(Ka2aModel):
        text: str
        agent_name: str | None = None
        context_id: str | None = None
        history_length: int | None = None
        history: list[dict[str, Any]] | None = None

    class TaskContinueRequest(Ka2aModel):
        text: str
        history_length: int | None = None
        history: list[dict[str, Any]] | None = None

    def _sanitize_direct_stream_history(history: Any, history_length: int | None) -> list[dict[str, Any]]:
        if not isinstance(history, list):
            return []
        limit = max(0, min(int(history_length), 100)) if isinstance(history_length, int) else 20
        entries: list[dict[str, Any]] = []
        for item in history[-limit:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in {"user", "assistant", "system"}:
                continue
            content = item.get("content")
            content_text = content.strip() if isinstance(content, str) else ""
            structured_payload = item.get("structured_payload") or item.get("structuredPayload")
            entry: dict[str, Any] = {"role": role, "content": content_text}
            if isinstance(structured_payload, dict) and structured_payload:
                entry["structured_payload"] = structured_payload
            if content_text or entry.get("structured_payload"):
                entries.append(entry)
        return entries

    def _attach_direct_stream_history(metadata: dict[str, Any] | None, body: Any) -> dict[str, Any] | None:
        history = _sanitize_direct_stream_history(getattr(body, "history", None), getattr(body, "history_length", None))
        _LOGGER.info(
            "ka2a direct stream history received count=%s raw_is_list=%s roles=%s",
            len(history),
            isinstance(getattr(body, "history", None), list),
            [str(item.get("role") or "") for item in history[-6:]],
        )
        if not history:
            return metadata
        merged = dict(metadata or {})
        merged[KA2A_CONVERSATION_HISTORY_METADATA_KEY] = history
        return merged

    def _stream_payload_from_event(event: Any) -> dict[str, Any]:
        payload: Any = event
        if hasattr(event, "model_dump"):
            payload = event.model_dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(payload, dict):
            return payload
        return {"kind": "status-update", "status": {"state": "working"}}

    def _gateway_error_response(exc: Exception, *, action: str) -> tuple[int, str]:
        if isinstance(exc, TimeoutError):
            return 504, "Agent did not respond in time"
        if isinstance(exc, HTTPException):
            return exc.status_code, str(exc.detail)
        if isinstance(exc, A2AError):
            if exc.code == A2AErrorCode.UNAUTHENTICATED:
                return 401, exc.message or "Unauthenticated"
            if exc.code == A2AErrorCode.PERMISSION_DENIED:
                return 403, exc.message or "Permission denied"
            if exc.code in {
                A2AErrorCode.TASK_NOT_FOUND,
                A2AErrorCode.METHOD_NOT_FOUND,
            }:
                return 404, exc.message or "Requested resource was not found"
            if exc.code in {
                A2AErrorCode.INVALID_REQUEST,
                A2AErrorCode.INVALID_PARAMS,
                A2AErrorCode.UNSUPPORTED_OPERATION,
                A2AErrorCode.CONTENT_TYPE_NOT_SUPPORTED,
            }:
                return 400, exc.message or f"Unable to {action}."
            if exc.code == A2AErrorCode.AUTHENTICATED_EXTENDED_CARD_NOT_CONFIGURED:
                return 501, exc.message or f"Unable to {action}."
            if exc.code == A2AErrorCode.INVALID_AGENT_RESPONSE:
                return 502, exc.message or "Agent returned an invalid response."

        detail = str(exc).strip() or f"Unable to {action}."
        lowered = detail.lower()

        if "missing permission" in lowered:
            return 403, detail
        if "not found" in lowered or "was not found" in lowered:
            return 404, detail
        if "kafkaconnectionerror" in lowered or "requesttimedouterror" in lowered or "no brokers available" in lowered:
            return 503, "A2A gateway is temporarily unavailable while the agent transport reconnects."
        return 500, detail

    def _raise_gateway_http_error(exc: Exception, *, action: str) -> NoReturn:
        if isinstance(exc, A2AError):
            _LOGGER.warning(
                "A2A gateway %s failed with agent error code=%s message=%s data=%s",
                action,
                exc.code,
                exc.message,
                exc.data,
            )
        if isinstance(exc, HTTPException):
            raise exc
        if not isinstance(exc, A2AError):
            _LOGGER.exception("A2A gateway %s failed", action)
        status_code, detail = _gateway_error_response(exc, action=action)
        raise HTTPException(status_code=status_code, detail=detail) from exc

    def _stream_failure_event(*, detail: str, task_id: str | None = None, context_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": "status-update",
            "status": {
                "state": "failed",
                "message": {
                    "role": "assistant",
                    "parts": [{"kind": "text", "text": detail}],
                },
            },
            "final": True,
        }
        if task_id:
            payload["taskId"] = task_id
        if context_id:
            payload["contextId"] = context_id
        return payload

    async def _principal_from_token(token: str):
        auth_context = build_agent_auth_context(token=token, jwt=config.jwt)
        principal = verify_bearer_jwt(token=token, config=config.jwt)  # type: ignore[arg-type]
        claims = dict(principal.claims or {})
        overrides = await run_in_threadpool(
            agent_control_plane.build_principal_claim_overrides,
            profile_id=auth_context.profile_id,
        )
        claims.update(overrides)
        principal.claims = claims
        return principal

    def _authorization_from_request(request: Request) -> str | None:
        value = request.headers.get("authorization")
        if value and value.strip():
            return value.strip()
        return None

    def _token_from_request(request: Request) -> str | None:
        authorization = _authorization_from_request(request)
        if not authorization:
            return None
        return parse_authorization_header(authorization)

    async def _require_request_metadata(request: Request) -> dict[str, Any] | None:
        if config.jwt is None:
            return ensure_trace_metadata(None, headers=request.headers)

        token = _token_from_request(request)
        if not token:
            raise HTTPException(status_code=401, detail="Authentication required")

        principal = await _principal_from_token(token)
        metadata = with_principal({}, principal)
        return ensure_trace_metadata(metadata, headers=request.headers)

    async def _metadata_from_request(request: Request) -> dict[str, Any] | None:
        metadata: dict[str, Any] | None = None
        token = _token_from_request(request)
        if token and config.jwt is not None:
            principal = await _principal_from_token(token)
            metadata = with_principal({}, principal)
        return ensure_trace_metadata(metadata, headers=request.headers)

    def _runtime_access_from_authorization(authorization: str) -> AgentRuntimeAccessContext:
        try:
            token = parse_authorization_header(authorization)
        except JwtVerificationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        auth = build_agent_auth_context(token=token, jwt=config.jwt)
        return AgentRuntimeAccessContext(
            user_id=auth.user_id,
            profile_id=auth.profile_id,
            is_owner=auth.is_workspace_owner,
            permissions=auth.permissions,
        )

    async def _workspace_registry_from_authorization(authorization: str) -> dict[str, Any]:
        access = _runtime_access_from_authorization(authorization)
        if not access.can_interact():
            raise HTTPException(status_code=403, detail="Missing permission: interact_with_agent")
        return await run_in_threadpool(agent_control_plane.runtime_registry, access=access)

    async def _workspace_registry(request: Request) -> dict[str, Any] | None:
        authorization = _authorization_from_request(request)
        if not authorization:
            return None
        return await _workspace_registry_from_authorization(authorization)

    async def _workspace_agent_config_from_authorization(authorization: str, public_agent_name: str) -> dict[str, Any]:
        access = _runtime_access_from_authorization(authorization)
        if not access.can_interact():
            raise HTTPException(status_code=403, detail="Missing permission: interact_with_agent")
        try:
            return await run_in_threadpool(agent_control_plane.runtime_agent_config, access=access, slug=public_agent_name)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def _workspace_agent_config(request: Request, public_agent_name: str) -> dict[str, Any] | None:
        authorization = _authorization_from_request(request)
        if not authorization:
            return None
        return await _workspace_agent_config_from_authorization(authorization, public_agent_name)

    async def _workspace_agent_card_from_authorization(authorization: str, public_agent_name: str) -> dict[str, Any]:
        access = _runtime_access_from_authorization(authorization)
        if not access.can_interact():
            raise HTTPException(status_code=403, detail="Missing permission: interact_with_agent")
        try:
            return await run_in_threadpool(agent_control_plane.runtime_agent_card, access=access, slug=public_agent_name)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def _workspace_agent_card(request: Request, public_agent_name: str) -> dict[str, Any] | None:
        authorization = _authorization_from_request(request)
        if not authorization:
            return None
        return await _workspace_agent_card_from_authorization(authorization, public_agent_name)

    async def _resolve_runtime_agent_name(request: Request, public_agent_name: str | None) -> str:
        requested = (public_agent_name or config.default_agent).strip()
        agent_config = await _workspace_agent_config(request, requested)
        if agent_config is None:
            return requested
        runtime_name = str(agent_config.get("runtime_name") or "").strip()
        return runtime_name or requested

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    if metrics_enabled():

        @app.get("/metrics")
        async def metrics() -> Any:
            return JSONResponse(metrics_snapshot())

    @app.get("/agents")
    async def agents(request: Request, visible_only: bool = Query(False, alias="visibleOnly")) -> Any:
        registry_payload = await _workspace_registry(request)
        if registry_payload is not None:
            cards = []
            for item in registry_payload.get("agents") or []:
                if not isinstance(item, dict):
                    continue
                card_payload = item.get("card_payload")
                if isinstance(card_payload, dict):
                    cards.append(card_payload)
            cards.sort(key=lambda c: c.get("name") or "")
            return JSONResponse(cards)
        cards_now = directory.list()
        if visible_only:
            cards_now = filter_agent_cards(cards_now, include_names={config.default_agent})
        cards = [card.model_dump(mode="json", by_alias=True, exclude_none=True) for card in cards_now]
        cards.sort(key=lambda c: c.get("name") or "")
        return JSONResponse(cards)

    @app.get("/agent-card")
    async def agent_card(request: Request, agent_name: str | None = None) -> Any:
        card_payload = await _workspace_agent_card(request, agent_name or config.default_agent)
        if card_payload is not None:
            return JSONResponse(card_payload)
        try:
            result = await client.get_agent_card(
                agent_name=await _resolve_runtime_agent_name(request, agent_name or config.default_agent)
            )
            return JSONResponse(result)
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Agent did not respond in time") from exc

    @app.get("/tasks")
    async def list_tasks(
        request: Request,
        agent_name: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        status: str | None = None,
        context_id: str | None = Query(None, alias="contextId"),
    ) -> Any:
        metadata = await _require_request_metadata(request)
        params = TaskListParams(
            limit=limit,
            offset=offset,
            status=status,
            context_id=context_id,
            metadata=metadata,
        ).model_dump(
            by_alias=True, exclude_none=True
        )
        try:
            result = await client.call(
                agent_name=await _resolve_runtime_agent_name(request, agent_name or config.default_agent),
                method=METHOD_TASKS_LIST,
                params=params,
            )
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Agent did not respond in time") from exc
        payload = TaskListResult.model_validate(result).model_dump(mode="json", by_alias=True, exclude_none=True)
        return JSONResponse(payload)

    @app.get("/tasks/{task_id}")
    async def get_task(request: Request, task_id: str, agent_name: str | None = None) -> Any:
        metadata = await _require_request_metadata(request)
        try:
            task = await client.get_task(
                agent_name=await _resolve_runtime_agent_name(request, agent_name or config.default_agent),
                task_id=task_id,
                metadata=metadata,
            )
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Agent did not respond in time") from exc
        return JSONResponse(task.model_dump(mode="json", by_alias=True, exclude_none=True))

    @app.get("/tasks/{task_id}/events")
    async def task_events(
        request: Request,
        task_id: str,
        agent_name: str | None = None,
        replay_history: bool = True,
        ) -> Any:
        metadata = await _require_request_metadata(request)
        try:
            events = await client.subscribe_task(
                agent_name=await _resolve_runtime_agent_name(request, agent_name or config.default_agent),
                task_id=task_id,
                resubscribe=replay_history,
                metadata=metadata,
            )
        except Exception as exc:
            _raise_gateway_http_error(exc, action="subscribe to task events")

        async def _event_source():
            last_context_id: str | None = None
            try:
                async for ev in events:
                    payload = _stream_payload_from_event(ev)
                    last_context_id = str(payload.get("contextId") or "").strip() or last_context_id
                    yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
            except Exception as exc:
                _status_code, detail = _gateway_error_response(exc, action="stream task events")
                failure = _stream_failure_event(detail=detail, task_id=task_id, context_id=last_context_id)
                yield f"data: {json.dumps(failure, separators=(',', ':'))}\n\n"

        return StreamingResponse(_event_source(), media_type="text/event-stream")

    @app.post("/chat")
    async def chat(
        request: Request,
        body: ChatRequest,
    ) -> Any:
        metadata = await _require_request_metadata(request)
        msg = Message(role="user", parts=[TextPart(text=body.text)], context_id=body.context_id)
        configuration = (
            TaskConfiguration(history_length=body.history_length) if body.history_length is not None else None
        )
        try:
            task = await client.send_message(
                agent_name=await _resolve_runtime_agent_name(request, body.agent_name or config.default_agent),
                message=msg,
                configuration=configuration,
                metadata=metadata,
            )
        except Exception as exc:
            _raise_gateway_http_error(exc, action="send the chat request")
        return JSONResponse(task.model_dump(mode="json", by_alias=True, exclude_none=True))

    @app.post("/upload")
    async def upload(
        request: Request,
        file: UploadFile = File(...),
        agent_name: str | None = Form(None),
        context_id: str | None = Form(None, alias="contextId"),
        history_length: int | None = Form(None, alias="historyLength"),
    ) -> Any:
        metadata = await _require_request_metadata(request)
        raw = await file.read()
        mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
        b64 = base64.b64encode(raw).decode("utf-8")
        part = FilePart(file=FileWithBytes(bytes=b64, mime_type=mime))
        msg = Message(role="user", parts=[part], context_id=context_id)
        configuration = TaskConfiguration(history_length=history_length) if history_length is not None else None
        try:
            task = await client.send_message(
                agent_name=await _resolve_runtime_agent_name(request, agent_name or config.default_agent),
                message=msg,
                configuration=configuration,
                metadata=metadata,
            )
        except Exception as exc:
            _raise_gateway_http_error(exc, action="upload the message")
        return JSONResponse(task.model_dump(mode="json", by_alias=True, exclude_none=True))

    @app.post("/stream")
    async def stream(
        request: Request,
        body: ChatRequest,
    ):
        try:
            metadata = await _require_request_metadata(request)
            metadata = _attach_direct_stream_history(metadata, body)
            auth_context = (
                build_agent_auth_context(token=_token_from_request(request) or "", jwt=config.jwt)
                if config.jwt is not None
                else None
            )
            msg = Message(role="user", parts=[TextPart(text=body.text)], context_id=body.context_id)
            configuration = (
                TaskConfiguration(history_length=body.history_length) if body.history_length is not None else None
            )
            events = await client.stream_message(
                agent_name=await _resolve_runtime_agent_name(request, body.agent_name or config.default_agent),
                message=msg,
                configuration=configuration,
                metadata=metadata,
                timeout_s=config.stream_request_timeout_s,
            )
        except Exception as exc:
            _raise_gateway_http_error(exc, action="start the agent stream")

        async def _event_source():
            last_task_id: str | None = None
            last_context_id: str | None = body.context_id
            charge_tracker = A2ACompletionChargeTracker(
                profile_id=auth_context.profile_id if auth_context is not None else None,
                conversation_id=None,
                prompt_text=body.text,
            )
            try:
                async for ev in events:
                    payload = _stream_payload_from_event(ev)
                    last_task_id = str(payload.get("taskId") or payload.get("id") or "").strip() or last_task_id
                    last_context_id = str(payload.get("contextId") or "").strip() or last_context_id
                    pending_charge = charge_tracker.evaluate(payload)
                    if pending_charge is not None:
                        await run_in_threadpool(
                            spend_intera_coins_for_a2a_completion,
                            profile_id=pending_charge.profile_id,
                            task_id=pending_charge.task_id,
                            conversation_id=pending_charge.conversation_id,
                            prompt_text=pending_charge.prompt_text,
                        )
                    yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
            except Exception as exc:
                _status_code, detail = _gateway_error_response(exc, action="stream the agent response")
                failure = _stream_failure_event(detail=detail, task_id=last_task_id, context_id=last_context_id)
                yield f"data: {json.dumps(failure, separators=(',', ':'))}\n\n"

        return StreamingResponse(_event_source(), media_type="text/event-stream")

    @app.post("/tasks/{task_id}/continue")
    async def continue_task(
        request: Request,
        task_id: str,
        body: TaskContinueRequest,
        agent_name: str | None = None,
    ) -> Any:
        metadata = await _require_request_metadata(request)
        msg = Message(role="user", parts=[TextPart(text=body.text)])
        configuration = (
            TaskConfiguration(history_length=body.history_length) if body.history_length is not None else None
        )
        try:
            task = await client.continue_task(
                agent_name=await _resolve_runtime_agent_name(request, agent_name or config.default_agent),
                task_id=task_id,
                message=msg,
                configuration=configuration,
                metadata=metadata,
            )
        except Exception as exc:
            _raise_gateway_http_error(exc, action="continue the task")
        return JSONResponse(task.model_dump(mode="json", by_alias=True, exclude_none=True))

    @app.post("/tasks/{task_id}/continue/stream")
    async def continue_task_stream(
        request: Request,
        task_id: str,
        body: TaskContinueRequest,
        agent_name: str | None = None,
    ):
        try:
            metadata = await _require_request_metadata(request)
            metadata = _attach_direct_stream_history(metadata, body)
            auth_context = (
                build_agent_auth_context(token=_token_from_request(request) or "", jwt=config.jwt)
                if config.jwt is not None
                else None
            )
            msg = Message(role="user", parts=[TextPart(text=body.text)])
            configuration = (
                TaskConfiguration(history_length=body.history_length) if body.history_length is not None else None
            )
            events = await client.continue_task_stream(
                agent_name=await _resolve_runtime_agent_name(request, agent_name or config.default_agent),
                task_id=task_id,
                message=msg,
                configuration=configuration,
                metadata=metadata,
                timeout_s=config.stream_request_timeout_s,
            )
        except Exception as exc:
            _raise_gateway_http_error(exc, action="resume the agent stream")

        async def _event_source():
            last_context_id: str | None = None
            charge_tracker = A2ACompletionChargeTracker(
                profile_id=auth_context.profile_id if auth_context is not None else None,
                conversation_id=None,
                prompt_text=body.text,
                current_task_id=task_id,
            )
            try:
                async for ev in events:
                    payload = _stream_payload_from_event(ev)
                    last_context_id = str(payload.get("contextId") or "").strip() or last_context_id
                    pending_charge = charge_tracker.evaluate(payload)
                    if pending_charge is not None:
                        await run_in_threadpool(
                            spend_intera_coins_for_a2a_completion,
                            profile_id=pending_charge.profile_id,
                            task_id=pending_charge.task_id,
                            conversation_id=pending_charge.conversation_id,
                            prompt_text=pending_charge.prompt_text,
                        )
                    yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
            except Exception as exc:
                _status_code, detail = _gateway_error_response(exc, action="resume the agent response stream")
                failure = _stream_failure_event(detail=detail, task_id=task_id, context_id=last_context_id)
                yield f"data: {json.dumps(failure, separators=(',', ':'))}\n\n"

        return StreamingResponse(_event_source(), media_type="text/event-stream")

    return app
