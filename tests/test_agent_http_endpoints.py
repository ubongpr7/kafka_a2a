from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

jwt = pytest.importorskip("jwt")
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from kafka_a2a.secrets import clear_fernet_cache  # noqa: E402
from kafka_a2a.errors import A2AError, A2AErrorCode  # noqa: E402
from kafka_a2a.models import AgentCard  # noqa: E402
from kafka_a2a.server.auth import JwtBearerConfig  # noqa: E402
from kafka_a2a.server.gateway import GatewayConfig, create_gateway_app  # noqa: E402


JWT_SECRET = "test-secret-key-for-hs256-signing-123"
RUNTIME_SYNC_TOKEN = "runtime-sync-token"


async def _noop_async(self) -> None:
    return None


def _make_token(
    *,
    user_id: str,
    profile_id: str,
    owner_id: str | None = None,
    permissions: list[str] | None = None,
) -> str:
    claims = {
        "sub": user_id,
        "user_id": user_id,
        "profile_id": profile_id,
        "permissions": permissions or [],
        "token_type": "intera_websocket_ticket",
    }
    if owner_id is not None:
        claims["owner_id"] = owner_id
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


@contextmanager
def _gateway_client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[TestClient]:
    monkeypatch.setenv("KA2A_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KA2A_CONTROL_PLANE_STORE_PATH", str(tmp_path / "control-plane.json"))
    monkeypatch.setenv("KA2A_CHAT_STORE", "memory")
    monkeypatch.setenv("KA2A_RUNTIME_SHARED_TOKEN", RUNTIME_SYNC_TOKEN)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("KA2A_DATABASE_URL", raising=False)
    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.start", _noop_async)
    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stop", _noop_async)
    monkeypatch.setattr("kafka_a2a.registry.directory.KafkaAgentDirectory.start", _noop_async)
    monkeypatch.setattr("kafka_a2a.registry.directory.KafkaAgentDirectory.stop", _noop_async)

    app = create_gateway_app(
        GatewayConfig(
            bootstrap_servers="localhost:9092",
            default_agent="host",
            jwt=JwtBearerConfig(
                secret=JWT_SECRET,
                algorithms=["HS256"],
                user_claim="sub",
                include_claims=True,
            ),
        )
    )
    with TestClient(app) as client:
        yield client


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _FakeEvent:
    def __init__(self, payload: dict[str, object], **attrs: object) -> None:
        self._payload = payload
        for key, value in attrs.items():
            setattr(self, key, value)

    def model_dump(self, **_: object) -> dict[str, object]:
        return self._payload


def test_agent_setup_endpoints_allow_workspace_owner_without_explicit_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    owner_token = _make_token(user_id="17", profile_id="1", owner_id="17")

    with _gateway_client(monkeypatch, tmp_path) as client:
        templates_response = client.get("/agent_api/templates/", headers=_auth_headers(owner_token))
        assert templates_response.status_code == 200
        assert len(templates_response.json()) >= 1

        setup_response = client.post(
            "/agent_api/management/agent-setup/",
            headers=_auth_headers(owner_token),
            json={
                "name": "Chat GPT",
                "provider": "chatgpt",
                "provider_label": "ChatGPT",
                "version": "gpt-4.1-mini",
                "api_key": "sk-test-owner",
                "tavily_api_key": "tvly-owner",
            },
        )
        assert setup_response.status_code == 200
        payload = setup_response.json()
        assert payload["configured"] is True
        assert payload["agent"]["name"] == "Chat GPT"
        assert payload["agent"]["has_api_key"] is True
        assert payload["agent"]["has_tavily_api_key"] is True

        registry_response = client.get(
            "/agent_api/runtime/agents/registry/",
            headers=_auth_headers(owner_token),
        )
        assert registry_response.status_code == 200
        registry_payload = registry_response.json()
        assert registry_payload["agent_count"] >= 6
        assert {
            "host",
            "inventory",
            "inventory_visibility",
            "inventory_procurement",
            "pos_admin",
            "product_discovery",
            "users",
        }.issubset(
            {item["slug"] for item in registry_payload["agents"]}
        )


def test_agent_setup_endpoints_require_manage_permission_for_non_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    token = _make_token(user_id="22", profile_id="1", owner_id="17", permissions=["interact_with_agent"])

    with _gateway_client(monkeypatch, tmp_path) as client:
        response = client.get("/agent_api/templates/", headers=_auth_headers(token))
        assert response.status_code == 403
        assert response.json()["detail"] == "Missing permission: manage_agent_settings"


def test_management_tavily_credential_returns_live_workspace_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    owner_token = _make_token(user_id="17", profile_id="1", owner_id="17")

    with _gateway_client(monkeypatch, tmp_path) as client:
        setup_response = client.post(
            "/agent_api/management/agent-setup/",
            headers=_auth_headers(owner_token),
            json={
                "name": "Chat GPT",
                "provider": "chatgpt",
                "provider_label": "ChatGPT",
                "version": "gpt-4.1-mini",
                "api_key": "sk-test-owner",
                "tavily_api_key": "tvly-owner",
            },
        )
        assert setup_response.status_code == 200

        credential_response = client.get(
            "/agent_api/management/tavily-credential/",
            headers=_auth_headers(owner_token),
        )
        assert credential_response.status_code == 200
        assert credential_response.json() == {
            "profile_id": "1",
            "has_tavily_api_key": True,
            "tavily_api_key": "tvly-owner",
        }


