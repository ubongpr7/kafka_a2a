from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest

from kafka_a2a.decisioning.config import DecisioningConfig
from kafka_a2a.decisioning.contracts import (
    DecisionAttribution,
    DecisionCapability,
    DecisionErrorCategory,
    DecisionFallbackReason,
    DecisionMode,
    DecisionRequest,
    DecisionResult,
    DecisionUsage,
)
from kafka_a2a.decisioning.facade import AgentDecisionFacade
from kafka_a2a.decisioning.factory import build_decision_runtime_from_env
from kafka_a2a.decisioning.policy import host_turn_intent_question
from kafka_a2a.decisioning.provider_typesafe import TypeSafeDecisionProvider
from kafka_a2a.decisioning.service import DecisionService, ShadowDecisionObservation
from kafka_a2a.decisioning.state import build_host_turn_state
from kafka_a2a.decisioning.telemetry import DecisionTelemetryEvent
from kafka_a2a.langgraph_processor import make_langgraph_chat_processor_from_env
from kafka_a2a.models import Artifact, Message, Role, Task, TaskState, TaskStatus, TextPart


def _config(**overrides: Any) -> DecisioningConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "provider": "typesafe",
        "mode": DecisionMode.shadow,
        "api_key": "typesafe-test-secret",
        "model": "jev-2026-01-01",
        "timeout_s": 0.05,
        "max_concurrency": 2,
        "retry_max_retries": 0,
        "circuit_failure_threshold": 3,
        "circuit_open_s": 30,
        "state_max_chars": 800,
        "history_items": 2,
        "shadow_sample_rate": 1.0,
        "queue_maxsize": 20,
        "state_hash_key": "test-hmac-key",
    }
    base.update(overrides)
    return DecisioningConfig(**base)


def _request() -> DecisionRequest:
    question = host_turn_intent_question()
    return DecisionRequest(
        capability=DecisionCapability.host_turn_intent,
        state={"current_user_message": "Show sales for today."},
        state_hash="digest",
        state_was_redacted=False,
        state_was_truncated=False,
        question=question,
        attribution=DecisionAttribution(
            profile_id="4",
            task_id="task-1",
            context_id="context-1",
            trace_id="trace-1",
            agent_slug="host",
        ),
        mode=DecisionMode.shadow,
        policy_version="phase1-host-intent-v1",
    )


def _success_result(choice: str = "new_request") -> DecisionResult:
    probabilities = {candidate: 0.0 for candidate in host_turn_intent_question().candidates}
    probabilities[choice] = 1.0
    return DecisionResult(
        provider="typesafe",
        model="jev-2026-01-01",
        selected_candidate=choice,
        probabilities=probabilities,
        confidence=1.0,
        usage=DecisionUsage(input_tokens=12, output_tokens=4),
    )


class _FakeProvider:
    provider_name = "typesafe"

    def __init__(self, outcomes: list[DecisionResult | BaseException], *, delay_s: float = 0) -> None:
        self.outcomes = list(outcomes)
        self.delay_s = delay_s
        self.calls = 0
        self.closed = False
        self.active = 0
        self.max_active = 0

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        _ = request
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            outcome = self.outcomes.pop(0) if self.outcomes else _success_result()
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        finally:
            self.active -= 1

    async def aclose(self) -> None:
        self.closed = True


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.events: list[DecisionTelemetryEvent] = []
        self.ready = asyncio.Event()

    def emit(self, event: DecisionTelemetryEvent) -> None:
        self.events.append(event)
        self.ready.set()


async def _wait_for_event(sink: _RecordingTelemetry, count: int = 1) -> None:
    for _ in range(50):
        if len(sink.events) >= count:
            return
        sink.ready.clear()
        await asyncio.wait_for(sink.ready.wait(), timeout=0.1)
    raise AssertionError(f"Expected {count} telemetry events, got {len(sink.events)}")


