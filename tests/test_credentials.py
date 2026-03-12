from __future__ import annotations

import pytest

from kafka_a2a.credentials import (
    KA2A_JWT_CLAIM_KEY,
    resolve_llm_credentials_from_claims,
    resolve_llm_credentials_from_metadata,
    resolve_mcp_credentials_from_claims,
    resolve_mcp_credentials_from_metadata,
    resolve_tavily_credentials_from_claims,
    resolve_tavily_credentials_from_env,
    resolve_tavily_credentials_from_metadata,
    strip_principal_secrets_for_storage,
)
from kafka_a2a.models import Message, TextPart, Task
from kafka_a2a.protocol import METHOD_MESSAGE_SEND, RpcRequest
from kafka_a2a.runtime.agent import Ka2aAgent, Ka2aAgentConfig
from kafka_a2a.tenancy import KA2A_PRINCIPAL_METADATA_KEY, Principal, with_principal
from kafka_a2a.transport.kafka import KafkaConfig, KafkaEnvelope, KafkaTransport


def test_resolve_llm_credentials_from_claims() -> None:
    claims = {
        KA2A_JWT_CLAIM_KEY: {
            "v": 1,
            "llm": {"provider": "openai", "model": "gpt-4.1-mini", "apiKey": "ENC"},
        }
    }
    cred = resolve_llm_credentials_from_claims(jwt_claims=claims, decrypt=lambda _: "sk-test")
    assert cred is not None
    assert cred.provider == "openai"
    assert cred.model == "gpt-4.1-mini"
    assert cred.api_key == "sk-test"


def test_resolve_llm_credentials_from_flattened_claims() -> None:
    claims = {
        "ka2a.v": 1,
        "ka2a.llm.provider": "gemini",
        "ka2a.llm.model": "gemini-1.5-flash",
        "ka2a.llm.apiKey": "ENC",
    }
    cred = resolve_llm_credentials_from_claims(jwt_claims=claims, decrypt=lambda _: "key")
    assert cred is not None
    assert cred.provider == "gemini"
    assert cred.model == "gemini-1.5-flash"
    assert cred.api_key == "key"


def test_resolve_llm_credentials_from_metadata() -> None:
    principal = Principal(
        user_id="u1",
        tenant_id="acme",
        claims={
            KA2A_JWT_CLAIM_KEY: {
                "v": 1,
                "llm": {"provider": "gemini", "apiKey": {"ciphertext": "ENC", "alg": "A256GCM"}},
            }
        },
    )
    metadata = with_principal({}, principal)
    cred = resolve_llm_credentials_from_metadata(metadata=metadata, decrypt=lambda _: "key")
    assert cred is not None
    assert cred.provider == "gemini"
    assert cred.api_key == "key"


def test_strip_principal_secrets_for_storage() -> None:
    principal = Principal(user_id="u1", tenant_id="acme", bearer_token="tok", claims={"a": 1})
    metadata = with_principal({}, principal)
    stripped = strip_principal_secrets_for_storage(metadata=metadata)
    assert stripped is not None
    stored = stripped[KA2A_PRINCIPAL_METADATA_KEY]
    assert stored["userId"] == "u1"
    assert "bearerToken" not in stored
    assert "claims" not in stored


def test_resolve_mcp_credentials_from_claims() -> None:
    claims = {
        KA2A_JWT_CLAIM_KEY: {
            "v": 1,
            "mcp": {"serverUrl": "https://mcp.example", "token": "ENC"},
        }
    }
    cred = resolve_mcp_credentials_from_claims(jwt_claims=claims, decrypt=lambda _: "tok")
    assert cred is not None
    assert cred.server_url == "https://mcp.example"
    assert cred.token == "tok"


def test_resolve_mcp_credentials_from_metadata() -> None:
    principal = Principal(
        user_id="u1",
        tenant_id="acme",
        claims={
            KA2A_JWT_CLAIM_KEY: {
                "v": 1,
                "mcp": {"serverUrl": "https://mcp.example", "token": {"ciphertext": "ENC"}},
            }
        },
    )
    metadata = with_principal({}, principal)
    cred = resolve_mcp_credentials_from_metadata(metadata=metadata, decrypt=lambda _: "tok")
    assert cred is not None
    assert cred.server_url == "https://mcp.example"
    assert cred.token == "tok"


