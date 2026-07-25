from __future__ import annotations

from types import SimpleNamespace

import pytest

import kafka_a2a.livekit_voice.worker as voice_worker
from kafka_a2a.livekit_voice.worker import (
    VoiceRuntimeContext,
    _artifact_speakable_text,
    _collapse_repeated_phrase,
    _message_text,
    _room_participant_identities,
    _resolve_runtime_agent_name,
    _resolve_voice_host_name_from_setup,
    _sanitize_voice_text,
    _select_voice_response_candidate,
    _should_delegate_voice_transcript,
    _stream_payload_from_event,
    _transcript_comparison_key,
    _voice_direct_reply,
    _voice_backend_delegation_enabled,
    _voice_repeat_requested,
    _wait_for_voice_participant,
    _voice_request_metadata,
    _workspace_ai_setup_payload_from_rows,
    _get_workspace_ai_setup_cached,
)
from kafka_a2a.models import Artifact, DataPart, Message, Role, Task, TaskStatus, TaskState, TextPart
from kafka_a2a.tenancy import KA2A_PRINCIPAL_METADATA_KEY, Principal


def test_livekit_message_text_extracts_content_list() -> None:
    message = SimpleNamespace(
        content=["Can you help me check my best-performing products?"],
    )

    assert _message_text(message) == "Can you help me check my best-performing products?"


def test_livekit_message_text_does_not_stringify_unknown_models() -> None:
    class UnknownModel:
        def __str__(self) -> str:
            return "state=<TaskState.submitted: 'submitted'> bearerToken='secret-token'"

    assert _message_text(UnknownModel()) == ""


def test_resolve_runtime_agent_name_matches_host_slug_for_profile() -> None:
    registry = {
        "agents": [
            {
                "profile": "1",
                "name": "Host",
                "slug": "host",
                "source_template_slug": "host",
                "runtime_name": "wa-p1-host-wrong",
            },
            {
                "profile": "4",
                "name": "Host",
                "slug": "host",
                "source_template_slug": "host",
                "runtime_name": "wa-p4-host-4073ba349bba",
            },
        ]
    }

    assert (
        _resolve_runtime_agent_name(registry=registry, profile_id="4", requested_name="host")
        == "wa-p4-host-4073ba349bba"
    )


def test_resolve_runtime_agent_name_is_case_insensitive() -> None:
    registry = {
        "agents": [
            {
                "profile_id": "4",
                "name": "Host",
                "runtime_name": "wa-p4-host-case",
            }
        ]
    }

    assert _resolve_runtime_agent_name(registry=registry, profile_id="4", requested_name="host") == "wa-p4-host-case"


def test_resolve_voice_host_name_uses_lightweight_ai_setup_runtime_name() -> None:
    assert (
        _resolve_voice_host_name_from_setup(
            ai_setup={"agent": {"host_runtime_name": "wa-p4-host-4073ba349bba"}},
            requested_name="host",
        )
        == "wa-p4-host-4073ba349bba"
    )


def test_workspace_ai_setup_payload_from_rows_builds_host_runtime_name() -> None:
    payload = _workspace_ai_setup_payload_from_rows(
        ai={
            "id": "settings-1",
            "profile": "4",
            "name": "Debug AI",
            "version": "gpt-5-mini",
            "base_url": "",
            "api_key": "test-api-key",
            "tavily_api_key": "test-tavily-key",
            "special_instruction": "Speak clearly.",
        },
        version={
            "id": "gpt-5-mini",
            "provider": "chatgpt",
            "provider_label": "ChatGPT",
            "model_name": "gpt-5-mini",
            "base_url": "https://api.openai.com",
        },
        host_agent={
            "id": "4073ba34-9bba-4a60-96fa-0589e2a6202e",
            "profile": "4",
            "slug": "host",
        },
    )

    assert payload["configured"] is True
    assert payload["agent"]["has_api_key"] is True
    assert payload["agent"]["model_name"] == "gpt-5-mini"
    assert payload["agent"]["host_runtime_name"] == "wa-p4-host-4073ba349bba"


def test_resolve_voice_host_name_ignores_non_host_request() -> None:
    assert (
        _resolve_voice_host_name_from_setup(
            ai_setup={"agent": {"host_runtime_name": "wa-p4-host-4073ba349bba"}},
            requested_name="inventory",
        )
        is None
    )