@pytest.mark.asyncio
async def test_typesafe_provider_uses_platform_secret_and_maps_valid_choice() -> None:
    request = _request()

    class _Response:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "model": "jev-2026-01-01",
                "answers": {
                    "host_turn_intent": {
                        "type": "choice",
                        "choice": "new_request",
                        "probabilities": {
                            candidate: 1.0 if candidate == "new_request" else 0.0
                            for candidate in request.question.candidates
                        },
                        "confidence": 1.0,
                    }
                },
                "usage": {"input_tokens": 12, "output_tokens": 4},
            }

    class _Client:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] | None = None

        async def post(self, url: str, **kwargs: Any) -> _Response:
            self.kwargs = {"url": url, **kwargs}
            return _Response()

        async def aclose(self) -> None:
            return None

    client = _Client()
    provider = TypeSafeDecisionProvider(config=_config(), client_factory=lambda: client)
    result = await provider.decide(request)

    assert result.is_success
    assert result.selected_candidate == "new_request"
    assert result.usage == DecisionUsage(input_tokens=12, output_tokens=4)
    assert client.kwargs is not None
    assert client.kwargs["url"] == "https://api.typesafe.ai/v1/systemone"
    assert client.kwargs["headers"]["Authorization"] == "Bearer typesafe-test-secret"
    assert client.kwargs["json"]["questions"]["host_turn_intent"]["type"] == "choice"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (429, DecisionErrorCategory.rate_limited),
        (500, DecisionErrorCategory.provider_5xx),
        (400, DecisionErrorCategory.provider_4xx),
    ],
)
async def test_typesafe_provider_maps_http_failures(status_code: int, expected: DecisionErrorCategory) -> None:
    class _Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

        def json(self) -> dict[str, Any]:
            return {}

    class _Client:
        async def post(self, *_: Any, **__: Any) -> _Response:
            return _Response(status_code)

        async def aclose(self) -> None:
            return None

    result = await TypeSafeDecisionProvider(config=_config(), client_factory=_Client).decide(_request())
    assert result.error_category is expected
    assert result.fallback_reason is DecisionFallbackReason.provider_failure


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        {"type": "choice", "choice": "not-a-candidate", "probabilities": {}, "confidence": 1.0},
        {"type": "choice", "choice": "new_request", "probabilities": {"new_request": 1.0}, "confidence": 1.0},
        {"type": "choice", "choice": "new_request", "probabilities": {candidate: 1.0 if candidate == "new_request" else 0.0 for candidate in host_turn_intent_question().candidates}},
    ],
)
async def test_typesafe_provider_rejects_invalid_choice_shapes(answer: dict[str, Any]) -> None:
    class _Response:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"answers": {"host_turn_intent": answer}}

    class _Client:
        async def post(self, *_: Any, **__: Any) -> _Response:
            return _Response()

        async def aclose(self) -> None:
            return None

    result = await TypeSafeDecisionProvider(config=_config(), client_factory=_Client).decide(_request())
    assert result.error_category in {
        DecisionErrorCategory.unexpected_candidate,
        DecisionErrorCategory.malformed_probabilities,
        DecisionErrorCategory.missing_confidence,
    }


@pytest.mark.asyncio
async def test_typesafe_provider_preserves_a_valid_low_confidence_decision() -> None:
    candidates = host_turn_intent_question().candidates

    class _Response:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "answers": {
                    "host_turn_intent": {
                        "type": "choice",
                        "choice": "follow_up",
                        "probabilities": {
                            candidate: 0.3 if candidate in {"follow_up", "new_request"} else 0.08
                            for candidate in candidates
                        },
                        "confidence": 0.3,
                    }
                }
            }

    class _Client:
        async def post(self, *_: Any, **__: Any) -> _Response:
            return _Response()

        async def aclose(self) -> None:
            return None

    result = await TypeSafeDecisionProvider(config=_config(), client_factory=_Client).decide(_request())
    assert result.is_success
    assert result.selected_candidate == "follow_up"
    assert result.confidence == 0.3


@pytest.mark.asyncio
async def test_shadow_service_emits_comparison_without_affecting_baseline() -> None:
    sink = _RecordingTelemetry()
    provider = _FakeProvider([_success_result("new_request")])
    service = DecisionService(config=_config(), provider=provider, telemetry=sink)
    observation = ShadowDecisionObservation(request=_request(), baseline_decision="new_request")

    assert service.submit_shadow(observation) is True
    await _wait_for_event(sink)
    await service.aclose()

    event = sink.events[0]
    assert event.baseline_decision == "new_request"
    assert event.result.selected_candidate == "new_request"
    assert event.comparison == "matched"
    assert provider.calls == 1
    assert provider.closed is True


