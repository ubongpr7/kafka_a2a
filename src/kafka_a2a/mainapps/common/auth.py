from __future__ import annotations

from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any

from fastapi import HTTPException, Request, WebSocket

from kafka_a2a.server.auth import (
    JwtBearerConfig,
    JwtVerificationError,
    parse_authorization_header,
    verify_bearer_jwt,
)


@dataclass(slots=True)
class AgentAuthContext:
    user_id: str
    profile_id: str
    owner_id: str | None
    permissions: set[str]
    claims: dict[str, Any]
    bearer_token: str

    @property
    def is_workspace_owner(self) -> bool:
        return bool(self.owner_id and self.owner_id == self.user_id)

    def has_permission(self, permission: str) -> bool:
        return self.is_workspace_owner or permission in self.permissions


def _normalize_id(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _parse_cookie_token(cookie_header: str | None) -> str | None:
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


def get_bearer_token_from_request(request: Request) -> str:
    authorization = request.headers.get("authorization")
    if authorization:
        try:
            return parse_authorization_header(authorization)
        except JwtVerificationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    raise HTTPException(status_code=401, detail="Missing Authorization header.")


def get_bearer_token_from_websocket(websocket: WebSocket) -> str | None:
    authorization = websocket.headers.get("authorization")
    if authorization:
        try:
            return parse_authorization_header(authorization)
        except JwtVerificationError:
            return None
    query_token = (websocket.query_params.get("token") or "").strip()
    if query_token:
        return query_token
    return _parse_cookie_token(websocket.headers.get("cookie"))


def build_agent_auth_context(*, token: str, jwt: JwtBearerConfig | None) -> AgentAuthContext:
    if jwt is None:
        raise HTTPException(status_code=501, detail="JWT auth must be enabled.")
    try:
        principal = verify_bearer_jwt(token=token, config=jwt)
    except JwtVerificationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RuntimeError as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    claims = principal.claims or {}
    profile_id = _normalize_id(
        claims.get("profile_id")
        or ((claims.get("profile_context") or {}).get("id") if isinstance(claims.get("profile_context"), dict) else None)
        or claims.get("profile")
    )
    if not profile_id:
        raise HTTPException(status_code=403, detail="No active workspace context in token.")

    permissions_raw = claims.get("permissions") or []
    if isinstance(permissions_raw, (set, tuple)):
        permissions_raw = list(permissions_raw)
    permissions = {str(item).strip() for item in permissions_raw if str(item).strip()} if isinstance(permissions_raw, list) else set()

    user_id = _normalize_id(principal.user_id)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authenticated user id is missing.")

    return AgentAuthContext(
        user_id=user_id,
        profile_id=profile_id,
        owner_id=_normalize_id(claims.get("owner_id")),
        permissions=permissions,
        claims=claims,
        bearer_token=token,
    )


def require_permission(context: AgentAuthContext, permission: str) -> None:
    if not context.has_permission(permission):
        raise HTTPException(status_code=403, detail=f"Missing permission: {permission}")
