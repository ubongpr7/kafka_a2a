import asyncio
import json
import mimetypes
import base64
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from kafka_a2a.client import Ka2aClient, Ka2aClientConfig
from kafka_a2a.models import AgentCard, FilePart, FileWithBytes, Ka2aModel, Message, TaskConfiguration, TextPart
from kafka_a2a.protocol import METHOD_TASKS_LIST, TaskListParams, TaskListResult
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
    jwt: JwtBearerConfig | None = None


def create_gateway_app(config: GatewayConfig):
    FastAPI = _require_fastapi()
    from fastapi import File, Form, HTTPException, Request, UploadFile
    from fastapi.responses import JSONResponse, StreamingResponse

    transport = KafkaTransport(KafkaConfig(bootstrap_servers=config.bootstrap_servers))
    client = Ka2aClient(transport=transport, config=Ka2aClientConfig(client_id=config.client_id))
    registry = KafkaAgentRegistry(transport=transport, sender=config.client_id or "gateway")

    agent_cards: dict[str, AgentCard] = {}
    registry_task: Any | None = None

    app = FastAPI(title="K-A2A Gateway", version="0.1.0")

    class ChatRequest(Ka2aModel):
        text: str
        agent_name: str | None = None
        context_id: str | None = None
        history_length: int | None = None

    def _metadata_from_request(request: Request) -> dict[str, Any] | None:
        if config.jwt is None:
            return None
        try:
            token = parse_authorization_header(request.headers.get("authorization"))
            principal = verify_bearer_jwt(token=token, config=config.jwt)
        except JwtVerificationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RuntimeError as exc:  # pragma: no cover
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return with_principal({}, principal)

    @app.on_event("startup")
    async def _startup() -> None:
        await client.start()
        nonlocal registry_task

        async def _watch_registry() -> None:
            # Use a per-process group id so each gateway instance sees all cards.
            group_id = f"ka2a.gateway.registry.{uuid4()}"
            while True:
                try:
                    async for entry in registry.watch(
                        group_id=group_id,
                        auto_offset_reset="earliest",
                    ):
                        agent_cards[entry.agent_name] = entry.card
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # If Kafka is temporarily unavailable or the topic doesn't exist yet,
                    # keep retrying in the background.
                    await asyncio.sleep(1.0)

        registry_task = asyncio.create_task(_watch_registry())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        nonlocal registry_task
        if registry_task is not None:
            registry_task.cancel()
            try:
                await registry_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            registry_task = None
        await client.stop()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/agents")
    async def agents(request: Request) -> Any:
        _metadata_from_request(request)
        cards = [card.model_dump(mode="json", by_alias=True, exclude_none=True) for card in agent_cards.values()]
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
    ) -> Any:
        metadata = _metadata_from_request(request)
        params = TaskListParams(limit=limit, offset=offset, status=status, metadata=metadata).model_dump(
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