@pytest.mark.asyncio
async def test_shadow_service_fails_open_retries_once_and_opens_circuit() -> None:
    sink = _RecordingTelemetry()
    failure = DecisionResult.fallback(
        provider="typesafe",
        model="jev-2026-01-01",
        fallback_reason=DecisionFallbackReason.provider_failure,
        error_category=DecisionErrorCategory.rate_limited,
    )
    provider = _FakeProvider([failure, _success_result(), failure, failure])
    service = DecisionService(
        config=_config(retry_max_retries=1, circuit_failure_threshold=1),
        provider=provider,
        telemetry=sink,
    )

    assert service.submit_shadow(ShadowDecisionObservation(request=_request(), baseline_decision="new_request"))
    await _wait_for_event(sink)
    assert sink.events[0].retry_count == 1
    assert sink.events[0].result.is_success

    assert service.submit_shadow(ShadowDecisionObservation(request=_request(), baseline_decision="new_request"))
    await _wait_for_event(sink, 2)
    assert provider.calls == 4

    assert service.submit_shadow(ShadowDecisionObservation(request=_request(), baseline_decision="new_request"))
    await _wait_for_event(sink, 3)
    assert sink.events[2].result.error_category is DecisionErrorCategory.circuit_open
    assert provider.calls == 4

    await service.aclose()


@pytest.mark.asyncio
async def test_shadow_service_handles_timeout_network_queue_and_concurrency() -> None:
    sink = _RecordingTelemetry()
    provider = _FakeProvider([asyncio.TimeoutError(), OSError("offline")] + [_success_result()] * 6, delay_s=0.01)
    service = DecisionService(config=_config(max_concurrency=2, retry_max_retries=0), provider=provider, telemetry=sink)

    for _ in range(8):
        assert service.submit_shadow(ShadowDecisionObservation(request=_request(), baseline_decision="new_request"))
    await _wait_for_event(sink, 8)
    await service.aclose()

    categories = {event.result.error_category for event in sink.events}
    assert DecisionErrorCategory.timeout in categories
    assert DecisionErrorCategory.network in categories
    assert provider.max_active <= 2


def test_disabled_or_incomplete_decisioning_never_queues_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    config = DecisioningConfig.from_env(
        {
            "KA2A_DECISIONING_ENABLED": "true",
            "KA2A_DECISIONING_MODE": "shadow",
            "KA2A_DECISIONING_MODEL": "jev-2026-01-01",
        }
    )
    assert config.is_shadow_active is False
    service = DecisionService(config=replace(config, enabled=False), provider=None)
    facade = AgentDecisionFacade(service=service, config=config, agent_slug="host")

    assert facade.observe_host_turn(
        current_user_message="Show sales.",
        history=None,
        workflow_state=None,
        awaiting_clarification=False,
        previous_host_intent=None,
        baseline_intent="new_request",
        metadata=None,
        task_id="task-1",
        context_id="context-1",
    ) is False


@pytest.mark.asyncio
async def test_runtime_starts_and_stops_without_decisioning_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "KA2A_DECISIONING_ENABLED",
        "KA2A_DECISIONING_MODE",
        "KA2A_DECISIONING_MODEL",
        "TYPESAFE_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    runtime = build_decision_runtime_from_env()
    assert runtime.config.enabled is False
    assert runtime.facade_for("host").observe_host_turn(
        current_user_message="Show sales.",
        history=None,
        workflow_state=None,
        awaiting_clarification=False,
        previous_host_intent=None,
        baseline_intent="new_request",
        metadata=None,
        task_id="task-1",
        context_id="context-1",
    ) is False
    await runtime.aclose()


def test_state_builder_bounds_redacts_and_hashes_without_persisting_secrets() -> None:
    state = build_host_turn_state(
        current_user_message="Authorization: Bearer super-secret-token api_key=other-secret " + "x" * 1000,
        history=[
            Message(role=Role.user, parts=[TextPart(text="password=hunter2 " + "y" * 1000)]),
            {"role": "assistant", "content": "previous normal response"},
        ],
        workflow_state={"workflow": "clarification", "original_request": "very private request", "token": "nope"},
        awaiting_clarification=True,
        previous_host_intent="follow_up",
        max_chars=512,
        history_items=2,
        hash_key="hmac-key",
    )
    serialized = json.dumps(state.state)

    assert len(serialized) <= 512
    assert "super-secret-token" not in serialized
    assert "other-secret" not in serialized
    assert "hunter2" not in serialized
    assert "very private request" not in serialized
    assert state.state_hash is not None
    assert state.was_redacted is True
    assert state.was_truncated is True


