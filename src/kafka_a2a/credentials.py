from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from kafka_a2a.tenancy import KA2A_PRINCIPAL_METADATA_KEY, Principal, extract_principal


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(word[:1].upper() + word[1:] for word in parts[1:])


class Ka2aCredentialsModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="allow",
    )


KA2A_JWT_CLAIM_KEY = "ka2a"


class EncryptedSecret(Ka2aCredentialsModel):
    """
    An encrypted secret value embedded in JWT claims.

    `ciphertext` is an opaque string; its format depends on `alg`.
    """

    ciphertext: str
    alg: str | None = None
    kid: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"ciphertext": value}
        return value


class Ka2aLlmClaim(Ka2aCredentialsModel):
    """
    Minimal LLM selection/config shipped inside the JWT.

    This is intentionally provider-agnostic. Downstream code can map `provider`
    and the decrypted `api_key` into the specific SDK it wants.
    """

    provider: str
    model: str | None = None
    base_url: str | None = None
    api_key: EncryptedSecret | None = None
    extra: dict[str, Any] | None = None


class Ka2aMcpClaim(Ka2aCredentialsModel):
    """
    Placeholder for MCP auth. Keep this generic until your MCP tooling is finalized.
    """

    server_url: str | None = None
    token: EncryptedSecret | None = None
    extra: dict[str, Any] | None = None


class Ka2aTavilyClaim(Ka2aCredentialsModel):
    """
    Tavily credential claim for web-search enabled agents.
    """

    api_key: EncryptedSecret | None = None
    extra: dict[str, Any] | None = None


class Ka2aJwtClaim(Ka2aCredentialsModel):
    """
    Root K-A2A namespaced JWT claim (`ka2a`).
    """

    v: int = 1
    llm: Ka2aLlmClaim | None = None
    mcp: Ka2aMcpClaim | None = None
    tavily: Ka2aTavilyClaim | None = None


class ResolvedLlmCredentials(Ka2aCredentialsModel):
    provider: str
    api_key: str
    model: str | None = None
    base_url: str | None = None
    extra: dict[str, Any] | None = None


class ResolvedMcpCredentials(Ka2aCredentialsModel):
    server_url: str | None = None
    token: str | None = None
    extra: dict[str, Any] | None = None


class ResolvedTavilyCredentials(Ka2aCredentialsModel):
    api_key: str
    extra: dict[str, Any] | None = None


SecretDecryptor = Callable[[EncryptedSecret], str]


def extract_ka2a_jwt_claim(jwt_claims: dict[str, Any]) -> Ka2aJwtClaim | None:
    value = jwt_claims.get(KA2A_JWT_CLAIM_KEY)
    if value is None:
        # Also support flattened claim shapes like `ka2a.llm` / `ka2a.tavily` / `ka2a.mcp` / `ka2a.v`.
        # This is helpful for JWT issuers that don't easily support nested JSON objects.
        flattened: dict[str, Any] = {}

        def _set_path(root: dict[str, Any], parts: list[str], val: Any) -> None:
            cur: dict[str, Any] = root
            for name in parts[:-1]:
                next_obj = cur.get(name)
                if not isinstance(next_obj, dict):
                    next_obj = {}
                    cur[name] = next_obj
                cur = next_obj
            cur[parts[-1]] = val

        for k, v in jwt_claims.items():
            if not isinstance(k, str):
                continue
            if k == "ka2a_llm":
                _set_path(flattened, ["llm"], v)
                continue
            if k == "ka2a_mcp":
                _set_path(flattened, ["mcp"], v)
                continue
            if k == "ka2a_tavily":
                _set_path(flattened, ["tavily"], v)
                continue
            if not k.startswith(f"{KA2A_JWT_CLAIM_KEY}."):
                continue
            path = k[len(f"{KA2A_JWT_CLAIM_KEY}.") :]
            if not path:
                continue
            parts = [p for p in path.split(".") if p]
            if not parts:
                continue
            _set_path(flattened, parts, v)

        value = flattened or None
        if value is None:
            return None
    try:
        return Ka2aJwtClaim.model_validate(value)
    except Exception:
        return None


