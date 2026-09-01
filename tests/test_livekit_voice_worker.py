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
    _select_voice_transcript_batch,
    _should_delegate_voice_transcript,
    _voice_has_actionable_business_request,
    _voice_merge_clarification_answer,
    _voice_progress_clarification,
    _voice_clarification_requirement,
    _stream_payload_from_event,
    _voice_supersedes_pending_clarification,
    _transcript_comparison_key,
    _voice_direct_reply,
    _voice_backend_delegation_enabled,
    _voice_corrected_inventory_request,
    _voice_is_cancellation_request,
    _voice_session_greeting,
    _voice_repeat_requested,
    _voice_should_defer_fragment_clarification,
    _voice_tts_text,
    _wait_for_voice_participant,
    _voice_request_metadata,
    _voice_host_message,
    _workspace_ai_setup_payload_from_rows,
    _get_workspace_ai_setup_cached,
    _humanize_voice_progress_text,
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


def test_voice_host_message_preserves_context_for_follow_up_turns() -> None:
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

    first = _voice_host_message(
        runtime=runtime,
        transcript="Analyze my sales data for the past three months.",
        context_id="voice-call-1",
    )
    follow_up = _voice_host_message(
        runtime=runtime,
        transcript="Group it by location.",
        context_id=first.context_id or "",
    )

    assert first.context_id == "voice-call-1"
    assert follow_up.context_id == first.context_id
    assert follow_up.parts[0].text == "Group it by location."


def test_voice_router_answers_greeting_without_delegation() -> None:
    assert _voice_direct_reply("hello are you there")
    assert not _should_delegate_voice_transcript("hello are you there")


def test_voice_chat_speech_command_accepts_only_the_active_caller() -> None:
    packet = SimpleNamespace(
        topic="ka2a.voice.control",
        participant=SimpleNamespace(identity="owner@example.com"),
        data=b'{"source":"intera_a2a_chat","type":"speak_chat_update","text":"Your report is ready."}',
    )

    assert voice_worker._voice_chat_speech_command_text(
        packet,
        expected_participant_identity="owner@example.com",
    ) == "Your report is ready."
    assert voice_worker._voice_chat_speech_command_text(
        packet,
        expected_participant_identity="another-user@example.com",
    ) == ""


def test_voice_chat_speech_command_accepts_packet_with_matching_participant_name_only() -> None:
    packet = SimpleNamespace(
        topic="ka2a.voice.control",
        participant=SimpleNamespace(name="owner@example.com"),
        data=b'{"source":"intera_a2a_chat","type":"speak_chat_update","text":"Quick status update."}',
    )

    assert voice_worker._voice_chat_speech_command_text(
        packet,
        expected_participant_identity="owner@example.com",
    ) == "Quick status update."


def test_voice_chat_speech_command_rejects_non_control_messages() -> None:
    packet = SimpleNamespace(
        topic="ka2a.voice",
        participant=SimpleNamespace(identity="owner@example.com"),
        data=b'{"source":"ka2a_voice","type":"result","text":"Ignore me."}',
    )

    assert voice_worker._voice_chat_speech_command_text(
        packet,
        expected_participant_identity="owner@example.com",
    ) == ""


def test_voice_session_greeting_is_short_and_user_facing() -> None:
    greeting = _voice_session_greeting()

    assert greeting in voice_worker._VOICE_SESSION_GREETINGS
    assert "connected" not in greeting.lower()
    assert "listening now" not in greeting.lower()


def test_voice_progress_hides_internal_agent_lifecycle_messages() -> None:
    assert (
        _humanize_voice_progress_text(
            "wa-p4-product-d4a6fa2ad877 agent: "
            "Delegating this request to the product_catalog_admin specialist agent."
        )
        == "I'm handing this to the product catalog specialist."
    )
    assert (
        _humanize_voice_progress_text(
            "The product agent: product catalog admin specialist has accepted the task."
        )
        == ""
    )
    assert (
        _humanize_voice_progress_text(
            "The product agent: product catalog admin specialist is working on it now."
        )
        == ""
    )
    assert _humanize_voice_progress_text("working") == ""


def test_voice_router_answers_farewell_without_delegation() -> None:
    assert _voice_direct_reply("bye")
    assert not _should_delegate_voice_transcript("bye")