def test_management_tavily_credential_falls_back_to_env_when_workspace_secret_is_unusable(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    owner_token = _make_token(user_id="17", profile_id="1", owner_id="17")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-env-fallback")
    monkeypatch.setenv("KA2A_FERNET_KEY", Fernet.generate_key().decode())
    clear_fernet_cache()

    with _gateway_client(monkeypatch, tmp_path) as client:
        setup_response = client.post(
            "/agent_api/management/agent-setup/",
            headers=_auth_headers(owner_token),
            json={
                "name": "Chat GPT",
                "provider": "chatgpt",
                "provider_label": "ChatGPT",
                "version": "gpt-4.1-mini",
                "api_key": "sk-test-owner",
                "tavily_api_key": "tvly-owner",
            },
        )
        assert setup_response.status_code == 200

        # Simulate a runtime that can no longer decrypt the stored workspace secret.
        monkeypatch.delenv("KA2A_FERNET_KEY", raising=False)
        monkeypatch.delenv("FERNET_KEY", raising=False)
        clear_fernet_cache()

        credential_response = client.get(
            "/agent_api/management/tavily-credential/",
            headers=_auth_headers(owner_token),
        )
        assert credential_response.status_code == 200
        assert credential_response.json() == {
            "profile_id": "1",
            "has_tavily_api_key": True,
            "tavily_api_key": "tvly-env-fallback",
        }


def test_runtime_registry_hides_private_agents_from_other_workspace_users(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manager_token = _make_token(
        user_id="31",
        profile_id="9",
        owner_id="99",
        permissions=["manage_agent_settings", "interact_with_agent"],
    )
    coworker_token = _make_token(
        user_id="32",
        profile_id="9",
        owner_id="99",
        permissions=["interact_with_agent"],
    )

    with _gateway_client(monkeypatch, tmp_path) as client:
        templates = client.get("/agent_api/templates/", headers=_auth_headers(manager_token))
        assert templates.status_code == 200
        template = templates.json()[0]

        install_response = client.post(
            f"/agent_api/templates/{template['id']}/install/",
            headers=_auth_headers(manager_token),
            json={"slug": "workspace-host", "name": "Workspace Host"},
        )
        assert install_response.status_code == 200
        assert install_response.json()["slug"] == "workspace-host"

        private_response = client.post(
            "/agent_api/workspace-agents/",
            headers=_auth_headers(manager_token),
            json={
                "slug": "private-ops",
                "name": "Private Ops",
                "visibility": "private",
                "description": "Private specialist",
            },
        )
        assert private_response.status_code == 201
        assert private_response.json()["visibility"] == "private"

        owner_registry = client.get(
            "/agent_api/runtime/agents/registry/",
            headers=_auth_headers(manager_token),
        )
        assert owner_registry.status_code == 200
        owner_slugs = {item["slug"] for item in owner_registry.json()["agents"]}
        assert owner_slugs == {"workspace-host", "private-ops"}

        coworker_registry = client.get(
            "/agent_api/runtime/agents/registry/",
            headers=_auth_headers(coworker_token),
        )
        assert coworker_registry.status_code == 200
        coworker_slugs = {item["slug"] for item in coworker_registry.json()["agents"]}
        assert coworker_slugs == {"workspace-host"}


def test_runtime_internal_registry_requires_shared_runtime_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manager_token = _make_token(
        user_id="41",
        profile_id="11",
        owner_id="99",
        permissions=["manage_agent_settings"],
    )

    with _gateway_client(monkeypatch, tmp_path) as client:
        templates = client.get("/agent_api/templates/", headers=_auth_headers(manager_token))
        template = templates.json()[0]
        install_response = client.post(
            f"/agent_api/templates/{template['id']}/install/",
            headers=_auth_headers(manager_token),
            json={"slug": "runtime-host", "name": "Runtime Host"},
        )
        assert install_response.status_code == 200

        denied = client.get("/agent_api/runtime/internal/registry/")
        assert denied.status_code == 403

        allowed = client.get(
            "/agent_api/runtime/internal/registry/",
            headers={"X-KA2A-Runtime-Token": RUNTIME_SYNC_TOKEN},
        )
        assert allowed.status_code == 200
        slugs = {item["slug"] for item in allowed.json()["agents"]}
        assert slugs == {"runtime-host"}


def test_runtime_internal_workspace_ai_setup_returns_decrypted_workspace_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    owner_token = _make_token(
        user_id="81",
        profile_id="51",
        owner_id="81",
        permissions=["manage_agent_settings", "interact_with_agent"],
    )

    with _gateway_client(monkeypatch, tmp_path) as client:
        setup_response = client.post(
            "/agent_api/management/agent-setup/",
            headers=_auth_headers(owner_token),
            json={
                "name": "Voice GPT",
                "provider": "chatgpt",
                "provider_label": "ChatGPT",
                "version": "gpt-4.1-mini",
                "api_key": "sk-test-voice",
                "tavily_api_key": "tvly-voice",
            },
        )
        assert setup_response.status_code == 200

        denied = client.get("/agent_api/runtime/internal/workspaces/51/ai-setup/")
        assert denied.status_code == 403

        allowed = client.get(
            "/agent_api/runtime/internal/workspaces/51/ai-setup/",
            headers={"X-KA2A-Runtime-Token": RUNTIME_SYNC_TOKEN},
        )
        assert allowed.status_code == 200
        payload = allowed.json()
        assert payload["configured"] is True
        assert payload["agent"]["name"] == "Voice GPT"
        assert payload["agent"]["api_key"] == "sk-test-voice"
        assert payload["agent"]["tavily_api_key"] == "tvly-voice"
        assert payload["agent"]["has_api_key"] is True
        assert payload["agent"]["has_tavily_api_key"] is True


def test_conversation_crud_is_scoped_to_workspace_user_and_installed_agents(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manager_token = _make_token(
        user_id="51",
        profile_id="21",
        owner_id="99",
        permissions=["manage_agent_settings", "interact_with_agent"],
    )
    coworker_token = _make_token(
        user_id="52",
        profile_id="21",
        owner_id="99",
        permissions=["interact_with_agent"],
    )

    with _gateway_client(monkeypatch, tmp_path) as client:
        templates = client.get("/agent_api/templates/", headers=_auth_headers(manager_token))
        template = templates.json()[0]
        install_response = client.post(
            f"/agent_api/templates/{template['id']}/install/",
            headers=_auth_headers(manager_token),
            json={"slug": "chat-host", "name": "Chat Host"},
        )
        assert install_response.status_code == 200

        created = client.post(
            "/conversations",
            headers=_auth_headers(manager_token),
            json={"agent_slug": "chat-host", "title": "Ops thread", "history_length": 12},
        )
        assert created.status_code == 200
        created_payload = created.json()
        conversation_id = created_payload["conversation"]["id"]
        assert created_payload["conversation"]["agentSlug"] == "chat-host"
        assert created_payload["messages"] == []
        assert created_payload["activities"] == []

        listing = client.get("/conversations", headers=_auth_headers(manager_token))
        assert listing.status_code == 200
        assert [item["id"] for item in listing.json()] == [conversation_id]

        detail = client.get(f"/conversations/{conversation_id}", headers=_auth_headers(manager_token))
        assert detail.status_code == 200
        assert detail.json()["conversation"]["title"] == "Ops thread"

        updated = client.patch(
            f"/conversations/{conversation_id}",
            headers=_auth_headers(manager_token),
            json={"title": "Renamed thread", "status": "archived", "history_length": 7},
        )
        assert updated.status_code == 200
        updated_payload = updated.json()
        assert updated_payload["title"] == "Renamed thread"
        assert updated_payload["status"] == "archived"
        assert updated_payload["historyLength"] == 7

        foreign_read = client.get(f"/conversations/{conversation_id}", headers=_auth_headers(coworker_token))
        assert foreign_read.status_code == 404

        deleted = client.delete(f"/conversations/{conversation_id}", headers=_auth_headers(manager_token))
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True, "conversationId": conversation_id}

        after_delete = client.get(f"/conversations/{conversation_id}", headers=_auth_headers(manager_token))
        assert after_delete.status_code == 404


def test_conversation_websocket_stream_bootstraps_snapshot_and_supports_ping(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manager_token = _make_token(
        user_id="61",
        profile_id="31",
        owner_id="99",
        permissions=["manage_agent_settings", "interact_with_agent"],
    )

    with _gateway_client(monkeypatch, tmp_path) as client:
        templates = client.get("/agent_api/templates/", headers=_auth_headers(manager_token))
        template = templates.json()[0]
        install_response = client.post(
            f"/agent_api/templates/{template['id']}/install/",
            headers=_auth_headers(manager_token),
            json={"slug": "socket-host", "name": "Socket Host"},
        )
        assert install_response.status_code == 200

        created = client.post(
            "/conversations",
            headers=_auth_headers(manager_token),
            json={"agent_slug": "socket-host", "title": "Socket thread"},
        )
        assert created.status_code == 200
        conversation_id = created.json()["conversation"]["id"]

        with client.websocket_connect(
            f"/ws/conversations/{conversation_id}?ws_ticket={manager_token}"
        ) as websocket:
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "conversation.snapshot"
            assert snapshot["conversation"]["id"] == conversation_id
            assert snapshot["messages"] == []
            assert snapshot["activities"] == []

            websocket.send_json({"type": "ping"})
            pong = websocket.receive_json()
            assert pong == {"type": "pong"}

            websocket.send_json({"type": "message.send", "text": ""})
            error = websocket.receive_json()
            assert error["type"] == "error"
            assert error["message"] == "Message text is required."


def test_conversation_activities_persist_delegation_and_final_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manager_token = _make_token(
        user_id="71",
        profile_id="41",
        owner_id="99",
        permissions=["manage_agent_settings", "interact_with_agent"],
    )

    async def _fake_stream_message(self, **_: object):
        async def _stream():
            task_status = SimpleNamespace(state="working")
            yield _FakeEvent(
                {
                    "kind": "task",
                    "id": "task-71",
                    "contextId": "ctx-71",
                    "status": {"state": "working"},
                },
                kind="task",
                id="task-71",
                context_id="ctx-71",
                status=task_status,
            )
            artifact_payload = {
                "name": "delegation",
                "parts": [
                    {
                        "kind": "data",
                        "data": {
                            "selectedAgent": "inventory_visibility_agent",
                        },
                    }
                ],
            }
            yield _FakeEvent(
                {
                    "kind": "artifact-update",
                    "taskId": "task-71",
                    "contextId": "ctx-71",
                    "artifact": artifact_payload,
                },
                kind="artifact-update",
                task_id="task-71",
                context_id="ctx-71",
                artifact=SimpleNamespace(model_dump=lambda **__: artifact_payload),
            )
            message_payload = {
                "role": "assistant",
                "messageId": "msg-71",
                "parts": [
                    {
                        "kind": "text",
                        "text": "I need your approval before I continue.",
                    }
                ],
            }
            final_status = SimpleNamespace(
                state="input-required",
                message=SimpleNamespace(model_dump=lambda **__: message_payload),
            )
            yield _FakeEvent(
                {
                    "kind": "status-update",
                    "taskId": "task-71",
                    "contextId": "ctx-71",
                    "status": {
                        "state": "input-required",
                        "message": message_payload,
                    },
                    "final": True,
                },
                kind="status-update",
                task_id="task-71",
                context_id="ctx-71",
                status=final_status,
                final=True,
            )

        return _stream()

    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stream_message", _fake_stream_message)

    with _gateway_client(monkeypatch, tmp_path) as client:
        templates = client.get("/agent_api/templates/", headers=_auth_headers(manager_token))
        template = templates.json()[0]
        install_response = client.post(
            f"/agent_api/templates/{template['id']}/install/",
            headers=_auth_headers(manager_token),
            json={"slug": "activity-host", "name": "Activity Host"},
        )
        assert install_response.status_code == 200

        created = client.post(
            "/conversations",
            headers=_auth_headers(manager_token),
            json={"agent_slug": "activity-host", "title": "Activity thread"},
        )
        conversation_id = created.json()["conversation"]["id"]

        with client.websocket_connect(f"/ws/conversations/{conversation_id}?ws_ticket={manager_token}") as websocket:
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "conversation.snapshot"
            websocket.send_json({"type": "message.send", "text": "Please continue"})

            saw_final_status = False
            for _ in range(24):
                envelope = websocket.receive_json()
                if envelope.get("type") == "task.status" and envelope.get("final") is True:
                    saw_final_status = True
                    break

            assert saw_final_status is True

        detail = client.get(f"/conversations/{conversation_id}", headers=_auth_headers(manager_token))
        assert detail.status_code == 200
        conversation = detail.json()["conversation"]
        assert conversation["awaitingInput"] is True
        assert conversation["activeSpecialistSlug"] == "inventory_visibility_agent"
        activities = detail.json()["activities"]
        assert any(item["kind"] == "delegation" for item in activities)
        assert any(item["state"] == "input-required" for item in activities)


def test_conversation_websocket_completed_response_spends_one_coin(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manager_token = _make_token(
        user_id="72",
        profile_id="42",
        owner_id="99",
        permissions=["manage_agent_settings", "interact_with_agent"],
    )
    spend_calls: list[dict[str, object]] = []

    async def _fake_stream_message(self, **_: object):
        async def _stream():
            yield _FakeEvent(
                {
                    "kind": "task",
                    "id": "task-72",
                    "contextId": "ctx-72",
                    "status": {"state": "working"},
                },
                kind="task",
                id="task-72",
                context_id="ctx-72",
                status=SimpleNamespace(state="working"),
            )
            artifact_payload = {
                "name": "result",
                "parts": [
                    {
                        "kind": "data",
                        "data": {
                            "widgets": [{"type": "metric_grid", "title": "Sales Snapshot", "data": []}],
                        },
                    }
                ],
            }
            yield _FakeEvent(
                {
                    "kind": "artifact-update",
                    "taskId": "task-72",
                    "contextId": "ctx-72",
                    "artifact": artifact_payload,
                },
                kind="artifact-update",
                task_id="task-72",
                context_id="ctx-72",
                artifact=SimpleNamespace(model_dump=lambda **__: artifact_payload),
            )
            yield _FakeEvent(
                {
                    "kind": "status-update",
                    "taskId": "task-72",
                    "contextId": "ctx-72",
                    "status": {"state": "completed", "message": {"role": "assistant", "parts": []}},
                    "final": True,
                },
                kind="status-update",
                task_id="task-72",
                context_id="ctx-72",
                status=SimpleNamespace(state="completed", message=SimpleNamespace(model_dump=lambda **__: {"role": "assistant", "parts": []})),
                final=True,
            )

        return _stream()

    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stream_message", _fake_stream_message)
    monkeypatch.setattr(
        "kafka_a2a.mainapps.chat.views.spend_intera_coins_for_a2a_completion",
        lambda **kwargs: spend_calls.append(kwargs) or {"balance": 99},
    )

    with _gateway_client(monkeypatch, tmp_path) as client:
        templates = client.get("/agent_api/templates/", headers=_auth_headers(manager_token))
        template = templates.json()[0]
        install_response = client.post(
            f"/agent_api/templates/{template['id']}/install/",
            headers=_auth_headers(manager_token),
            json={"slug": "billing-host", "name": "Billing Host"},
        )
        assert install_response.status_code == 200

        created = client.post(
            "/conversations",
            headers=_auth_headers(manager_token),
            json={"agent_slug": "billing-host", "title": "Billing thread"},
        )
        assert created.status_code == 200
        conversation_id = created.json()["conversation"]["id"]

        with client.websocket_connect(f"/ws/conversations/{conversation_id}?ws_ticket={manager_token}") as websocket:
            assert websocket.receive_json()["type"] == "conversation.snapshot"
            websocket.send_json({"type": "message.send", "text": "Show sales by location for last month."})
            for _ in range(20):
                envelope = websocket.receive_json()
                if envelope.get("type") == "message.created" and envelope.get("message", {}).get("structuredPayload"):
                    break

    assert spend_calls == [
        {
            "profile_id": "42",
            "task_id": "task-72",
            "conversation_id": conversation_id,
            "prompt_text": "Show sales by location for last month.",
        }
    ]


def test_conversation_websocket_input_required_does_not_spend_until_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manager_token = _make_token(
        user_id="73",
        profile_id="43",
        owner_id="99",
        permissions=["manage_agent_settings", "interact_with_agent"],
    )
    spend_calls: list[dict[str, object]] = []
    stream_calls = {"value": 0}
    continue_calls = {"value": 0}

    async def _fake_stream_message(self, **_: object):
        stream_calls["value"] += 1

        async def _stream():
            final_message = {
                "role": "assistant",
                "messageId": "msg-73-a",
                "parts": [{"kind": "text", "text": "Please confirm before I continue."}],
            }
            yield _FakeEvent(
                {
                    "kind": "task",
                    "id": "task-73",
                    "contextId": "ctx-73",
                    "status": {"state": "working"},
                },
                kind="task",
                id="task-73",
                context_id="ctx-73",
                status=SimpleNamespace(state="working"),
            )
            yield _FakeEvent(
                {
                    "kind": "status-update",
                    "taskId": "task-73",
                    "contextId": "ctx-73",
                    "status": {"state": "input-required", "message": final_message},
                    "final": True,
                },
                kind="status-update",
                task_id="task-73",
                context_id="ctx-73",
                status=SimpleNamespace(state="input-required", message=SimpleNamespace(model_dump=lambda **__: final_message)),
                final=True,
            )

        return _stream()

    async def _fake_continue_task_stream(self, **_: object):
        continue_calls["value"] += 1

        async def _stream():
            artifact_payload = {
                "name": "result",
                "parts": [{"kind": "data", "data": {"widgets": [{"type": "timeline", "events": []}]}}],
            }
            yield _FakeEvent(
                {
                    "kind": "artifact-update",
                    "taskId": "task-73",
                    "contextId": "ctx-73",
                    "artifact": artifact_payload,
                },
                kind="artifact-update",
                task_id="task-73",
                context_id="ctx-73",
                artifact=SimpleNamespace(model_dump=lambda **__: artifact_payload),
            )
            yield _FakeEvent(
                {
                    "kind": "status-update",
                    "taskId": "task-73",
                    "contextId": "ctx-73",
                    "status": {"state": "completed", "message": {"role": "assistant", "parts": []}},
                    "final": True,
                },
                kind="status-update",
                task_id="task-73",
                context_id="ctx-73",
                status=SimpleNamespace(state="completed", message=SimpleNamespace(model_dump=lambda **__: {"role": "assistant", "parts": []})),
                final=True,
            )

        return _stream()

    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stream_message", _fake_stream_message)
    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.continue_task_stream", _fake_continue_task_stream)
    monkeypatch.setattr(
        "kafka_a2a.mainapps.chat.views.spend_intera_coins_for_a2a_completion",
        lambda **kwargs: spend_calls.append(kwargs) or {"balance": 98},
    )

    with _gateway_client(monkeypatch, tmp_path) as client:
        templates = client.get("/agent_api/templates/", headers=_auth_headers(manager_token))
        template = templates.json()[0]
        install_response = client.post(
            f"/agent_api/templates/{template['id']}/install/",
            headers=_auth_headers(manager_token),
            json={"slug": "approval-host", "name": "Approval Host"},
        )
        assert install_response.status_code == 200

        created = client.post(
            "/conversations",
            headers=_auth_headers(manager_token),
            json={"agent_slug": "approval-host", "title": "Approval thread"},
        )
        assert created.status_code == 200
        conversation_id = created.json()["conversation"]["id"]

        with client.websocket_connect(f"/ws/conversations/{conversation_id}?ws_ticket={manager_token}") as websocket:
            assert websocket.receive_json()["type"] == "conversation.snapshot"
            websocket.send_json({"type": "message.send", "text": "Start the PO receiving review for last month."})
            for _ in range(20):
                envelope = websocket.receive_json()
                if envelope.get("type") == "task.status" and envelope.get("final") is True:
                    break
            assert spend_calls == []

            websocket.send_json({"type": "message.send", "text": "{\"approved\":true}"})
            for _ in range(20):
                envelope = websocket.receive_json()
                if envelope.get("type") == "message.created" and envelope.get("message", {}).get("structuredPayload"):
                    break

    assert stream_calls["value"] == 1
    assert continue_calls["value"] == 1
    assert spend_calls == [
        {
            "profile_id": "43",
            "task_id": "task-73",
            "conversation_id": conversation_id,
            "prompt_text": "{\"approved\":true}",
        }
    ]


def test_conversation_websocket_rebuilds_structured_history_for_follow_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manager_token = _make_token(
        user_id="81",
        profile_id="51",
        owner_id="99",
        permissions=["manage_agent_settings", "interact_with_agent"],
    )
    call_count = {"value": 0}

    async def _fake_stream_message(self, *, message=None, metadata=None, **_: object):
        call_count["value"] += 1

        async def _stream():
            if call_count["value"] == 1:
                yield _FakeEvent(
                    {
                        "kind": "task",
                        "id": "task-81-a",
                        "contextId": "ctx-81",
                        "status": {"state": "working"},
                    },
                    kind="task",
                    id="task-81-a",
                    context_id="ctx-81",
                    status=SimpleNamespace(state="working"),
                )
                artifact_payload = {
                    "name": "result",
                    "parts": [
                        {
                            "kind": "data",
                            "data": {
                                "interaction_type": "multiple_choice",
                                "title": "Choose What You Need Help With",
                                "description": "Select the area you want help with. I can continue from your choice.",
                                "options": [{"value": "general", "label": "General Question"}],
                                "multiple": False,
                                "allow_input": True,
                            },
                        }
                    ],
                }
                yield _FakeEvent(
                    {
                        "kind": "artifact-update",
                        "taskId": "task-81-a",
                        "contextId": "ctx-81",
                        "artifact": artifact_payload,
                    },
                    kind="artifact-update",
                    task_id="task-81-a",
                    context_id="ctx-81",
                    artifact=SimpleNamespace(model_dump=lambda **__: artifact_payload),
                )
                yield _FakeEvent(
                    {
                        "kind": "status-update",
                        "taskId": "task-81-a",
                        "contextId": "ctx-81",
                        "status": {"state": "completed", "message": {"role": "assistant", "parts": []}},
                        "final": True,
                    },
                    kind="status-update",
                    task_id="task-81-a",
                    context_id="ctx-81",
                    status=SimpleNamespace(state="completed", message=SimpleNamespace(model_dump=lambda **__: {"role": "assistant", "parts": []})),
                    final=True,
                )
                return

            history = (metadata or {}).get("urn:ka2a:conversation:history") or []
            assert any(
                item.get("role") == "assistant"
                and "Choose What You Need Help With" in str(item.get("content") or "")
                and '"interaction_type": "multiple_choice"' in str(item.get("content") or "")
                for item in history
            )
            assert any(item.get("role") == "user" and item.get("content") == "what can you do for me?" for item in history)
            assert getattr(message, "parts", None)
            yield _FakeEvent(
                {
                    "kind": "task",
                    "id": "task-81-b",
                    "contextId": "ctx-81",
                    "status": {"state": "working"},
                },
                kind="task",
                id="task-81-b",
                context_id="ctx-81",
                status=SimpleNamespace(state="working"),
            )
            final_message = {
                "role": "assistant",
                "messageId": "msg-81",
                "parts": [
                    {
                        "kind": "text",
                        "text": "Tell me what you need help with, and I will answer directly or route it to the right specialist.",
                    }
                ],
            }
            yield _FakeEvent(
                {
                    "kind": "status-update",
                    "taskId": "task-81-b",
                    "contextId": "ctx-81",
                    "status": {"state": "completed", "message": final_message},
                    "final": True,
                },
                kind="status-update",
                task_id="task-81-b",
                context_id="ctx-81",
                status=SimpleNamespace(state="completed", message=SimpleNamespace(model_dump=lambda **__: final_message)),
                final=True,
            )

        return _stream()

    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stream_message", _fake_stream_message)

    with _gateway_client(monkeypatch, tmp_path) as client:
        templates = client.get("/agent_api/templates/", headers=_auth_headers(manager_token))
        template = templates.json()[0]
        install_response = client.post(
            f"/agent_api/templates/{template['id']}/install/",
            headers=_auth_headers(manager_token),
            json={"slug": "history-host", "name": "History Host"},
        )
        assert install_response.status_code == 200

        created = client.post(
            "/conversations",
            headers=_auth_headers(manager_token),
            json={"agent_slug": "history-host", "title": "History thread"},
        )
        assert created.status_code == 200
        conversation_id = created.json()["conversation"]["id"]

        with client.websocket_connect(f"/ws/conversations/{conversation_id}?ws_ticket={manager_token}") as websocket:
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "conversation.snapshot"

            websocket.send_json({"type": "message.send", "text": "what can you do for me?"})
            saw_first_persisted = False
            for _ in range(20):
                envelope = websocket.receive_json()
                if envelope.get("type") == "message.created" and envelope.get("message", {}).get("structuredPayload"):
                    saw_first_persisted = True
                    break
            assert saw_first_persisted is True

            websocket.send_json(
                {
                    "type": "message.send",
                    "text": '{"type":"multiple_choice_response","selected":"general","additional_input":null}',
                }
            )
            saw_final_response = False
            for _ in range(20):
                envelope = websocket.receive_json()
                if envelope.get("type") == "message.created" and envelope.get("message", {}).get("content"):
                    if "Tell me what you need help with" in envelope["message"]["content"]:
                        saw_final_response = True
                        break
            assert saw_final_response is True

        assert call_count["value"] == 2


def test_conversation_plain_text_breaks_out_of_closed_interaction_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manager_token = _make_token(
        user_id="82",
        profile_id="52",
        owner_id="99",
        permissions=["manage_agent_settings", "interact_with_agent"],
    )
    stream_message_calls = {"value": 0}
    continue_calls = {"value": 0}

    async def _fake_stream_message(self, *, message=None, **_: object):
        stream_message_calls["value"] += 1

        async def _stream():
            if stream_message_calls["value"] == 1:
                yield _FakeEvent(
                    {
                        "kind": "task",
                        "id": "task-82-a",
                        "contextId": "ctx-82",
                        "status": {"state": "working"},
                    },
                    kind="task",
                    id="task-82-a",
                    context_id="ctx-82",
                    status=SimpleNamespace(state="working"),
                )
                artifact_payload = {
                    "name": "result",
                    "parts": [
                        {
                            "kind": "data",
                            "data": {
                                "interaction_type": "multiple_choice",
                                "title": "Continue Workflow",
                                "description": "Choose how you want to continue.",
                                "options": [{"value": "continue_next", "label": "Continue"}],
                                "multiple": False,
                                "allow_input": False,
                            },
                        }
                    ],
                }
                yield _FakeEvent(
                    {
                        "kind": "artifact-update",
                        "taskId": "task-82-a",
                        "contextId": "ctx-82",
                        "artifact": artifact_payload,
                    },
                    kind="artifact-update",
                    task_id="task-82-a",
                    context_id="ctx-82",
                    artifact=SimpleNamespace(model_dump=lambda **__: artifact_payload),
                )
                final_message = {
                    "role": "assistant",
                    "parts": artifact_payload["parts"],
                }
                yield _FakeEvent(
                    {
                        "kind": "status-update",
                        "taskId": "task-82-a",
                        "contextId": "ctx-82",
                        "status": {"state": "input-required", "message": final_message},
                        "final": True,
                    },
                    kind="status-update",
                    task_id="task-82-a",
                    context_id="ctx-82",
                    status=SimpleNamespace(state="input-required", message=SimpleNamespace(model_dump=lambda **__: final_message)),
                    final=True,
                )
                return

            yield _FakeEvent(
                {
                    "kind": "task",
                    "id": "task-82-b",
                    "contextId": "ctx-82-b",
                    "status": {"state": "working"},
                },
                kind="task",
                id="task-82-b",
                context_id="ctx-82-b",
                status=SimpleNamespace(state="working"),
            )
            final_message = {
                "role": "assistant",
                "messageId": "msg-82",
                "parts": [
                    {
                        "kind": "text",
                        "text": "Started a fresh request instead of resuming the stale workflow prompt.",
                    }
                ],
            }
            yield _FakeEvent(
                {
                    "kind": "status-update",
                    "taskId": "task-82-b",
                    "contextId": "ctx-82-b",
                    "status": {"state": "completed", "message": final_message},
                    "final": True,
                },
                kind="status-update",
                task_id="task-82-b",
                context_id="ctx-82-b",
                status=SimpleNamespace(state="completed", message=SimpleNamespace(model_dump=lambda **__: final_message)),
                final=True,
            )

        return _stream()

    async def _fake_continue_task_stream(self, **_: object):
        continue_calls["value"] += 1
        raise AssertionError("Plain-text follow-up should not resume a closed-choice interaction.")

    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stream_message", _fake_stream_message)
    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.continue_task_stream", _fake_continue_task_stream)

    with _gateway_client(monkeypatch, tmp_path) as client:
        templates = client.get("/agent_api/templates/", headers=_auth_headers(manager_token))
        template = templates.json()[0]
        install_response = client.post(
            f"/agent_api/templates/{template['id']}/install/",
            headers=_auth_headers(manager_token),
            json={"slug": "closed-prompt-host", "name": "Closed Prompt Host"},
        )
        assert install_response.status_code == 200

        created = client.post(
            "/conversations",
            headers=_auth_headers(manager_token),
            json={"agent_slug": "closed-prompt-host", "title": "Closed prompt thread"},
        )
        assert created.status_code == 200
        conversation_id = created.json()["conversation"]["id"]

        with client.websocket_connect(f"/ws/conversations/{conversation_id}?ws_ticket={manager_token}") as websocket:
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "conversation.snapshot"

            websocket.send_json({"type": "message.send", "text": "continue"})
            saw_prompt = False
            for _ in range(20):
                envelope = websocket.receive_json()
                if envelope.get("type") == "message.created" and envelope.get("message", {}).get("structuredPayload"):
                    saw_prompt = True
                    break
            assert saw_prompt is True

            websocket.send_json({"type": "message.send", "text": "search for Adidas shoes online"})
            saw_fresh_response = False
            for _ in range(20):
                envelope = websocket.receive_json()
                if envelope.get("type") == "message.created":
                    content = envelope.get("message", {}).get("content") or ""
                    if "Started a fresh request" in content:
                        saw_fresh_response = True
                        break
            assert saw_fresh_response is True

        assert stream_message_calls["value"] == 2
        assert continue_calls["value"] == 0


def test_gateway_stream_returns_handled_transport_error_with_cors_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    token = _make_token(user_id="17", profile_id="1", owner_id="17")
    monkeypatch.setattr(
        "kafka_a2a.mainapps.agents.services.AgentControlPlaneService.runtime_agent_config",
        lambda self, *, access, slug: {"runtime_name": "host"},
    )

    async def _fake_stream_message(self, **_: object):
        raise RuntimeError("KafkaConnectionError: Connection at kafka.interaims.com:443 closed")

    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stream_message", _fake_stream_message)

    with _gateway_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/stream",
            headers={"Origin": "http://localhost:3000", **_auth_headers(token)},
            json={"text": "hello", "agentName": "host", "historyLength": 6},
        )

    assert response.status_code == 503
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.json() == {
        "detail": "A2A gateway is temporarily unavailable while the agent transport reconnects."
    }


def test_gateway_stream_emits_final_failed_status_update_when_iterator_crashes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    token = _make_token(user_id="17", profile_id="1", owner_id="17")
    monkeypatch.setattr(
        "kafka_a2a.mainapps.agents.services.AgentControlPlaneService.runtime_agent_config",
        lambda self, *, access, slug: {"runtime_name": "host"},
    )

    async def _fake_stream_message(self, **_: object):
        async def _stream():
            yield _FakeEvent(
                {
                    "kind": "task",
                    "id": "task-stream-failure",
                    "contextId": "ctx-stream-failure",
                    "status": {"state": "working"},
                },
                kind="task",
                id="task-stream-failure",
                context_id="ctx-stream-failure",
                status=SimpleNamespace(state="working"),
            )
            raise RuntimeError("KafkaConnectionError: Connection at kafka.interaims.com:443 closed")

        return _stream()

    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stream_message", _fake_stream_message)

    with _gateway_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/stream",
            headers={"Origin": "http://localhost:3000", **_auth_headers(token)},
            json={"text": "hello", "agentName": "host", "historyLength": 6},
        )

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.startswith("data:")]
    assert len(lines) == 2
    assert '"kind":"task"' in lines[0]
    assert '"kind":"status-update"' in lines[1]
    assert '"state":"failed"' in lines[1]
    assert '"taskId":"task-stream-failure"' in lines[1]
    assert '"contextId":"ctx-stream-failure"' in lines[1]
    assert "A2A gateway is temporarily unavailable while the agent transport reconnects." in lines[1]


def test_gateway_continue_stream_returns_handled_transport_error_with_cors_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    token = _make_token(user_id="17", profile_id="1", owner_id="17")
    monkeypatch.setattr(
        "kafka_a2a.mainapps.agents.services.AgentControlPlaneService.runtime_agent_config",
        lambda self, *, access, slug: {"runtime_name": "host"},
    )

    async def _fake_continue_task_stream(self, **_: object):
        raise RuntimeError("KafkaConnectionError: Connection at kafka.interaims.com:443 closed")

    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.continue_task_stream", _fake_continue_task_stream)

    with _gateway_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/tasks/task-123/continue/stream",
            headers={"Origin": "http://localhost:3000", **_auth_headers(token)},
            json={"text": "continue", "historyLength": 6},
        )

    assert response.status_code == 503
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.json() == {
        "detail": "A2A gateway is temporarily unavailable while the agent transport reconnects."
    }