def resolve_llm_credentials_from_claims(
    *,
    jwt_claims: dict[str, Any],
    decrypt: SecretDecryptor,
) -> ResolvedLlmCredentials | None:
    ka2a = extract_ka2a_jwt_claim(jwt_claims)
    if ka2a is None or ka2a.llm is None:
        return None
    if ka2a.llm.api_key is None:
        raise ValueError("ka2a.llm.apiKey is required")
    api_key = decrypt(ka2a.llm.api_key)
    return ResolvedLlmCredentials(
        provider=ka2a.llm.provider,
        api_key=api_key,
        model=ka2a.llm.model,
        base_url=ka2a.llm.base_url,
        extra=ka2a.llm.extra,
    )


def resolve_llm_credentials_from_metadata(
    *,
    metadata: dict[str, Any] | None,
    decrypt: SecretDecryptor,
    principal_metadata_key: str = KA2A_PRINCIPAL_METADATA_KEY,
) -> ResolvedLlmCredentials | None:
    principal = extract_principal(metadata or {}, key=principal_metadata_key)
    if principal is None or principal.claims is None:
        return None
    return resolve_llm_credentials_from_claims(jwt_claims=principal.claims, decrypt=decrypt)


def resolve_mcp_credentials_from_claims(
    *,
    jwt_claims: dict[str, Any],
    decrypt: SecretDecryptor,
) -> ResolvedMcpCredentials | None:
    ka2a = extract_ka2a_jwt_claim(jwt_claims)
    if ka2a is None or ka2a.mcp is None:
        return None
    token = decrypt(ka2a.mcp.token) if ka2a.mcp.token is not None else None
    return ResolvedMcpCredentials(
        server_url=ka2a.mcp.server_url,
        token=token,
        extra=ka2a.mcp.extra,
    )


def resolve_mcp_credentials_from_metadata(
    *,
    metadata: dict[str, Any] | None,
    decrypt: SecretDecryptor,
    principal_metadata_key: str = KA2A_PRINCIPAL_METADATA_KEY,
) -> ResolvedMcpCredentials | None:
    principal = extract_principal(metadata or {}, key=principal_metadata_key)
    if principal is None or principal.claims is None:
        return None
    return resolve_mcp_credentials_from_claims(jwt_claims=principal.claims, decrypt=decrypt)


def resolve_tavily_credentials_from_claims(
    *,
    jwt_claims: dict[str, Any],
    decrypt: SecretDecryptor,
) -> ResolvedTavilyCredentials | None:
    ka2a = extract_ka2a_jwt_claim(jwt_claims)
    if ka2a is None or ka2a.tavily is None or ka2a.tavily.api_key is None:
        return None
    return ResolvedTavilyCredentials(
        api_key=decrypt(ka2a.tavily.api_key),
        extra=ka2a.tavily.extra,
    )


def resolve_tavily_credentials_from_metadata(
    *,
    metadata: dict[str, Any] | None,
    decrypt: SecretDecryptor,
    principal_metadata_key: str = KA2A_PRINCIPAL_METADATA_KEY,
) -> ResolvedTavilyCredentials | None:
    principal = extract_principal(metadata or {}, key=principal_metadata_key)
    if principal is None or principal.claims is None:
        return None
    return resolve_tavily_credentials_from_claims(jwt_claims=principal.claims, decrypt=decrypt)