def test_voice_router_recognizes_explicit_cancellation_before_delegation() -> None:
    assert _voice_is_cancellation_request("Sorry, not that, not that. Don't do that.")
    assert _voice_is_cancellation_request("Cancel that request")
    assert _voice_is_cancellation_request("Never mind, stop it")
    assert _voice_is_cancellation_request(
        "Can you help me analyze my business data? Oh, no, no, no, no, don't don't worry about that."
    )
    assert _voice_is_cancellation_request("I do not want that")
    assert not _voice_is_cancellation_request("Do not include expired products in the analysis")


def test_voice_router_delegates_inventory_question() -> None:
    assert _should_delegate_voice_transcript("Can you help me check my best-performing products?")


def test_voice_router_answers_general_inventory_help_without_delegation() -> None:
    transcript = "Okay, so yeah, I need to do some things within my inventory system and I hope you can help with that."

    assert _voice_direct_reply(transcript) == "Absolutely. Tell me the specific inventory task you want help with."
    assert not _should_delegate_voice_transcript(transcript)


def test_voice_router_requires_clarification_for_bare_time_fragment() -> None:
    clarification = _voice_clarification_requirement("I have sewed in one year")

    assert clarification is not None
    assert clarification["kind"] == "continuation"
    assert "time range" in clarification["question"]
    assert not _should_delegate_voice_transcript("I have sewed in one year")


def test_voice_router_requires_time_range_for_sales_analysis() -> None:
    clarification = _voice_clarification_requirement("Can you help me analyze my sales data?")

    assert clarification is not None
    assert clarification["kind"] == "time_range"
    assert "time range" in clarification["question"]


def test_voice_router_handles_low_stock_as_a_current_snapshot() -> None:
    assert _voice_clarification_requirement("Analyze my low-stock report") is None


def test_voice_router_repairs_low_stock_transcript_correction_before_delegation() -> None:
    correction = "I did not say new stock. I said low stock products."

    assert _voice_corrected_inventory_request(correction) == "Show low-stock products."
    transcript, remaining = _select_voice_transcript_batch(
        ["What new stock products do I have?", correction]
    )
    assert transcript == "Show low-stock products."
    assert remaining == []


def test_voice_transcript_batch_merges_question_with_time_range_follow_up() -> None:
    transcript, remaining = _select_voice_transcript_batch(
        ["Can you help me analyze my sales data?", "for the past one year"]
    )

    assert transcript == "Can you help me analyze my sales data? for the past one year"
    assert remaining == []


def test_voice_transcript_batch_drops_filler_before_sales_request() -> None:
    transcript, remaining = _select_voice_transcript_batch(
        ["OK.", "Can you help me analyze my sales data?", "for the past one year"]
    )

    assert transcript == "Can you help me analyze my sales data? for the past one year"
    assert remaining == []


def test_voice_transcript_batch_drops_farewell_before_sales_request() -> None:
    transcript, remaining = _select_voice_transcript_batch(
        ["bye", "Can you help me analyze my sales data for the past one year?"]
    )

    assert transcript == "Can you help me analyze my sales data for the past one year?"
    assert remaining == []


def test_voice_transcript_batch_merges_question_with_following_time_range_fragment() -> None:
    transcript, remaining = _select_voice_transcript_batch(
        ["OK.", "Do you know how many products?", "I have sewed in one year."]
    )

    assert transcript == "Do you know how many products?"
    assert remaining == ["I have sewed in one year."]
    clarification = _voice_clarification_requirement(transcript)
    assert clarification is not None
    assert clarification["kind"] == "continuation"


def test_voice_transcript_batch_keeps_garbled_time_range_followup_out_of_sales_request() -> None:
    transcript, remaining = _select_voice_transcript_batch(
        ["Can you help me analyze my sales data?", "I have sewed in one year."]
    )

    assert transcript == "Can you help me analyze my sales data?"
    assert remaining == ["I have sewed in one year."]
    clarification = _voice_clarification_requirement(transcript)
    assert clarification is not None
    assert clarification["kind"] == "time_range"


def test_voice_transcript_batch_merges_business_question_with_time_range_fragment() -> None:
    transcript, remaining = _select_voice_transcript_batch(
        ["Do you know how many products?", "for the past one year"]
    )

    assert transcript == "Do you know how many products?"
    assert remaining == ["for the past one year"]
    clarification = _voice_clarification_requirement(transcript)
    assert clarification is not None
    assert clarification["kind"] == "continuation"


