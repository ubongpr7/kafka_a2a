import asyncio

import httpx
import pytest


fastapi = pytest.importorskip("fastapi")
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from kafka_a2a.server.a2a_http import A2AHttpProxyConfig, create_a2a_http_proxy_app  # noqa: E402
from kafka_a2a.server.gateway import GatewayConfig, create_gateway_app  # noqa: E402


def test_gateway_openapi_builds() -> None:
    app = create_gateway_app(GatewayConfig(bootstrap_servers="localhost:9092", default_agent="host"))
    schema = app.openapi()
    assert isinstance(schema, dict)
    assert "/health" in schema.get("paths", {})
    assert "/agents" in schema.get("paths", {})
    assert "/chat" in schema.get("paths", {})
    assert "/upload" in schema.get("paths", {})
    assert "/stream" in schema.get("paths", {})
    assert "/tasks" in schema.get("paths", {})
    assert "/tasks/{task_id}" in schema.get("paths", {})
    assert "/tasks/{task_id}/events" in schema.get("paths", {})


def test_proxy_openapi_builds() -> None:
    app = create_a2a_http_proxy_app(A2AHttpProxyConfig(bootstrap_servers="localhost:9092", agent_name="host"))
    schema = app.openapi()
    assert isinstance(schema, dict)
    assert "/health" in schema.get("paths", {})
    assert "/" in schema.get("paths", {})
    assert "/.well-known/agent-card.json" in schema.get("paths", {})


def test_proxy_enables_cors_middleware() -> None:
    app = create_a2a_http_proxy_app(A2AHttpProxyConfig(bootstrap_servers="localhost:9092", agent_name="host"))
    middleware_classes = [middleware.cls for middleware in app.user_middleware]
    assert CORSMiddleware in middleware_classes


def test_proxy_cors_preflight_allows_configured_local_frontend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KA2A_CORS_ALLOW_ORIGINS", "https://dev.interaims.com,http://localhost:3000")
    monkeypatch.setenv("KA2A_CORS_ALLOW_CREDENTIALS", "true")
    app = create_a2a_http_proxy_app(A2AHttpProxyConfig(bootstrap_servers="localhost:9092", agent_name="host"))

    async def request_preflight() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.options(
                "/",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )

    response = asyncio.run(request_preflight())

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "POST" in response.headers["access-control-allow-methods"]


def test_gateway_enables_cors_middleware() -> None:
    app = create_gateway_app(GatewayConfig(bootstrap_servers="localhost:9092", default_agent="host"))
    middleware_classes = [middleware.cls for middleware in app.user_middleware]
    assert CORSMiddleware in middleware_classes


def test_gateway_cors_credentials_can_be_enabled_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KA2A_CORS_ALLOW_CREDENTIALS", "true")
    app = create_gateway_app(GatewayConfig(bootstrap_servers="localhost:9092", default_agent="host"))
    cors_middleware = next(middleware for middleware in app.user_middleware if middleware.cls is CORSMiddleware)
    assert cors_middleware.kwargs["allow_credentials"] is True


def test_gateway_cors_credentials_can_be_disabled_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KA2A_CORS_ALLOW_CREDENTIALS", "false")
    app = create_gateway_app(GatewayConfig(bootstrap_servers="localhost:9092", default_agent="host"))
    cors_middleware = next(middleware for middleware in app.user_middleware if middleware.cls is CORSMiddleware)
    assert cors_middleware.kwargs["allow_credentials"] is False