def resolve_llm_credentials_from_env(
    *,
    env: Mapping[str, str] | None = None,
    provider_key: str = "KA2A_LLM_PROVIDER",
    model_key: str = "KA2A_LLM_MODEL",
    base_url_key: str = "KA2A_LLM_BASE_URL",
    api_key_key: str = "KA2A_LLM_API_KEY",
    api_key_env_key: str = "KA2A_LLM_API_KEY_ENV",
) -> ResolvedLlmCredentials | None:
    """
    Resolve LLM credentials from process environment variables.

    This supports "local/dev" usage where a single set of credentials is used for all requests.

    Required:
    - `KA2A_LLM_PROVIDER`
    - `KA2A_LLM_API_KEY` (or `KA2A_LLM_API_KEY_ENV=<OTHER_ENV_VAR_NAME>`)
    """

    env_map = env or os.environ

    provider = (env_map.get(provider_key) or "").strip()
    if not provider:
        return None

    model = (env_map.get(model_key) or "").strip() or None
    base_url = (env_map.get(base_url_key) or "").strip() or None

    api_key = (env_map.get(api_key_key) or "").strip() or None
    if not api_key:
        api_key_env = (env_map.get(api_key_env_key) or "").strip() or None
        if api_key_env:
            api_key = (env_map.get(api_key_env) or "").strip() or None

    if not api_key:
        provider_lower = provider.lower()
        fallback_env_vars: list[str] = []
        if provider_lower in ("openai", "openai_compat", "openai-compatible", "openai-compatible-api"):
            fallback_env_vars = ["OPENAI_API_KEY"]
        elif provider_lower in ("gemini", "google", "google_genai", "google-genai"):
            fallback_env_vars = ["GOOGLE_API_KEY", "GEMINI_API_KEY"]
        elif provider_lower in ("anthropic",):
            fallback_env_vars = ["ANTHROPIC_API_KEY"]
        elif provider_lower in ("groq",):
            fallback_env_vars = ["GROQ_API_KEY"]
        elif provider_lower in ("mistral",):
            fallback_env_vars = ["MISTRAL_API_KEY"]
        elif provider_lower in ("cohere",):
            fallback_env_vars = ["COHERE_API_KEY"]
        elif provider_lower in ("xai", "grok"):
            fallback_env_vars = ["XAI_API_KEY"]

        for name in fallback_env_vars:
            api_key = (env_map.get(name) or "").strip() or None
            if api_key:
                break

    if not api_key:
        raise ValueError(
            "Missing LLM API key. Set KA2A_LLM_API_KEY or KA2A_LLM_API_KEY_ENV (or provider-specific key like OPENAI_API_KEY)."
        )

    return ResolvedLlmCredentials(
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
    )


def resolve_tavily_credentials_from_env(
    *,
    env: Mapping[str, str] | None = None,
    api_key_key: str = "TAVILY_API_KEY",
    ka2a_api_key_key: str = "KA2A_TAVILY_API_KEY",
    api_key_env_key: str = "KA2A_TAVILY_API_KEY_ENV",
) -> ResolvedTavilyCredentials | None:
    env_map = env or os.environ

    api_key = (env_map.get(ka2a_api_key_key) or "").strip() or None
    if not api_key:
        api_key = (env_map.get(api_key_key) or "").strip() or None

    if not api_key:
        api_key_env = (env_map.get(api_key_env_key) or "").strip() or None
        if api_key_env:
            api_key = (env_map.get(api_key_env) or "").strip() or None

    if not api_key:
        return None

    return ResolvedTavilyCredentials(api_key=api_key)


def strip_principal_secrets_for_storage(
    *,
    metadata: dict[str, Any] | None,
    principal_metadata_key: str = KA2A_PRINCIPAL_METADATA_KEY,
) -> dict[str, Any] | None:
    """
    Return a copy of metadata suitable for persistence on the Task.

    Removes `bearerToken` and `claims` from the stored Principal (keeps `userId`/`tenantId`).
    """

    if not metadata:
        return metadata
    principal = extract_principal(metadata, key=principal_metadata_key)
    if principal is None:
        return dict(metadata)
    stored = Principal(user_id=principal.user_id, tenant_id=principal.tenant_id)
    clone = dict(metadata)
    clone[principal_metadata_key] = stored.model_dump(by_alias=True, exclude_none=True)
    return clone
