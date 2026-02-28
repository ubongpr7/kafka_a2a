from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kafka_a2a.tenancy import Principal


@dataclass(slots=True)
class JwtBearerConfig:
    """
    Configuration for verifying incoming user JWTs (typically issued by your SaaS).

    This is intentionally generic so it can work with Django/DRF SimpleJWT and similar setups.
    """

    # HS* => shared secret; RS*/ES* => public key (PEM)
    secret: str
    algorithms: list[str] = field(default_factory=lambda: ["HS256"])
    audience: str | None = None
    issuer: str | None = None
    leeway_s: int = 0

    user_claim: str = "sub"
    tenant_claim: str | None = None

    forward_bearer_token: bool = False
    include_claims: bool = False


class JwtVerificationError(Exception):
    pass


def verify_bearer_jwt(*, token: str, config: JwtBearerConfig) -> Principal:
    """
    Verify and decode a Bearer JWT and return a `Principal`.

    Requires `pyjwt` to be installed.
    """

    try:
        import jwt  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "JWT auth requires PyJWT. Install with: pip install 'kafka-a2a[auth]'"
        ) from exc

    options = {"verify_aud": config.audience is not None}
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            config.secret,
            algorithms=config.algorithms,
            audience=config.audience,
            issuer=config.issuer,
            leeway=config.leeway_s,
            options=options,
        )
    except Exception as exc:
        raise JwtVerificationError(str(exc)) from exc

    user_id = claims.get(config.user_claim) or claims.get("sub") or claims.get("user_id") or claims.get("id")
    if not user_id:
        raise JwtVerificationError(f"JWT missing user claim: {config.user_claim}")

    tenant_id = None
    if config.tenant_claim:
        tenant_id = claims.get(config.tenant_claim)

    return Principal(
        user_id=str(user_id),
        tenant_id=str(tenant_id) if tenant_id is not None else None,
        bearer_token=token if config.forward_bearer_token else None,
        claims=claims if config.include_claims else None,
    )


def parse_authorization_header(value: str | None) -> str:
    if not value:
        raise JwtVerificationError("Missing Authorization header")
    parts = value.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise JwtVerificationError("Invalid Authorization header")
    return parts[1]
