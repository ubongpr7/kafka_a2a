from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

from pydantic import ValidationError

from kafka_a2a.client import Ka2aClient, Ka2aClientConfig
from kafka_a2a.errors import A2AError
from kafka_a2a.protocol import (
    METHOD_AGENT_GET_AUTHENTICATED_EXTENDED_CARD,
    METHOD_MESSAGE_SEND,
    METHOD_MESSAGE_STREAM,
    METHOD_TASKS_CANCEL,
    METHOD_TASKS_GET,
    METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_DELETE,
    METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_GET,
    METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_LIST,
    METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_SET,
    METHOD_TASKS_RESUBSCRIBE,
    MessageSendParams,
    TaskIdParams,
    TaskQueryParams,
)
from kafka_a2a.transport.kafka import KafkaConfig, KafkaTransport
from kafka_a2a.server.auth import JwtBearerConfig, JwtVerificationError, parse_authorization_header, verify_bearer_jwt
from kafka_a2a.tenancy import with_principal


def _require_fastapi() -> Any:
    try:
        from fastapi import FastAPI  # noqa: F401
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "FastAPI server extras not installed. Install with: pip install 'kafka-a2a[server]'"
        ) from exc
    from fastapi import FastAPI

    return FastAPI


def _jsonrpc_success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


@dataclass(slots=True)
class A2AHttpProxyConfig:
    bootstrap_servers: str
    agent_name: str
    client_id: str | None = None
    title: str = "K-A2A (A2A JSON-RPC over Kafka)"
    version: str = "0.1.0"
    jwt: JwtBearerConfig | None = None