@pytest.mark.asyncio
async def test_facade_is_sampled_stably_and_telemetry_excludes_state_secrets() -> None:
    sink = _RecordingTelemetry()
    provider = _FakeProvider([_success_result()])
    config = _config(shadow_sample_rate=1.0)
    service = DecisionService(config=config, provider=provider, telemetry=sink)
    facade = AgentDecisionFacade(service=service, config=config, agent_slug="host")

    assert facade.observe_host_turn(
        current_user_message="api_key=do-not-log-this Show sales by location.",
        history=None,
        workflow_state=None,
        awaiting_clarification=False,
        previous_host_intent=None,
        baseline_intent="new_request",
        metadata={"profile_id": "4"},
        task_id="task-1",
        context_id="context-1",
    ) is True
    await _wait_for_event(sink)
    await service.aclose()

    event_payload = json.dumps(sink.events[0].to_dict())
    assert "do-not-log-this" not in event_payload
    assert sink.events[0].request.attribution.profile_id == "4"
    assert service.should_sample(_request()) is True
    assert DecisionService(config=_config(shadow_sample_rate=0), provider=provider).should_sample(_request()) is False


@pytest.mark.asyncio
async def test_full_decisioning_facade_observes_specialist_tool_and_response_capabilities() -> None:
    sink = _RecordingTelemetry()
    provider = _FakeProvider([_success_result()] * 4)
    config = _config()
    service = DecisionService(config=config, provider=provider, telemetry=sink)
    facade = AgentDecisionFacade(service=service, config=config, agent_slug="host")

    selected = await facade.choose_specialist(
        current_user_message="Show low stock.",
        candidates={"inventory": "Inventory specialist", "product": "Product specialist"},
        baseline_agent="inventory",
        history=None,
        workflow_state=None,
        metadata={"profile_id": "4"},
        task_id="task-1",
        context_id="context-1",
    )
    tools = await facade.shortlist_tools(
        current_user_message="Show low stock.",
        candidates={"inventory.get_stock_risk": "Get stock risk"},
        history=None,
        workflow_state=None,
        metadata={"profile_id": "4"},
        task_id="task-1",
        context_id="context-1",
    )
    assert facade.observe_specialist_result(
        current_user_message="Show low stock.",
        response_text="There are two low-stock items.",
        metadata={"profile_id": "4"},
        task_id="task-1",
        context_id="context-1",
    )
    assert facade.observe_final_response(
        current_user_message="Show low stock.",
        response_text="There are two low-stock items.",
        metadata={"profile_id": "4"},
        task_id="task-1",
        context_id="context-1",
    )
    await _wait_for_event(sink, 4)
    await service.aclose()

    assert selected == "inventory"
    assert tools == ["inventory.get_stock_risk"]
    assert {event.request.capability for event in sink.events} == {
        DecisionCapability.specialist_selection,
        DecisionCapability.tool_shortlist,
        DecisionCapability.specialist_result_evaluation,
        DecisionCapability.final_response_evaluation,
    }


@pytest.mark.asyncio
async def test_enforcement_is_explicit_bounded_and_falls_back_on_low_confidence() -> None:
    high_confidence = DecisionResult(
        provider="typesafe",
        model="jev-2026-01-01",
        selected_candidate="product",
        probabilities={"inventory": 0.05, "product": 0.95},
        confidence=0.95,
    )
    low_confidence = DecisionResult(
        provider="typesafe",
        model="jev-2026-01-01",
        selected_candidate="no_tool_needed",
        probabilities={"no_tool_needed": 0.4, "inventory.get_stock_risk": 0.6},
        confidence=0.4,
    )
    config = _config(
        mode=DecisionMode.enforce,
        enforce_capabilities=frozenset(
            {DecisionCapability.specialist_selection, DecisionCapability.tool_shortlist}
        ),
        enforce_min_confidence=0.85,
    )
    sink = _RecordingTelemetry()
    service = DecisionService(config=config, provider=_FakeProvider([high_confidence, low_confidence]), telemetry=sink)
    facade = AgentDecisionFacade(service=service, config=config, agent_slug="host")

    selected = await facade.choose_specialist(
        current_user_message="Compare product variants.",
        candidates={"inventory": "Inventory specialist", "product": "Product specialist"},
        baseline_agent="inventory",
        history=None,
        workflow_state=None,
        metadata=None,
        task_id="task-1",
        context_id="context-1",
    )
    tools = await facade.shortlist_tools(
        current_user_message="Compare product variants.",
        candidates={"inventory.get_stock_risk": "Get stock risk"},
        history=None,
        workflow_state=None,
        metadata=None,
        task_id="task-1",
        context_id="context-1",
    )
    await service.aclose()

    assert selected == "product"
    assert tools == ["inventory.get_stock_risk"]
    assert [event.outcome for event in sink.events] == ["enforcement_evaluated", "enforcement_evaluated"]