def test_voice_router_requires_clarification_for_ambiguous_product_count_question() -> None:
    clarification = _voice_clarification_requirement("Do you know how many products for the past one year")

    assert clarification is not None
    assert clarification["kind"] == "continuation"
    assert "products in inventory" in clarification["question"]


def test_voice_merge_clarification_answer_canonicalizes_sales_request() -> None:
    merged = _voice_merge_clarification_answer(
        "Can you help me analyze my sales data?",
        "for the past one year",
        {"kind": "time_range", "original": "Can you help me analyze my sales data?"},
    )

    assert merged == "Analyze my sales data for the past one year"


def test_voice_merge_clarification_answer_reorders_time_range_then_request() -> None:
    merged = _voice_merge_clarification_answer(
        "for the past one year",
        "analyze my sales data",
        {"kind": "continuation", "original": "for the past one year"},
    )

    assert merged == "Analyze my sales data for the past one year"


def test_voice_merge_clarification_answer_removes_filler_from_inventory_count_request() -> None:
    merged = _voice_merge_clarification_answer(
        "OK. Do you know how many products?",
        "in my inventory",
        {"kind": "continuation", "original": "OK. Do you know how many products?"},
    )

    assert merged == "How many products are in my inventory?"


def test_voice_merge_clarification_answer_removes_farewell_prefix_from_full_request() -> None:
    merged = _voice_merge_clarification_answer(
        "Can you help me analyze my sales data?",
        "bye can you help me analyze my sales data for the past one year",
        {"kind": "time_range", "original": "Can you help me analyze my sales data?"},
    )

    assert merged == "Analyze my sales data for the past one year"


def test_voice_detects_actionable_business_request() -> None:
    assert _voice_has_actionable_business_request("Can you help me analyze my sales data for the past one year?")
    assert not _voice_has_actionable_business_request("for the past one year")
    assert not _voice_has_actionable_business_request("hello there")


def test_voice_fresh_business_request_supersedes_pending_clarification() -> None:
    pending = {
        "kind": "continuation",
        "question": "Tell me what you want me to analyze or check, and I’ll send it through.",
        "original": "Hello there",
    }

    assert _voice_supersedes_pending_clarification(
        "Can you help me analyze my sales data for the past one year?",
        pending,
    )


def test_voice_business_request_needing_time_range_supersedes_generic_continuation() -> None:
    pending = {
        "kind": "continuation",
        "question": "Tell me what you want me to analyze or check, and I’ll send it through.",
        "original": "Can you help me to",
    }

    assert _voice_supersedes_pending_clarification(
        "Can you help me to analyze my business performance?",
        pending,
    )


def test_voice_time_range_followup_does_not_supersede_pending_clarification() -> None:
    pending = {
        "kind": "time_range",
        "question": "What time range should I use for that analysis?",
        "original": "Can you help me analyze my sales data?",
    }

    assert not _voice_supersedes_pending_clarification("for the past one year", pending)


def test_voice_progress_clarification_upgrades_generic_continuation_to_time_range() -> None:
    merged, next_pending = _voice_progress_clarification(
        "business performance.",
        {
            "kind": "continuation",
            "question": "Tell me what you want me to analyze or check, and I’ll send it through.",
            "original": "Can you help me to analyze my...",
        },
    )

    assert "business performance" in merged.lower()
    assert next_pending is not None
    assert next_pending["kind"] == "time_range"
    assert "time range" in next_pending["question"]


def test_voice_defers_incomplete_business_request_fragment() -> None:
    clarification = _voice_clarification_requirement("Can you help me to")

    assert clarification is not None
    assert _voice_should_defer_fragment_clarification("Can you help me to", clarification)


def test_voice_does_not_defer_complete_business_request() -> None:
    clarification = _voice_clarification_requirement("Can you help me to analyze my business performance?")

    assert clarification is not None
    assert clarification["kind"] == "time_range"
    assert not _voice_should_defer_fragment_clarification(
        "Can you help me to analyze my business performance?",
        clarification,
    )


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


def test_voice_tts_expands_naira_symbol_for_unambiguous_pronunciation() -> None:
    text = "646 sales were recorded, totaling ₦210,971,280.00."

    assert _voice_tts_text(text) == "646 sales were recorded, totaling Nigerian naira 210,971,280.00."
    assert _voice_tts_text("Revenue NGN 5000") == "Revenue Nigerian naira 5000"


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
