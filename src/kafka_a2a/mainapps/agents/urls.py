from __future__ import annotations

from fastapi import APIRouter

from kafka_a2a.server.auth import JwtBearerConfig

from .services import AgentControlPlaneService
from .views import build_agents_router


def build_urlpatterns(
    *,
    service: AgentControlPlaneService,
    jwt: JwtBearerConfig | None,
    runtime_shared_token: str | None,
) -> APIRouter:
    return build_agents_router(
        service=service,
        jwt=jwt,
        runtime_shared_token=runtime_shared_token,
    )