@pytest.mark.asyncio
async def test_final_response_retry_requires_explicit_capability_and_high_confidence() -> None:
    result = DecisionResult(
        provider="typesafe",
        model="jev-2026-01-01",
        selected_candidate="incomplete_or_unsupported",
        probabilities={
            "answers_request": 0.02,
            "needs_follow_up": 0.03,
            "incomplete_or_unsupported": 0.95,
        },
        confidence=0.95,
    )
    config = _config(
        mode=DecisionMode.enforce,
        enforce_capabilities=frozenset({DecisionCapability.final_response_evaluation}),
        enforce_min_confidence=0.85,
        response_retry_enabled=True,
    )
    service = DecisionService(config=config, provider=_FakeProvider([result]), telemetry=_RecordingTelemetry())
    facade = AgentDecisionFacade(service=service, config=config, agent_slug="host")

    evaluated = await facade.evaluate_final_response(
        current_user_message="Explain today's sales.",
        response_text="I cannot help.",
        metadata=None,
        task_id="task-1",
        context_id="context-1",
    )
    await service.aclose()

    assert evaluated == result
    assert facade.should_retry_final_response(evaluated) is True
    assert AgentDecisionFacade(
        service=DecisionService(config=_config(), provider=None), config=_config(), agent_slug="host"
    ).should_retry_final_response(evaluated) is False


def test_voice_decisioning_uses_an_independent_safe_runtime_profile() -> None:
    config = DecisioningConfig.from_env(
        {
            "KA2A_DECISIONING_ENABLED": "true",
            "KA2A_DECISIONING_MODE": "enforce",
            "KA2A_DECISIONING_MODEL": "jev-latest",
            "TYPESAFE_API_KEY": "platform-secret",
            "KA2A_DECISIONING_TIMEOUT_S": "0.8",
            "KA2A_DECISIONING_RETRY_MAX_RETRIES": "1",
            "KA2A_DECISIONING_SHADOW_SAMPLE_RATE": "0.1",
            "KA2A_VOICE_DECISIONING_ENABLED": "true",
            "KA2A_VOICE_DECISIONING_TIMEOUT_S": "0.25",
            "KA2A_VOICE_DECISIONING_SHADOW_SAMPLE_RATE": "0.5",
            "KA2A_VOICE_DECISIONING_ENFORCE_MIN_CONFIDENCE": "0.92",
            "KA2A_VOICE_DECISIONING_ENFORCE_CAPABILITIES": "voice_turn_readiness",
        }
    )

    voice = config.for_voice()

    assert voice.is_active is True
    assert voice.timeout_s == 0.25
    assert voice.retry_max_retries == 0
    assert voice.shadow_sample_rate == 0.5
    assert voice.enforce_min_confidence == 0.92
    assert voice.enforce_capabilities == frozenset({DecisionCapability.voice_turn_readiness})


def test_voice_decisioning_rejects_non_voice_enforcement_capabilities() -> None:
    with pytest.raises(ValueError, match="supports only voice capabilities"):
        DecisioningConfig.from_env(
            {
                "KA2A_VOICE_DECISIONING_ENFORCE_CAPABILITIES": "specialist_selection",
            }
        )


