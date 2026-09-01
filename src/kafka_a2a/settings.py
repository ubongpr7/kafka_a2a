from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast


CredentialsSource = Literal["env", "jwt", "auto"]


def _profile_id_from_metadata(metadata: dict[str, object] | None) -> str | None:
    from kafka_a2a.tenancy import extract_principal

    principal = extract_principal(metadata or {})
    if principal is not None:
        for candidate in (
            principal.tenant_id,
            principal.claims.get("profile_id") if isinstance(principal.claims, dict) else None,
            principal.claims.get("tenant_id") if isinstance(principal.claims, dict) else None,
        ):
            value = str(candidate or "").strip()
            if value:
                return value

    for key in ("profile_id", "tenant_id"):
        value = str((metadata or {}).get(key) or "").strip()
        if value:
            return value
    return None


def _resolve_llm_credentials_from_control_plane(
    *,
    metadata: dict[str, object] | None,
    env: Mapping[str, str] | None = None,
) -> ResolvedLlmCredentials | None:
    from kafka_a2a.control_plane import ControlPlaneClient
    from kafka_a2a.credentials import ResolvedLlmCredentials

    env_map = env or os.environ
    profile_id = _profile_id_from_metadata(metadata)
    if profile_id is None:
        profile_id = str(env_map.get("KA2A_WORKSPACE_PROFILE_ID") or "").strip() or None
    if not profile_id:
        return None

    client = ControlPlaneClient()
    if not client.enabled:
        return None

    try:
        payload = client.get_internal_workspace_ai_setup(profile_id=profile_id)
    except Exception:
        return None

    if not isinstance(payload, dict) or not payload.get("configured"):
        return None
    agent_payload = payload.get("agent")
    if not isinstance(agent_payload, dict):
        return None

    provider = str(agent_payload.get("provider") or env_map.get("KA2A_LLM_PROVIDER") or "").strip()
    api_key = str(agent_payload.get("api_key") or "").strip()
    if not provider or not api_key:
        return None

    model = str(agent_payload.get("model_name") or agent_payload.get("version") or env_map.get("KA2A_LLM_MODEL") or "").strip() or None
    base_url = str(agent_payload.get("effective_base_url") or "").strip() or None
    return ResolvedLlmCredentials(
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
    )


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    value = value.strip().lower()
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off"):
        return False
    return default


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def load_dotenv(path: str | os.PathLike[str] = ".env", *, override: bool = False) -> bool:
    """
    Minimal `.env` loader.

    - Supports `KEY=VALUE` lines (optionally `export KEY=...`)
    - Ignores empty lines and `#` comments
    - Does not do variable interpolation
    - By default does NOT override existing `os.environ` values
    """

    env_path = Path(path)
    if not env_path.exists():
        return False

    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = _strip_quotes(value)
        if not override and key in os.environ:
            continue
        os.environ[key] = value

    return True


if TYPE_CHECKING:
    from collections.abc import Mapping

    from kafka_a2a.credentials import ResolvedLlmCredentials, SecretDecryptor


@dataclass(slots=True)
class Ka2aSettings:
    """
    Lightweight runtime settings loader for K-A2A.

    This is intentionally dependency-free (no `python-dotenv`, no `pydantic-settings`).
    """

    load_dotenv: bool = False
    dotenv_path: str = ".env"

    # Where to load user LLM credentials from:
    # - "env": process environment (single-tenant / local dev)
    # - "jwt": request metadata -> Principal.claims -> `ka2a` claim (SaaS)
    # - "auto": try JWT first, then env
    llm_credentials_source: CredentialsSource = "env"
    email_system_from_email: str = "Intera IMS <noreply@interaims.com>"
    email_agent_from_email: str = "Intera Agent <intera-agent@interaims.com>"
    email_default_reply_to: str = "support@interaims.com"
    email_support_email: str = "support@interaims.com"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Ka2aSettings:
        env_map = env or os.environ

        load_dotenv_flag = _parse_bool(env_map.get("KA2A_LOAD_DOTENV"), default=False)
        dotenv_path = env_map.get("KA2A_DOTENV_PATH", ".env")
        if load_dotenv_flag:
            load_dotenv(dotenv_path, override=False)
            env_map = os.environ

        # Allow either key name for convenience.
        source = (env_map.get("KA2A_LLM_CREDENTIALS_SOURCE") or env_map.get("KA2A_CREDENTIALS_SOURCE") or "env").strip().lower()

        # Boolean aliases (matches common "LOAD_FROM_ENV=true" style).
        if _parse_bool(env_map.get("KA2A_LOAD_FROM_ENV") or env_map.get("KA2A_USE_ENV_CREDENTIALS"), default=False):
            source = "env"

        if source not in ("env", "jwt", "auto"):
            raise ValueError(
                "Invalid KA2A_LLM_CREDENTIALS_SOURCE. Expected one of: env, jwt, auto"
            )

        return cls(
            load_dotenv=load_dotenv_flag,
            dotenv_path=str(dotenv_path),
            llm_credentials_source=cast(CredentialsSource, source),
            email_system_from_email=(
                env_map.get("EMAIL_SYSTEM_FROM_EMAIL")
                or env_map.get("DEFAULT_FROM_EMAIL")
                or "Intera IMS <noreply@interaims.com>"
            ).strip(),
            email_agent_from_email=(
                env_map.get("EMAIL_AGENT_FROM_EMAIL")
                or "Intera Agent <intera-agent@interaims.com>"
            ).strip(),
            email_default_reply_to=(
                env_map.get("EMAIL_DEFAULT_REPLY_TO")
                or env_map.get("EMAIL_SUPPORT_EMAIL")
                or "support@interaims.com"
            ).strip(),
            email_support_email=(env_map.get("EMAIL_SUPPORT_EMAIL") or "support@interaims.com").strip(),
        )

    def resolve_llm_credentials(
        self,
        *,
        metadata: dict[str, object] | None = None,
        decrypt: SecretDecryptor | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ResolvedLlmCredentials | None:
        from kafka_a2a.credentials import resolve_llm_credentials_from_env, resolve_llm_credentials_from_metadata

        if self.llm_credentials_source == "env":
            return resolve_llm_credentials_from_env(env=env)
        if self.llm_credentials_source == "jwt":
            if decrypt is None:
                raise ValueError("decrypt is required when KA2A_LLM_CREDENTIALS_SOURCE=jwt")
            try:
                resolved = resolve_llm_credentials_from_metadata(metadata=metadata, decrypt=decrypt)
            except Exception:
                resolved = None
            if resolved is not None:
                return resolved
            return _resolve_llm_credentials_from_control_plane(metadata=metadata, env=env)
        if self.llm_credentials_source == "auto":
            if decrypt is not None:
                try:
                    resolved = resolve_llm_credentials_from_metadata(metadata=metadata, decrypt=decrypt)
                except Exception:
                    resolved = None
                if resolved is not None:
                    return resolved
            resolved = _resolve_llm_credentials_from_control_plane(metadata=metadata, env=env)
            if resolved is not None:
                return resolved
            return resolve_llm_credentials_from_env(env=env)

        raise AssertionError(f"Unhandled credentials source: {self.llm_credentials_source}")