def test_voice_request_metadata_forwards_gateway_principal_shape() -> None:
    runtime = VoiceRuntimeContext(
        profile_id="4",
        access_token="token-value",
        user_email="owner@example.com",
        workspace_name="Debug Workspace",
        participant_name="Owner",
        host_agent_name="wa-p4-host-4073ba349bba",
        principal=Principal(
            user_id="1",
            tenant_id="4",
            bearer_token="token-value",
            claims={"profile_id": "4", "permissions": ["oral_conversation_with_ai"]},
        ),
        metadata={},
    )

    metadata = _voice_request_metadata(runtime)

    assert metadata["profileId"] == "4"
    assert metadata[KA2A_PRINCIPAL_METADATA_KEY]["userId"] == "1"
    assert metadata[KA2A_PRINCIPAL_METADATA_KEY]["tenantId"] == "4"
    assert metadata[KA2A_PRINCIPAL_METADATA_KEY]["bearerToken"] == "token-value"


def test_voice_router_answers_greeting_without_delegation() -> None:
    assert _voice_direct_reply("hello are you there")
    assert not _should_delegate_voice_transcript("hello are you there")


def test_voice_router_delegates_inventory_question() -> None:
    assert _should_delegate_voice_transcript("Can you help me check my best-performing products?")


def test_voice_backend_delegation_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KA2A_VOICE_BACKEND_DELEGATION_ENABLED", raising=False)
    assert not _voice_backend_delegation_enabled()

    monkeypatch.setenv("KA2A_VOICE_BACKEND_DELEGATION_ENABLED", "true")
    assert _voice_backend_delegation_enabled()


def test_voice_router_ignores_background_non_business_audio() -> None:
    transcript = "These pink pelicans can hold fish in their pouches and they are beautiful birds."

    assert _voice_direct_reply(transcript) is None
    assert not _should_delegate_voice_transcript(transcript)


def test_voice_speech_ignores_submitted_user_task_payload() -> None:
    task = Task(
        id="task-1",
        context_id="context-1",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text="Can you help me check my best-performing products?")],
                metadata={
                    "profileId": "4",
                    "urn:ka2a:principal": {
                        "userId": "7",
                        "tenantId": "4",
                        "bearerToken": "secret-token",
                    },
                },
            ),
        ),
    )

    assert _artifact_speakable_text(task) == ""


def test_voice_speech_reads_structured_summary_not_widget_payload() -> None:
    message = Message(
        role=Role.agent,
        parts=[
            DataPart(
                data={
                    "summary": "Top products are ready for last year.",
                    "widgets": [
                        {
                            "type": "ranked_list",
                            "rows": [
                                {"row_type": "ranked_item", "label": "Eva Premium Water", "barcode": "8800000001101"}
                            ],
                        }
                    ],
                    "insights": ["Eva Premium Water led the period."],
                }
            )
        ],
    )
    task = Task(
        id="task-2",
        context_id="context-1",
        status=TaskStatus(state=TaskState.completed, message=message),
    )

    speakable = _artifact_speakable_text(task)

    assert "Top products are ready for last year." in speakable
    assert "Eva Premium Water led the period." in speakable
    assert "ranked_item" not in speakable
    assert "8800000001101" not in speakable


def test_voice_speech_prefers_artifact_over_generic_terminal_status() -> None:
    task = Task(
        id="task-3",
        context_id="context-1",
        status=TaskStatus(
            state=TaskState.completed,
            message=Message(role=Role.agent, parts=[TextPart(text="How can I help you today?")]),
        ),
        artifacts=[
            Artifact(
                parts=[
                    DataPart(
                        data={
                            "summary": "There are 17 active inventory products in this workspace.",
                        }
                    )
                ]
            )
        ],
    )

    assert _artifact_speakable_text(task) == "There are 17 active inventory products in this workspace."


def test_voice_response_candidate_rejects_generic_and_uses_later_answer() -> None:
    assert (
        _select_voice_response_candidate(
            [
                "I’m checking that with the workspace agent now.",
                "How can I help you today?",
                "There are 17 active inventory products in this workspace.",
            ]
        )
        == "There are 17 active inventory products in this workspace."
    )


def test_voice_sanitizer_maps_provider_key_error_to_safe_message() -> None:
    raw_error = (
        'OpenAI-compatible upstream error (401): {"error":{"message":"Incorrect API key provided: '
        'sk-proj-secret","type":"invalid_request_error","code":"invalid_api_key"}}'
    )

    safe_text = _sanitize_voice_text(raw_error, reject_generic=True)

    assert "workspace AI provider key is invalid" in safe_text
    assert "sk-proj" not in safe_text
    assert "invalid_api_key" not in safe_text


def test_voice_sanitizer_drops_submitted_task_payload_metadata() -> None:
    unsafe_text = "state=<TaskState.submitted: 'submitted'> metadata={'bearerToken': 'secret-token'}"

    assert _sanitize_voice_text(unsafe_text, reject_generic=True) == ""


def test_voice_sanitizer_drops_empty_json_response() -> None:
    assert _sanitize_voice_text("{}", reject_generic=True) == ""
    assert _select_voice_response_candidate(["{}", "[]"]) == ""


