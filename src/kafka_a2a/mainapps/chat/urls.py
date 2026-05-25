from __future__ import annotations

from fastapi import APIRouter

from .views import ChatRouterDependencies, build_chat_router


def build_urlpatterns(*, deps: ChatRouterDependencies) -> APIRouter:
    return build_chat_router(deps=deps)
