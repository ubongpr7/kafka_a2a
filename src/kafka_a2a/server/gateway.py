import asyncio
import json
import mimetypes
import base64
import os
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from kafka_a2a.agent_filter import filter_agent_cards
from kafka_a2a.client import Ka2aClient, Ka2aClientConfig
from kafka_a2a.models import AgentCard, FilePart, FileWithBytes, Ka2aModel, Message, TaskConfiguration, TextPart
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
    jwt: JwtBearerConfig | None = None


_DEFAULT_CORS_ALLOW_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"


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

    app = FastAPI(title="K-A2A Gateway", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_parse_csv_env("KA2A_CORS_ALLOW_ORIGINS"),
        allow_origin_regex=os.environ.get("KA2A_CORS_ALLOW_ORIGIN_REGEX", _DEFAULT_CORS_ALLOW_ORIGIN_REGEX),
        allow_credentials=_parse_bool_env("KA2A_CORS_ALLOW_CREDENTIALS", default=False),
        allow_methods=_parse_csv_env("KA2A_CORS_ALLOW_METHODS") or ["GET", "POST", "OPTIONS"],
        allow_headers=_parse_csv_env("KA2A_CORS_ALLOW_HEADERS")
        or ["Authorization", "Content-Type", "X-Requested-With"],
    )
    class ChatRequest(Ka2aModel):
        text: str
        agent_name: str | None = None
        context_id: str | None = None
        history_length: int | None = None

    def _metadata_from_request(request: Request) -> dict[str, Any] | None:
        metadata: dict[str, Any] | None = None
        if config.jwt is not None:
            try:
                token = parse_authorization_header(request.headers.get("authorization"))
                principal = verify_bearer_jwt(token=token, config=config.jwt)
            except JwtVerificationError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            except RuntimeError as exc:  # pragma: no cover
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            metadata = with_principal({}, principal)
        return ensure_trace_metadata(metadata, headers=request.headers)

    @app.on_event("startup")
    async def _startup() -> None:
        await client.start()
        await directory.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await directory.stop()
        await client.stop()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    if metrics_enabled():

        @app.get("/metrics")
        async def metrics() -> Any:
            return JSONResponse(metrics_snapshot())

    @app.get("/agents")
    async def agents(request: Request) -> Any:
        _metadata_from_request(request)
        visible_cards = filter_agent_cards(directory.list(), include_names={config.default_agent})
        cards = [card.model_dump(mode="json", by_alias=True, exclude_none=True) for card in visible_cards]
        cards.sort(key=lambda c: c.get("name") or "")
        return JSONResponse(cards)

    @app.get("/agent-card")
    async def agent_card(agent_name: str | None = None) -> Any:
        try:
            result = await client.get_agent_card(agent_name=agent_name or config.default_agent)
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
        metadata = _metadata_from_request(request)
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
                agent_name=agent_name or config.default_agent,
                method=METHOD_TASKS_LIST,
                params=params,
            )
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Agent did not respond in time") from exc
        payload = TaskListResult.model_validate(result).model_dump(mode="json", by_alias=True, exclude_none=True)
        return JSONResponse(payload)

    @app.get("/tasks/{task_id}")
    async def get_task(request: Request, task_id: str, agent_name: str | None = None) -> Any:
        metadata = _metadata_from_request(request)
        try:
            task = await client.get_task(
                agent_name=agent_name or config.default_agent, task_id=task_id, metadata=metadata
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
        metadata = _metadata_from_request(request)
        try:
            events = await client.subscribe_task(
                agent_name=agent_name or config.default_agent,
                task_id=task_id,
                resubscribe=replay_history,
                metadata=metadata,
            )
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Agent did not respond in time") from exc

        async def _event_source():
            async for ev in events:
                payload: Any = ev
                if hasattr(ev, "model_dump"):
                    payload = ev.model_dump(mode="json", by_alias=True, exclude_none=True)
                yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"

        return StreamingResponse(_event_source(), media_type="text/event-stream")

    @app.post("/chat")
    async def chat(
        request: Request,
        body: ChatRequest,
    ) -> Any:
        metadata = _metadata_from_request(request)
        msg = Message(role="user", parts=[TextPart(text=body.text)], context_id=body.context_id)
        configuration = (
            TaskConfiguration(history_length=body.history_length) if body.history_length is not None else None
        )
        try:
            task = await client.send_message(
                agent_name=body.agent_name or config.default_agent,
                message=msg,
                configuration=configuration,
                metadata=metadata,
            )
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Agent did not respond in time") from exc
        return JSONResponse(task.model_dump(mode="json", by_alias=True, exclude_none=True))

    @app.post("/upload")
    async def upload(
        request: Request,
        file: UploadFile = File(...),
        agent_name: str | None = Form(None),
        context_id: str | None = Form(None, alias="contextId"),
        history_length: int | None = Form(None, alias="historyLength"),
    ) -> Any:
        metadata = _metadata_from_request(request)
        raw = await file.read()
        mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
        b64 = base64.b64encode(raw).decode("utf-8")
        part = FilePart(file=FileWithBytes(bytes=b64, mime_type=mime))
        msg = Message(role="user", parts=[part], context_id=context_id)
        configuration = TaskConfiguration(history_length=history_length) if history_length is not None else None
        try:
            task = await client.send_message(
                agent_name=agent_name or config.default_agent,
                message=msg,
                configuration=configuration,
                metadata=metadata,
            )
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Agent did not respond in time") from exc
        return JSONResponse(task.model_dump(mode="json", by_alias=True, exclude_none=True))

    @app.post("/stream")
    async def stream(
        request: Request,
        body: ChatRequest,
    ):
        metadata = _metadata_from_request(request)
        msg = Message(role="user", parts=[TextPart(text=body.text)], context_id=body.context_id)
        configuration = (
            TaskConfiguration(history_length=body.history_length) if body.history_length is not None else None
        )
        try:
            events = await client.stream_message(
                agent_name=body.agent_name or config.default_agent,
                message=msg,
                configuration=configuration,
                metadata=metadata,
            )
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Agent did not respond in time") from exc

        async def _event_source():
            async for ev in events:
                payload: Any = ev
                if hasattr(ev, "model_dump"):
                    payload = ev.model_dump(mode="json", by_alias=True, exclude_none=True)
                yield f"data: {json.dumps(payload)}\n\n"

        return StreamingResponse(_event_source(), media_type="text/event-stream")

    return app