def create_a2a_http_proxy_app(config: A2AHttpProxyConfig):
    FastAPI = _require_fastapi()
    from fastapi import Request
    from fastapi.responses import JSONResponse, StreamingResponse

    transport = KafkaTransport(KafkaConfig(bootstrap_servers=config.bootstrap_servers))
    client = Ka2aClient(transport=transport, config=Ka2aClientConfig(client_id=config.client_id))

    app = FastAPI(title=config.title, version=config.version)

    @app.on_event("startup")
    async def _startup() -> None:
        await client.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await client.stop()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/.well-known/agent-card.json")
    async def agent_card(request: Request) -> Any:
        if config.jwt is not None:
            try:
                token = parse_authorization_header(request.headers.get("authorization"))
                verify_bearer_jwt(token=token, config=config.jwt)
            except JwtVerificationError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=401)
            except RuntimeError as exc:  # pragma: no cover
                return JSONResponse({"detail": str(exc)}, status_code=500)
        card = await client.get_agent_card(agent_name=config.agent_name)
        return JSONResponse(card)

    # Backward compatibility with older A2A implementations
    @app.get("/.well-known/agent.json")
    async def deprecated_agent_card(request: Request) -> Any:
        if config.jwt is not None:
            try:
                token = parse_authorization_header(request.headers.get("authorization"))
                verify_bearer_jwt(token=token, config=config.jwt)
            except JwtVerificationError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=401)
            except RuntimeError as exc:  # pragma: no cover
                return JSONResponse({"detail": str(exc)}, status_code=500)
        card = await client.get_agent_card(agent_name=config.agent_name)
        return JSONResponse(card)

    @app.post("/")
    async def a2a_jsonrpc(request: Request) -> Any:  # noqa: C901
        request_id: Any = None
        principal = None
        if config.jwt is not None:
            try:
                token = parse_authorization_header(request.headers.get("authorization"))
                principal = verify_bearer_jwt(token=token, config=config.jwt)
            except JwtVerificationError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=401)
            except RuntimeError as exc:  # pragma: no cover
                return JSONResponse({"detail": str(exc)}, status_code=500)
        try:
            body = await request.json()
        except Exception as exc:
            return JSONResponse(_jsonrpc_error(None, -32700, "Parse error", {"detail": str(exc)}))

        if not isinstance(body, dict):
            return JSONResponse(_jsonrpc_error(None, -32600, "Invalid Request"))

        request_id = body.get("id")
        if body.get("jsonrpc") != "2.0":
            return JSONResponse(_jsonrpc_error(request_id, -32600, "Invalid Request"))

        method = body.get("method")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}

        if not isinstance(method, str):
            return JSONResponse(_jsonrpc_error(request_id, -32600, "Invalid Request"))

        if method in (METHOD_MESSAGE_STREAM, METHOD_TASKS_RESUBSCRIBE):

            async def _event_source() -> AsyncIterator[str]:
                try:
                    if method == METHOD_MESSAGE_STREAM:
                        p = MessageSendParams.model_validate(params)
                        events = await client.stream_message(
                            agent_name=config.agent_name,
                            message=p.message,
                            configuration=p.configuration,
                            metadata=with_principal(p.metadata or {}, principal) if principal else p.metadata,
                        )
                    else:
                        p = TaskIdParams.model_validate(params)
                        events = await client.subscribe_task(
                            agent_name=config.agent_name,
                            task_id=p.id,
                            resubscribe=True,
                            metadata=with_principal(p.metadata or {}, principal) if principal else p.metadata,
                        )

                    async for ev in events:
                        result_obj = ev
                        if hasattr(result_obj, "model_dump"):
                            result_obj = result_obj.model_dump(by_alias=True, exclude_none=True)
                        payload = _jsonrpc_success(
                            request_id,
                            result_obj,
                        )
                        yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                except ValidationError as exc:
                    err = _jsonrpc_error(
                        request_id, -32602, "Invalid params", {"detail": json.loads(exc.json())}
                    )
                    yield f"data: {json.dumps(err, separators=(',', ':'))}\n\n"
                except A2AError as exc:
                    err = _jsonrpc_error(request_id, exc.code, exc.message, exc.data)
                    yield f"data: {json.dumps(err, separators=(',', ':'))}\n\n"
                except Exception as exc:  # pragma: no cover
                    err = _jsonrpc_error(request_id, -32603, "Internal error", {"detail": str(exc)})
                    yield f"data: {json.dumps(err, separators=(',', ':'))}\n\n"

            return StreamingResponse(_event_source(), media_type="text/event-stream")

        try:
            if method == METHOD_MESSAGE_SEND:
                p = MessageSendParams.model_validate(params)
                task = await client.send_message(
                    agent_name=config.agent_name,
                    message=p.message,
                    configuration=p.configuration,
                    metadata=with_principal(p.metadata or {}, principal) if principal else p.metadata,
                )
                return JSONResponse(
                    _jsonrpc_success(request_id, task.model_dump(by_alias=True, exclude_none=True))
                )

            if method == METHOD_TASKS_GET:
                p = TaskQueryParams.model_validate(params)
                task = await client.get_task(
                    agent_name=config.agent_name,
                    task_id=p.id,
                    metadata=with_principal(p.metadata or {}, principal) if principal else p.metadata,
                )
                return JSONResponse(
                    _jsonrpc_success(request_id, task.model_dump(by_alias=True, exclude_none=True))
                )

            if method == METHOD_TASKS_CANCEL:
                p = TaskIdParams.model_validate(params)
                task = await client.cancel_task(
                    agent_name=config.agent_name,
                    task_id=p.id,
                    metadata=with_principal(p.metadata or {}, principal) if principal else p.metadata,
                )
                return JSONResponse(
                    _jsonrpc_success(request_id, task.model_dump(by_alias=True, exclude_none=True))
                )

            if method in (
                METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_SET,
                METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_GET,
                METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_LIST,
                METHOD_TASKS_PUSH_NOTIFICATION_CONFIG_DELETE,
            ):
                if principal:
                    params = dict(params)
                    params["metadata"] = with_principal(params.get("metadata") or {}, principal)
                result = await client.call(agent_name=config.agent_name, method=method, params=params)
                return JSONResponse(_jsonrpc_success(request_id, result))

            if method == METHOD_AGENT_GET_AUTHENTICATED_EXTENDED_CARD:
                card = await client.get_agent_card(agent_name=config.agent_name)
                return JSONResponse(_jsonrpc_success(request_id, card))

            return JSONResponse(_jsonrpc_error(request_id, -32601, "Method not found"))
        except ValidationError as exc:
            return JSONResponse(_jsonrpc_error(request_id, -32602, "Invalid params", json.loads(exc.json())))
        except A2AError as exc:
            return JSONResponse(_jsonrpc_error(request_id, exc.code, exc.message, exc.data))
        except Exception as exc:  # pragma: no cover
            return JSONResponse(_jsonrpc_error(request_id, -32603, "Internal error", {"detail": str(exc)}))

    return app
