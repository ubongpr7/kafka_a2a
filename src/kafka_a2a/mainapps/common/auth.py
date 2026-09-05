from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
import time
from typing import Any
from urllib import error, request as urlrequest

from fastapi import HTTPException, Request, WebSocket

from kafka_a2a.server.auth import (
    JwtBearerConfig,
    JwtVerificationError,
    parse_authorization_header,
    verify_bearer_jwt,
)


_permission_cache: dict[str, tuple[float, bool]] = {}
_permission_cache_version = 1


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
        return self.is_workspace_owner or _has_permission(self, permission)


def _normalize_id(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _matches_permission(required: str, granted_permissions: set[str]) -> bool:
    return any(
        granted == required
        or (granted.endswith(".*") and required.startswith(granted[:-1]))
        for granted in granted_permissions
    )


def _permission_service_config() -> tuple[str, str, str, float, int]:
    return (
        (
            os.getenv("USER_SERVICE_URL")
            or os.getenv("INTERA_USERS_SERVICE_URL")
            or os.getenv("AUTHORIZATION_SERVICE_URL")
            or ""
        ).rstrip("/"),
        (
            os.getenv("PERMISSION_EVALUATION_SERVICE_KEY")
            or os.getenv("INTERA_INTERNAL_SERVICE_KEY")
            or os.getenv("SUBSCRIPTION_SERVICE_KEY")
            or ""
        ),
        os.getenv("KAFKA_SERVICE_NAME") or os.getenv("SERVICE_NAME") or "kafka_a2a",
        float(os.getenv("PERMISSION_EVALUATION_TIMEOUT", "2.0")),
        int(os.getenv("PERMISSION_EVALUATION_CACHE_TTL_SECONDS", "3600")),
    )


def _permission_cache_key(*, context: AgentAuthContext, permission: str, service_name: str) -> str:
    raw = json.dumps(
        {
            "permission": permission,
            "platform": str(context.claims.get("platform") or "intera_ims"),
            "profile_id": context.profile_id,
            "service": service_name,
            "user_id": context.user_id,
            "version": _permission_cache_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _has_permission(context: AgentAuthContext, permission: str) -> bool:
    permission = str(permission or "").strip()
    if not permission:
        return False
    if _matches_permission(permission, context.permissions):
        return True

    base_url, service_key, service_name, timeout, default_ttl = _permission_service_config()
    if not base_url or not service_key:
        return False

    cache_key = _permission_cache_key(context=context, permission=permission, service_name=service_name)
    now = time.monotonic()
    cached = _permission_cache.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    payload = json.dumps(
        {
            "user_id": context.user_id,
            "profile_id": context.profile_id,
            "platform": str(context.claims.get("platform") or "intera_ims"),
            "service": service_name,
            "permissions": [permission],
        }
    ).encode("utf-8")
    req = urlrequest.Request(
        f"{base_url}/permission_api/internal/evaluate-permissions/",
        data=payload,
        headers={"Content-Type": "application/json", "X-Intera-Service-Key": service_key},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, error.HTTPError, error.URLError):
        return False

    allowed = bool((data.get("grants") or {}).get(permission))
    _permission_cache[cache_key] = (now + max(int(data.get("expires_in") or default_ttl), 1), allowed)
    return allowed


def invalidate_permission_cache() -> None:
    global _permission_cache_version
    _permission_cache_version += 1
    _permission_cache.clear()


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
