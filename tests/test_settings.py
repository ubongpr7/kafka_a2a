from __future__ import annotations

import os

import pytest

from kafka_a2a.credentials import KA2A_JWT_CLAIM_KEY, resolve_llm_credentials_from_env
from kafka_a2a.settings import Ka2aSettings, load_dotenv
from kafka_a2a.tenancy import Principal, with_principal


def test_load_dotenv_sets_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("KA2A_LLM_PROVIDER=openai\nKA2A_LLM_API_KEY=sk-test\n", encoding="utf-8")

    monkeypatch.delenv("KA2A_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("KA2A_LLM_API_KEY", raising=False)

    assert load_dotenv(env_file) is True
    assert os.environ["KA2A_LLM_PROVIDER"] == "openai"
    assert os.environ["KA2A_LLM_API_KEY"] == "sk-test"


def test_load_dotenv_does_not_override_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("KA2A_LLM_PROVIDER=openai\n", encoding="utf-8")

    monkeypatch.setenv("KA2A_LLM_PROVIDER", "keep-me")
    assert load_dotenv(env_file, override=False) is True
    assert os.environ["KA2A_LLM_PROVIDER"] == "keep-me"


def test_settings_from_env_can_load_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("KA2A_LLM_CREDENTIALS_SOURCE=env\n", encoding="utf-8")

    monkeypatch.setenv("KA2A_LOAD_DOTENV", "true")
    monkeypatch.setenv("KA2A_DOTENV_PATH", str(env_file))

    settings = Ka2aSettings.from_env()
    assert settings.load_dotenv is True
    assert settings.llm_credentials_source == "env"


def test_settings_from_env_boolean_alias_for_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KA2A_LLM_CREDENTIALS_SOURCE", "jwt")
    monkeypatch.setenv("KA2A_USE_ENV_CREDENTIALS", "true")
    settings = Ka2aSettings.from_env()
    assert settings.llm_credentials_source == "env"


def test_resolve_llm_credentials_from_env_direct() -> None:
    env = {"KA2A_LLM_PROVIDER": "openai", "KA2A_LLM_API_KEY": "sk-test", "KA2A_LLM_MODEL": "gpt-4.1-mini"}
    cred = resolve_llm_credentials_from_env(env=env)
    assert cred is not None
    assert cred.provider == "openai"
    assert cred.model == "gpt-4.1-mini"
    assert cred.api_key == "sk-test"


def test_resolve_llm_credentials_from_env_api_key_env_name() -> None:
    env = {"KA2A_LLM_PROVIDER": "openai", "KA2A_LLM_API_KEY_ENV": "OPENAI_API_KEY", "OPENAI_API_KEY": "sk-test"}
    cred = resolve_llm_credentials_from_env(env=env)
    assert cred is not None
    assert cred.api_key == "sk-test"


def test_resolve_llm_credentials_from_env_requires_key_when_provider_set() -> None:
    with pytest.raises(ValueError):
        resolve_llm_credentials_from_env(env={"KA2A_LLM_PROVIDER": "openai"})


def test_settings_resolve_llm_credentials_env() -> None:
    settings = Ka2aSettings(llm_credentials_source="env")
    env = {"KA2A_LLM_PROVIDER": "gemini", "KA2A_LLM_API_KEY": "key"}
    cred = settings.resolve_llm_credentials(env=env)
    assert cred is not None
    assert cred.provider == "gemini"
    assert cred.api_key == "key"


def test_settings_resolve_llm_credentials_jwt_requires_decrypt() -> None:
    settings = Ka2aSettings(llm_credentials_source="jwt")
    principal = Principal(
        user_id="u1",
        claims={
            KA2A_JWT_CLAIM_KEY: {"v": 1, "llm": {"provider": "openai", "apiKey": "ENC"}},
        },
    )
    metadata = with_principal({}, principal)

    with pytest.raises(ValueError):
        settings.resolve_llm_credentials(metadata=metadata)