def test_gateway_stream_maps_a2a_permission_denied_to_http_403(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    token = _make_token(user_id="17", profile_id="1", owner_id="17")
    monkeypatch.setattr(
        "kafka_a2a.mainapps.agents.services.AgentControlPlaneService.runtime_agent_config",
        lambda self, *, access, slug: {"runtime_name": "host"},
    )

    async def _fake_stream_message(self, **_: object):
        raise A2AError(code=A2AErrorCode.PERMISSION_DENIED, message="Permission denied")

    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stream_message", _fake_stream_message)

    with _gateway_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/stream",
            headers={"Origin": "http://localhost:3000", **_auth_headers(token)},
            json={"text": "hello", "agentName": "host", "historyLength": 6},
        )

    assert response.status_code == 403
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.json() == {"detail": "Permission denied"}


def test_gateway_stream_requires_authentication(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    async def _fake_stream_message(self, **_: object):
        raise AssertionError("stream_message should not be called without authentication")

    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stream_message", _fake_stream_message)

    with _gateway_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/stream",
            headers={"Origin": "http://localhost:3000"},
            json={"text": "hello", "agentName": "host", "historyLength": 6},
        )

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.json() == {"detail": "Authentication required"}


def test_gateway_stream_falls_back_to_registry_runtime_name_when_control_plane_lookup_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    token = _make_token(user_id="17", profile_id="1", owner_id="17")
    captured: dict[str, object] = {}

    def _raise_runtime_lookup(self, *, access, slug):
        raise RuntimeError("control plane unavailable")

    monkeypatch.setattr(
        "kafka_a2a.mainapps.agents.services.AgentControlPlaneService.runtime_agent_config",
        _raise_runtime_lookup,
    )
    monkeypatch.setattr(
        "kafka_a2a.registry.directory.KafkaAgentDirectory.list",
        lambda self: [
            AgentCard.model_validate(
                {
                    "name": "wa-p1-host-registry",
                    "description": "Registry host",
                    "url": "kafka://wa-p1-host-registry",
                    "version": "0.1.0",
                    "capabilities": {"streaming": True},
                    "metadata": {
                        "ka2aRuntime": {
                            "publicSlug": "host",
                            "profileId": "1",
                        }
                    },
                }
            )
        ],
    )

    async def _fake_stream_message(self, **kwargs: object):
        captured.update(kwargs)

        async def _stream():
            yield _FakeEvent(
                {
                    "kind": "status-update",
                    "status": {"state": "completed"},
                },
                kind="status-update",
                status=SimpleNamespace(state="completed"),
            )

        return _stream()

    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stream_message", _fake_stream_message)

    with _gateway_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/stream",
            headers={"Origin": "http://localhost:3000", **_auth_headers(token)},
            json={"text": "hello", "agentName": "host", "historyLength": 6},
        )

    assert response.status_code == 200
    assert captured["agent_name"] == "wa-p1-host-registry"


