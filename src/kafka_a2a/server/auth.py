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

    # HS* => shared secret; RS*/ES* => public key (PEM). Optional if `jwks_url` is set.
    secret: str = ""
    algorithms: list[str] = field(default_factory=lambda: ["HS256"])
    audience: str | None = None
    issuer: str | None = None
    leeway_s: int = 0

    # JWKS support (recommended for RS*/ES* deployments with key rotation).
    jwks_url: str | None = None
    jwks_cache_lifespan_s: float = 300.0
    jwks_timeout_s: float = 30.0
    jwks_headers: dict[str, str] | None = None

    user_claim: str = "sub"
    tenant_claim: str | None = None

    forward_bearer_token: bool = False
    include_claims: bool = False

    # Cached JWKS client (created lazily). Stored here to preserve caching across requests.
    _jwks_client: Any | None = field(default=None, init=False, repr=False)


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
            "JWT auth requires PyJWT. Install the `auth` extra (e.g. `uv sync --extra auth`)."
        ) from exc

    options = {"verify_aud": config.audience is not None}
    try:
        key: Any = config.secret
        if config.jwks_url:
            if config._jwks_client is None:
                config._jwks_client = jwt.PyJWKClient(
                    config.jwks_url,
                    cache_keys=True,
                    lifespan=float(config.jwks_cache_lifespan_s),
                    timeout=float(config.jwks_timeout_s),
                    headers=dict(config.jwks_headers or {}),
                )
            key = config._jwks_client.get_signing_key_from_jwt(token).key

        claims: dict[str, Any] = jwt.decode(
            token,
            key,
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
