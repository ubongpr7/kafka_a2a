from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

from kafka_a2a.mainapps.common.auth import build_agent_auth_context
from kafka_a2a.server.auth import JwtBearerConfig


def test_authorization_context_expands_bound_wildcard_permissions() -> None:
    secret = "authorization-context-test-secret"
    config = JwtBearerConfig(secret=secret, algorithms=["HS256"], include_claims=True)
    now = datetime.now(timezone.utc)
    access_token = jwt.encode(
        {"sub": "user-1", "profile_id": "profile-1", "permissions": [], "exp": now + timedelta(minutes=5)},
        secret,
        algorithm="HS256",
    )
    context_token = jwt.encode(
        {
            "token_type": "intera_authorization_context",
            "user_id": "user-1",
            "profile_id": "profile-1",
            "permissions": [],
            "wildcards": ["system:administrator"],
            "wildcard_permissions": {"system:administrator": ["manage_agent_settings"]},
            "exp": now + timedelta(minutes=5),
        },
        secret,
        algorithm="HS256",
    )

    context = build_agent_auth_context(
        token=access_token,
        jwt=config,
        context_token=context_token,
    )

    assert context.has_permission("manage_agent_settings")