@pytest.mark.asyncio
async def test_voice_turn_decisioning_is_explicit_and_falls_back_on_low_confidence() -> None:
    high_confidence = DecisionResult(
        provider="typesafe",
        model="jev-latest",
        selected_candidate="delegate_to_host",
        probabilities={"ask_clarification": 0.04, "delegate_to_host": 0.96},
        confidence=0.96,
    )
    low_confidence = DecisionResult(
        provider="typesafe",
        model="jev-latest",
        selected_candidate="delegate_to_host",
        probabilities={"ask_clarification": 0.45, "delegate_to_host": 0.55},
        confidence=0.55,
    )
    config = _config(
        mode=DecisionMode.enforce,
        voice_enabled=True,
        voice_enforce_capabilities=frozenset({DecisionCapability.voice_turn_readiness}),
        voice_enforce_min_confidence=0.92,
    ).for_voice()
    service = DecisionService(config=config, provider=_FakeProvider([high_confidence, low_confidence]))
    facade = AgentDecisionFacade(service=service, config=config, agent_slug="voice")
    candidates = {
        "ask_clarification": "Ask for the missing detail.",
        "delegate_to_host": "Send a complete request to the host.",
    }

    selected = await facade.choose_voice_turn_readiness(
        current_user_message="Please analyze my business performance.",
        candidates=candidates,
        baseline_action="ask_clarification",
        history=None,
        metadata={"profile_id": "4"},
        task_id="voice-turn-1",
        context_id="voice-context-1",
    )
    fallback = await facade.choose_voice_turn_readiness(
        current_user_message="Please analyze my business performance.",
        candidates=candidates,
        baseline_action="ask_clarification",
        history=None,
        metadata={"profile_id": "4"},
        task_id="voice-turn-2",
        context_id="voice-context-1",
    )
    await service.aclose()

    assert selected == "delegate_to_host"
    assert fallback == "ask_clarification"


@pytest.mark.asyncio
async def test_langgraph_host_observes_shadow_intent_without_changing_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RecordingFacade:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def observe_host_turn(self, **kwargs: Any) -> bool:
            self.calls.append(kwargs)
            return True

    class _NoopFacade:
        def observe_host_turn(self, **_: Any) -> bool:
            return False

    class _FailingFacade:
        def observe_host_turn(self, **_: Any) -> bool:
            raise RuntimeError("decisioning is unavailable")

    for key, value in {
        "KA2A_LLM_CREDENTIALS_SOURCE": "env",
        "KA2A_LLM_PROVIDER": "openai_compat",
        "KA2A_LLM_API_KEY": "test-key",
        "KA2A_LLM_BASE_URL": "https://example.com",
        "KA2A_LLM_FACTORY": "tests.fake_langgraph_components:fake_llm_factory",
        "KA2A_TOOLS_ENABLED": "false",
        "KA2A_CONTEXT_MEMORY_STORE": "off",
    }.items():
        monkeypatch.setenv(key, value)

    def _task() -> Task:
        return Task(
            id="task-shadow-host",
            context_id="context-shadow-host",
            status=TaskStatus(
                state=TaskState.submitted,
                message=Message(role=Role.user, parts=[TextPart(text="Please explain this report.")]),
            ),
        )

    observed = _RecordingFacade()
    observed_processor = make_langgraph_chat_processor_from_env(
        agent_name="host",
        tool_executor_override=None,
        decision_facade=observed,
    )
    observed_task = _task()
    observed_events = [
        event async for event in observed_processor(observed_task, observed_task.status.message, None, None)
    ]

    baseline_processor = make_langgraph_chat_processor_from_env(
        agent_name="host",
        tool_executor_override=None,
        decision_facade=_NoopFacade(),
    )
    baseline_task = _task()
    baseline_events = [
        event async for event in baseline_processor(baseline_task, baseline_task.status.message, None, None)
    ]

    failing_processor = make_langgraph_chat_processor_from_env(
        agent_name="host",
        tool_executor_override=None,
        decision_facade=_FailingFacade(),
    )
    failing_task = _task()
    failing_events = [
        event async for event in failing_processor(failing_task, failing_task.status.message, None, None)
    ]

    observed_result = next(event for event in observed_events if isinstance(event, Artifact) and event.name == "result")
    baseline_result = next(event for event in baseline_events if isinstance(event, Artifact) and event.name == "result")
    failing_result = next(event for event in failing_events if isinstance(event, Artifact) and event.name == "result")
    assert observed_result.parts == baseline_result.parts
    assert failing_result.parts == baseline_result.parts
    assert observed.calls[0]["baseline_intent"] == "new_request"
    assert observed.calls[0]["current_user_message"] == "Please explain this report."
