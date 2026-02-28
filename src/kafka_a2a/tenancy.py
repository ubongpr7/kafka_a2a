from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(word[:1].upper() + word[1:] for word in parts[1:])


class Ka2aTenancyModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="allow",
    )


KA2A_PRINCIPAL_METADATA_KEY = "urn:ka2a:principal"


class Principal(Ka2aTenancyModel):
    """
    A multi-tenant identity for SaaS-style deployments.

    This is typically derived by an HTTP gateway from an incoming user JWT, then
    forwarded as request metadata over Kafka so agents can enforce task access.

    NOTE: `bearer_token` is sensitive. Only include it if you explicitly intend for
    downstream agents/tools to call back into your SaaS on behalf of the user.
    """

    user_id: str = Field(..., min_length=1)
    tenant_id: str | None = None
    bearer_token: str | None = None
    claims: dict[str, Any] | None = None


def extract_principal(
    metadata: dict[str, Any] | None, *, key: str = KA2A_PRINCIPAL_METADATA_KEY
) -> Principal | None:
    if not metadata:
        return None
    value = metadata.get(key)
    if value is None:
        return None
    try:
        return Principal.model_validate(value)
    except Exception:
        return None


def with_principal(
    metadata: dict[str, Any] | None, principal: Principal, *, key: str = KA2A_PRINCIPAL_METADATA_KEY
) -> dict[str, Any]:
    merged = dict(metadata or {})
    merged[key] = principal.model_dump(by_alias=True, exclude_none=True)
    return merged


@dataclass(slots=True)
class PrincipalMatcher:
    """
    Helper for comparing principals consistently.
    """

    require_tenant_match: bool = True

    def matches(self, *, stored: Principal, request: Principal) -> bool:
        if stored.user_id != request.user_id:
            return False
        if not self.require_tenant_match:
            return True
        return (stored.tenant_id or "") == (request.tenant_id or "")
