from __future__ import annotations

import json
import mimetypes
import base64
from dataclasses import dataclass
from typing import Any

from kafka_a2a.client import Ka2aClient, Ka2aClientConfig
from kafka_a2a.models import FilePart, FileWithBytes, Message, TextPart
from kafka_a2a.server.auth import JwtBearerConfig, JwtVerificationError, parse_authorization_header, verify_bearer_jwt
from kafka_a2a.tenancy import with_principal
from kafka_a2a.transport.kafka import KafkaConfig, KafkaTransport


def _require_fastapi() -> Any:
    try:
        from fastapi import FastAPI  # noqa: F401
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "FastAPI server extras not installed. Install with: pip install 'kafka-a2a[server]'"
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
    from fastapi import Body, File, Form, HTTPException, Request, UploadFile
    from fastapi.responses import JSONResponse, StreamingResponse

    transport = KafkaTransport(KafkaConfig(bootstrap_servers=config.bootstrap_servers))
    client = Ka2aClient(transport=transport, config=Ka2aClientConfig(client_id=config.client_id))

    app = FastAPI(title="K-A2A Gateway", version="0.1.0")

    @app.on_event("startup")
    async def _startup() -> None:
        await client.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await client.stop()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/agent-card")
    async def agent_card(agent_name: str | None = None) -> Any:
        result = await client.get_agent_card(agent_name=agent_name or config.default_agent)
        return JSONResponse(result)

    @app.post("/chat")
    async def chat(
        request: Request,
        text: str = Body(..., embed=True),
        agent_name: str | None = Body(None, embed=True),
    ) -> Any:
        metadata = None
        if config.jwt is not None:
            try:
                token = parse_authorization_header(request.headers.get("authorization"))
                principal = verify_bearer_jwt(token=token, config=config.jwt)
            except JwtVerificationError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            except RuntimeError as exc:  # pragma: no cover
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            metadata = with_principal({}, principal)
        msg = Message(role="user", parts=[TextPart(text=text)])
        task = await client.send_message(
            agent_name=agent_name or config.default_agent, message=msg, metadata=metadata
        )
        return JSONResponse(task.model_dump(by_alias=True, exclude_none=True))

    @app.post("/upload")
    async def upload(
        request: Request,
        file: UploadFile = File(...),
        agent_name: str | None = Form(None),
    ) -> Any:
        metadata = None
        if config.jwt is not None:
            try:
                token = parse_authorization_header(request.headers.get("authorization"))
                principal = verify_bearer_jwt(token=token, config=config.jwt)
            except JwtVerificationError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            except RuntimeError as exc:  # pragma: no cover
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            metadata = with_principal({}, principal)
        raw = await file.read()
        mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
        b64 = base64.b64encode(raw).decode("utf-8")
        part = FilePart(file=FileWithBytes(bytes=b64, mime_type=mime))
        msg = Message(role="user", parts=[part])
        task = await client.send_message(
            agent_name=agent_name or config.default_agent, message=msg, metadata=metadata
        )
        return JSONResponse(task.model_dump(by_alias=True, exclude_none=True))

    @app.post("/stream")
    async def stream(
        request: Request,
        text: str = Body(..., embed=True),
        agent_name: str | None = Body(None, embed=True),
    ):
        metadata = None
        if config.jwt is not None:
            try:
                token = parse_authorization_header(request.headers.get("authorization"))
                principal = verify_bearer_jwt(token=token, config=config.jwt)
            except JwtVerificationError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            except RuntimeError as exc:  # pragma: no cover
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            metadata = with_principal({}, principal)
        msg = Message(role="user", parts=[TextPart(text=text)])
        events = await client.stream_message(
            agent_name=agent_name or config.default_agent, message=msg, metadata=metadata
        )

        async def _event_source():
            async for ev in events:
                payload: Any = ev
                if hasattr(ev, "model_dump"):
                    payload = ev.model_dump(by_alias=True, exclude_none=True)
                yield f"data: {json.dumps(payload)}\n\n"

        return StreamingResponse(_event_source(), media_type="text/event-stream")

    return app
