import pytest


fastapi = pytest.importorskip("fastapi")
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from kafka_a2a.server.a2a_http import A2AHttpProxyConfig, create_a2a_http_proxy_app  # noqa: E402
from kafka_a2a.server.gateway import GatewayConfig, create_gateway_app  # noqa: E402


def test_gateway_openapi_builds() -> None:
    app = create_gateway_app(GatewayConfig(bootstrap_servers="localhost:9092", default_agent="echo"))
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
    app = create_a2a_http_proxy_app(A2AHttpProxyConfig(bootstrap_servers="localhost:9092", agent_name="echo"))
    schema = app.openapi()
    assert isinstance(schema, dict)
    assert "/health" in schema.get("paths", {})
    assert "/" in schema.get("paths", {})
    assert "/.well-known/agent-card.json" in schema.get("paths", {})


def test_gateway_enables_cors_middleware() -> None:
    app = create_gateway_app(GatewayConfig(bootstrap_servers="localhost:9092", default_agent="echo"))
    middleware_classes = [middleware.cls for middleware in app.user_middleware]
    assert CORSMiddleware in middleware_classes
