from __future__ import annotations

from dataclasses import dataclass, replace
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
        owner_claim = self.claims.get("is_owner")
        claim_marks_owner = owner_claim is True or (
            isinstance(owner_claim, str) and owner_claim.strip().lower() in {"1", "true", "yes"}
        )
        return claim_marks_owner or bool(self.owner_id and self.owner_id == self.user_id)

    def has_permission(self, permission: str) -> bool:
        return self.is_workspace_owner or permission in self.permissions


def _normalize_id(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def get_bearer_token_from_request(request: Request) -> str:
    authorization = request.headers.get("authorization")
    if authorization:
        try:
            return parse_authorization_header(authorization)
        except JwtVerificationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    raise HTTPException(status_code=401, detail="Missing Authorization header.")


def build_agent_auth_context(*, token: str, jwt: JwtBearerConfig | None, context_token: str | None = None) -> AgentAuthContext:
    if jwt is None:
        raise HTTPException(status_code=501, detail="JWT auth must be enabled.")
    try:
        principal = verify_bearer_jwt(token=token, config=jwt)
    except JwtVerificationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RuntimeError as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    claims = dict(principal.claims or {})
    if context_token:
        try:
            context_principal = verify_bearer_jwt(
                token=context_token,
                config=replace(jwt, include_claims=True),
            )
        except (JwtVerificationError, RuntimeError) as exc:
            raise HTTPException(status_code=401, detail="Invalid authorization context.") from exc
        context_claims = dict(context_principal.claims or {})
        if (
            context_claims.get("token_type") not in {"intera_authorization_context", "intera_websocket_ticket"}
            or str(context_claims.get("user_id") or "") != str(principal.user_id)
            or str(context_claims.get("profile_id") or "") != str(claims.get("profile_id") or "")
        ):
            raise HTTPException(status_code=401, detail="Authorization context does not match the access token.")
        permissions = set(claims.get("permissions") or []) | set(context_claims.get("permissions") or [])
        for wildcard in context_claims.get("wildcards") or []:
            permissions.update((context_claims.get("wildcard_permissions") or {}).get(wildcard) or [])
        claims = {**claims, "permissions": sorted(str(item) for item in permissions if str(item).strip())}
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