def test_gateway_stream_keeps_verified_jwt_claims_when_principal_overrides_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    token = _make_token(user_id="17", profile_id="1", owner_id="17")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "kafka_a2a.mainapps.agents.services.AgentControlPlaneService.runtime_agent_config",
        lambda self, *, access, slug: {"runtime_name": "wa-p1-host"},
    )
    monkeypatch.setattr(
        "kafka_a2a.mainapps.agents.services.AgentControlPlaneService.build_principal_claim_overrides",
        lambda self, *, profile_id: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )

    async def _fake_stream_message(self, **kwargs: object):
        captured.update(kwargs)

        async def _stream():
            yield _FakeEvent(
                {
                    "kind": "status-update",
                    "status": {"state": "completed"},
                },
                kind="status-update",
                status=SimpleNamespace(state="completed"),
            )

        return _stream()

    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stream_message", _fake_stream_message)

    with _gateway_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/stream",
            headers={"Origin": "http://localhost:3000", **_auth_headers(token)},
            json={"text": "hello", "agentName": "host", "historyLength": 6},
        )

    assert response.status_code == 200
    metadata = captured["metadata"]
    principal = metadata["urn:ka2a:principal"]
    assert principal["userId"] == "17"
    assert principal["claims"]["profile_id"] == "1"