def test_voice_stream_payload_redacts_sensitive_metadata() -> None:
    payload = _stream_payload_from_event(
        {
            "kind": "status-update",
            "taskId": "task-1",
            "contextId": "context-1",
            "status": {
                "state": "completed",
                "message": {
                    "kind": "message",
                    "role": "agent",
                    "parts": [{"kind": "text", "text": "Analysis is ready."}],
                    "metadata": {
                        "profileId": "4",
                        "bearerToken": "secret-token",
                        "accessToken": "secret-access-token",
                        "urn:ka2a:principal": {"bearerToken": "nested-secret"},
                    },
                },
            },
        }
    )

    message_metadata = payload["status"]["message"]["metadata"]
    assert message_metadata["profileId"] == "4"
    assert "bearerToken" not in message_metadata
    assert "accessToken" not in message_metadata
    assert "urn:ka2a:principal" not in message_metadata


def test_voice_transcript_collapse_removes_repeated_final_phrase() -> None:
    phrase = "Can you help me check my best-performing products?"

    assert _collapse_repeated_phrase(f"{phrase} {phrase} {phrase}") == phrase


def test_voice_transcript_collapse_handles_quote_variants() -> None:
    repeated = "Voice session connected. I’m listening now. Voice session connected. I'm listening now."

    assert _collapse_repeated_phrase(repeated) == "Voice session connected. I’m listening now."


def test_voice_transcript_comparison_key_normalizes_punctuation() -> None:
    assert _transcript_comparison_key("I’m checking that now.") == _transcript_comparison_key("I'm checking that now")


def test_voice_repeat_request_is_handled_locally() -> None:
    assert _voice_repeat_requested("Please say that again, I did not hear you.")


def test_room_participant_identities_reads_livekit_remote_participants() -> None:
    room = SimpleNamespace(
        remote_participants={
            "sid-1": SimpleNamespace(identity="owner@example.com"),
            "sid-2": SimpleNamespace(identity=""),
        },
    )

    assert _room_participant_identities(room) == {"owner@example.com"}


@pytest.mark.asyncio
async def test_wait_for_voice_participant_returns_false_when_caller_left(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KA2A_VOICE_PARTICIPANT_WAIT_S", "0")
    ctx = SimpleNamespace(room=SimpleNamespace(remote_participants={}))

    assert not await _wait_for_voice_participant(ctx, "owner@example.com")


@pytest.mark.asyncio
async def test_wait_for_voice_participant_accepts_expected_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KA2A_VOICE_PARTICIPANT_WAIT_S", "0")
    ctx = SimpleNamespace(
        room=SimpleNamespace(remote_participants={"sid-1": SimpleNamespace(identity="owner@example.com")})
    )

    assert await _wait_for_voice_participant(ctx, "owner@example.com")


@pytest.mark.asyncio
async def test_voice_ai_setup_prefers_control_plane_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KA2A_VOICE_AI_SETUP_SOURCE", "control_plane")
    monkeypatch.setenv("KA2A_VOICE_AI_SETUP_CACHE_TTL_S", "0")
    direct_db_calls = 0

    def direct_db_lookup(*, profile_id: str) -> dict[str, object] | None:
        nonlocal direct_db_calls
        direct_db_calls += 1
        return {"configured": False, "agent": None, "available_versions": []}

    class ControlPlane:
        def get_internal_workspace_ai_setup(self, *, profile_id: str) -> dict[str, object]:
            return {"configured": True, "agent": {"profile": profile_id, "api_key": "key"}, "available_versions": []}

    monkeypatch.setattr(voice_worker, "_get_workspace_ai_setup_from_database", direct_db_lookup)

    setup = await _get_workspace_ai_setup_cached(ControlPlane(), profile_id="4")  # type: ignore[arg-type]

    assert setup["configured"] is True
    assert direct_db_calls == 0


@pytest.mark.asyncio
async def test_voice_ai_setup_defaults_to_database_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KA2A_VOICE_AI_SETUP_SOURCE", raising=False)
    monkeypatch.setenv("KA2A_VOICE_AI_SETUP_CACHE_TTL_S", "0")
    control_plane_calls = 0

    def direct_db_lookup(*, profile_id: str) -> dict[str, object] | None:
        return {"configured": True, "agent": {"profile": profile_id, "api_key": "key"}, "available_versions": []}

    class ControlPlane:
        def get_internal_workspace_ai_setup(self, *, profile_id: str) -> dict[str, object]:
            nonlocal control_plane_calls
            control_plane_calls += 1
            return {"configured": False, "agent": None, "available_versions": []}

    monkeypatch.setattr(voice_worker, "_get_workspace_ai_setup_from_database", direct_db_lookup)

    setup = await _get_workspace_ai_setup_cached(ControlPlane(), profile_id="4")  # type: ignore[arg-type]

    assert setup["configured"] is True
    assert control_plane_calls == 0