def test_resolve_tavily_credentials_from_claims() -> None:
    claims = {
        KA2A_JWT_CLAIM_KEY: {
            "v": 1,
            "tavily": {"apiKey": "ENC"},
        }
    }
    cred = resolve_tavily_credentials_from_claims(jwt_claims=claims, decrypt=lambda _: "tvly")
    assert cred is not None
    assert cred.api_key == "tvly"


def test_resolve_tavily_credentials_from_flattened_claims() -> None:
    claims = {
        "ka2a.v": 1,
        "ka2a.tavily.apiKey": "ENC",
    }
    cred = resolve_tavily_credentials_from_claims(jwt_claims=claims, decrypt=lambda _: "tvly")
    assert cred is not None
    assert cred.api_key == "tvly"


def test_resolve_tavily_credentials_from_flattened_alias_claims() -> None:
    claims = {
        "ka2a_tavily": {"apiKey": "ENC"},
    }
    cred = resolve_tavily_credentials_from_claims(jwt_claims=claims, decrypt=lambda _: "tvly")
    assert cred is not None
    assert cred.api_key == "tvly"


def test_resolve_tavily_credentials_from_metadata() -> None:
    principal = Principal(
        user_id="u1",
        tenant_id="acme",
        claims={
            KA2A_JWT_CLAIM_KEY: {
                "v": 1,
                "tavily": {"apiKey": {"ciphertext": "ENC", "alg": "fernet"}},
            }
        },
    )
    metadata = with_principal({}, principal)
    cred = resolve_tavily_credentials_from_metadata(metadata=metadata, decrypt=lambda _: "tvly")
    assert cred is not None
    assert cred.api_key == "tvly"


def test_resolve_tavily_credentials_from_env() -> None:
    cred = resolve_tavily_credentials_from_env(env={"KA2A_TAVILY_API_KEY": "tvly-key"})
    assert cred is not None
    assert cred.api_key == "tvly-key"


@pytest.mark.asyncio
async def test_agent_does_not_persist_principal_secrets_by_default() -> None:
    transport = KafkaTransport(KafkaConfig(bootstrap_servers="localhost:9092"))
    agent = Ka2aAgent(config=Ka2aAgentConfig(agent_name="t", tenant_isolation=True), transport=transport)

    principal = Principal(user_id="u1", tenant_id="acme", bearer_token="tok", claims={"a": 1})
    metadata = with_principal({}, principal)

    msg = Message(role="user", parts=[TextPart(text="hi")])
    req = RpcRequest(id="1", method=METHOD_MESSAGE_SEND, params={"message": msg.model_dump(by_alias=True), "metadata": metadata})
    env = KafkaEnvelope(type="request", sender="c", recipient="t", reply_to="reply", payload=req.model_dump())
    result = await agent._route(req, env)  # type: ignore[attr-defined]
    task = Task.model_validate(result)

    assert task.metadata is not None
    stored = task.metadata[KA2A_PRINCIPAL_METADATA_KEY]
    assert stored["userId"] == "u1"
    assert "bearerToken" not in stored
    assert "claims" not in stored


@pytest.mark.asyncio
async def test_agent_can_persist_principal_secrets_when_enabled() -> None:
    transport = KafkaTransport(KafkaConfig(bootstrap_servers="localhost:9092"))
    agent = Ka2aAgent(
        config=Ka2aAgentConfig(
            agent_name="t",
            tenant_isolation=True,
            store_principal_secrets=True,
        ),
        transport=transport,
    )

    principal = Principal(user_id="u1", tenant_id="acme", bearer_token="tok", claims={"a": 1})
    metadata = with_principal({}, principal)

    msg = Message(role="user", parts=[TextPart(text="hi")])
    req = RpcRequest(id="1", method=METHOD_MESSAGE_SEND, params={"message": msg.model_dump(by_alias=True), "metadata": metadata})
    env = KafkaEnvelope(type="request", sender="c", recipient="t", reply_to="reply", payload=req.model_dump())
    result = await agent._route(req, env)  # type: ignore[attr-defined]
    task = Task.model_validate(result)

    assert task.metadata is not None
    stored = task.metadata[KA2A_PRINCIPAL_METADATA_KEY]
    assert stored["userId"] == "u1"
    assert stored["bearerToken"] == "tok"
    assert stored["claims"] == {"a": 1}