def test_gateway_stream_uses_stream_specific_timeout_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    token = _make_token(user_id="17", profile_id="1", owner_id="17")
    monkeypatch.setenv("KA2A_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KA2A_CONTROL_PLANE_STORE_PATH", str(tmp_path / "control-plane.json"))
    monkeypatch.setenv("KA2A_CHAT_STORE", "memory")
    monkeypatch.setenv("KA2A_RUNTIME_SHARED_TOKEN", RUNTIME_SYNC_TOKEN)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("KA2A_DATABASE_URL", raising=False)
    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.start", _noop_async)
    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stop", _noop_async)
    monkeypatch.setattr("kafka_a2a.registry.directory.KafkaAgentDirectory.start", _noop_async)
    monkeypatch.setattr("kafka_a2a.registry.directory.KafkaAgentDirectory.stop", _noop_async)
    monkeypatch.setattr(
        "kafka_a2a.mainapps.agents.services.AgentControlPlaneService.runtime_agent_config",
        lambda self, *, access, slug: {"runtime_name": "host"},
    )

    captured: dict[str, object] = {}

    async def _fake_stream_message(self, **kwargs: object):
        captured.update(kwargs)

        async def _stream():
            yield _FakeEvent(
                {
                    "kind": "status-update",
                    "status": {"state": "completed"},
                },
                kind="status-update",
                status=SimpleNamespace(state="completed"),
            )

        return _stream()

    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stream_message", _fake_stream_message)

    app = create_gateway_app(
        GatewayConfig(
            bootstrap_servers="localhost:9092",
            default_agent="host",
            stream_request_timeout_s=123.0,
            jwt=JwtBearerConfig(
                secret=JWT_SECRET,
                algorithms=["HS256"],
                user_claim="sub",
                include_claims=True,
            ),
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/stream",
            headers={"Origin": "http://localhost:3000", **_auth_headers(token)},
            json={"text": "hello", "agentName": "host", "historyLength": 6},
        )

    assert response.status_code == 200
    assert captured["timeout_s"] == 123.0


def test_gateway_stream_completed_response_spends_one_coin(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    token = _make_token(user_id="18", profile_id="2", owner_id="18")
    spend_calls: list[dict[str, object]] = []
    monkeypatch.setenv("KA2A_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KA2A_CONTROL_PLANE_STORE_PATH", str(tmp_path / "control-plane.json"))
    monkeypatch.setenv("KA2A_CHAT_STORE", "memory")
    monkeypatch.setenv("KA2A_RUNTIME_SHARED_TOKEN", RUNTIME_SYNC_TOKEN)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("KA2A_DATABASE_URL", raising=False)
    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.start", _noop_async)
    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stop", _noop_async)
    monkeypatch.setattr("kafka_a2a.registry.directory.KafkaAgentDirectory.start", _noop_async)
    monkeypatch.setattr("kafka_a2a.registry.directory.KafkaAgentDirectory.stop", _noop_async)
    monkeypatch.setattr(
        "kafka_a2a.mainapps.agents.services.AgentControlPlaneService.runtime_agent_config",
        lambda self, *, access, slug: {"runtime_name": "host"},
    )
    monkeypatch.setattr(
        "kafka_a2a.server.gateway.spend_intera_coins_for_a2a_completion",
        lambda **kwargs: spend_calls.append(kwargs) or {"balance": 97},
    )

    async def _fake_stream_message(self, **_: object):
        async def _stream():
            yield _FakeEvent(
                {
                    "kind": "task",
                    "id": "task-18",
                    "contextId": "ctx-18",
                    "status": {"state": "working"},
                },
                kind="task",
                id="task-18",
                context_id="ctx-18",
                status=SimpleNamespace(state="working"),
            )
            yield _FakeEvent(
                {
                    "kind": "artifact-update",
                    "taskId": "task-18",
                    "contextId": "ctx-18",
                    "artifact": {
                        "name": "result",
                        "parts": [{"kind": "data", "data": {"widgets": [{"type": "bar_chart", "data": []}]}}],
                    },
                },
                kind="artifact-update",
                task_id="task-18",
                context_id="ctx-18",
                artifact=SimpleNamespace(
                    model_dump=lambda **__: {
                        "name": "result",
                        "parts": [{"kind": "data", "data": {"widgets": [{"type": "bar_chart", "data": []}]}}],
                    }
                ),
            )
            yield _FakeEvent(
                {
                    "kind": "status-update",
                    "taskId": "task-18",
                    "contextId": "ctx-18",
                    "status": {"state": "completed", "message": {"role": "assistant", "parts": []}},
                    "final": True,
                },
                kind="status-update",
                task_id="task-18",
                context_id="ctx-18",
                status=SimpleNamespace(state="completed", message=SimpleNamespace(model_dump=lambda **__: {"role": "assistant", "parts": []})),
                final=True,
            )

        return _stream()

    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stream_message", _fake_stream_message)

    app = create_gateway_app(
        GatewayConfig(
            bootstrap_servers="localhost:9092",
            default_agent="host",
            jwt=JwtBearerConfig(
                secret=JWT_SECRET,
                algorithms=["HS256"],
                user_claim="sub",
                include_claims=True,
            ),
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/stream",
            headers=_auth_headers(token),
            json={"text": "Show top sellers for last month.", "agentName": "host", "historyLength": 6},
        )

    assert response.status_code == 200
    assert spend_calls == [
        {
            "profile_id": "2",
            "task_id": "task-18",
            "conversation_id": None,
            "prompt_text": "Show top sellers for last month.",
        }
    ]


def test_gateway_stream_failed_response_does_not_spend_coin(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    token = _make_token(user_id="19", profile_id="3", owner_id="19")
    spend_calls: list[dict[str, object]] = []
    monkeypatch.setenv("KA2A_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KA2A_CONTROL_PLANE_STORE_PATH", str(tmp_path / "control-plane.json"))
    monkeypatch.setenv("KA2A_CHAT_STORE", "memory")
    monkeypatch.setenv("KA2A_RUNTIME_SHARED_TOKEN", RUNTIME_SYNC_TOKEN)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("KA2A_DATABASE_URL", raising=False)
    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.start", _noop_async)
    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stop", _noop_async)
    monkeypatch.setattr("kafka_a2a.registry.directory.KafkaAgentDirectory.start", _noop_async)
    monkeypatch.setattr("kafka_a2a.registry.directory.KafkaAgentDirectory.stop", _noop_async)
    monkeypatch.setattr(
        "kafka_a2a.mainapps.agents.services.AgentControlPlaneService.runtime_agent_config",
        lambda self, *, access, slug: {"runtime_name": "host"},
    )
    monkeypatch.setattr(
        "kafka_a2a.server.gateway.spend_intera_coins_for_a2a_completion",
        lambda **kwargs: spend_calls.append(kwargs) or {"balance": 96},
    )

    async def _fake_stream_message(self, **_: object):
        async def _stream():
            yield _FakeEvent(
                {
                    "kind": "task",
                    "id": "task-19",
                    "contextId": "ctx-19",
                    "status": {"state": "working"},
                },
                kind="task",
                id="task-19",
                context_id="ctx-19",
                status=SimpleNamespace(state="working"),
            )
            yield _FakeEvent(
                {
                    "kind": "status-update",
                    "taskId": "task-19",
                    "contextId": "ctx-19",
                    "status": {
                        "state": "failed",
                        "message": {"role": "assistant", "parts": [{"kind": "text", "text": "Unable to complete request."}]},
                    },
                    "final": True,
                },
                kind="status-update",
                task_id="task-19",
                context_id="ctx-19",
                status=SimpleNamespace(
                    state="failed",
                    message=SimpleNamespace(model_dump=lambda **__: {"role": "assistant", "parts": [{"kind": "text", "text": "Unable to complete request."}]}),
                ),
                final=True,
            )

        return _stream()

    monkeypatch.setattr("kafka_a2a.client.Ka2aClient.stream_message", _fake_stream_message)

    app = create_gateway_app(
        GatewayConfig(
            bootstrap_servers="localhost:9092",
            default_agent="host",
            jwt=JwtBearerConfig(
                secret=JWT_SECRET,
                algorithms=["HS256"],
                user_claim="sub",
                include_claims=True,
            ),
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/stream",
            headers=_auth_headers(token),
            json={"text": "Show stock value changes for last month.", "agentName": "host", "historyLength": 6},
        )

    assert response.status_code == 200
    assert spend_calls == []
