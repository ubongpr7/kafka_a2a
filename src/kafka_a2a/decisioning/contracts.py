from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol


class DecisionMode(str, Enum):
    off = "off"
    shadow = "shadow"
    enforce = "enforce"


class DecisionCapability(str, Enum):
    host_turn_intent = "host_turn_intent"
    specialist_selection = "specialist_selection"
    tool_shortlist = "tool_shortlist"
    specialist_result_evaluation = "specialist_result_evaluation"
    final_response_evaluation = "final_response_evaluation"
    voice_turn_readiness = "voice_turn_readiness"
    voice_turn_relation = "voice_turn_relation"
    voice_response_speakability = "voice_response_speakability"


class DecisionErrorCategory(str, Enum):
    timeout = "timeout"
    network = "network"
    rate_limited = "rate_limited"
    provider_4xx = "provider_4xx"
    provider_5xx = "provider_5xx"
    invalid_response = "invalid_response"
    unexpected_candidate = "unexpected_candidate"
    malformed_probabilities = "malformed_probabilities"
    missing_confidence = "missing_confidence"
    circuit_open = "circuit_open"
    unavailable = "unavailable"
    cancelled = "cancelled"


class DecisionFallbackReason(str, Enum):
    disabled = "disabled"
    missing_configuration = "missing_configuration"
    unsupported_provider = "unsupported_provider"
    circuit_open = "circuit_open"
    provider_failure = "provider_failure"
    queue_full = "queue_full"
    cancelled = "cancelled"
    low_confidence = "low_confidence"


@dataclass(frozen=True, slots=True)
class DecisionUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class DecisionAttribution:
    profile_id: str | None
    task_id: str | None
    context_id: str | None
    trace_id: str | None
    agent_slug: str


@dataclass(frozen=True, slots=True)
class ChoiceQuestion:
    key: str
    instructions: str
    candidates: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    capability: DecisionCapability
    state: Mapping[str, Any]
    state_hash: str | None
    state_was_redacted: bool
    state_was_truncated: bool
    question: ChoiceQuestion
    attribution: DecisionAttribution
    mode: DecisionMode
    policy_version: str


@dataclass(frozen=True, slots=True)
class DecisionResult:
    provider: str
    model: str | None
    selected_candidate: str | None = None
    probabilities: Mapping[str, float] = field(default_factory=dict)
    confidence: float | None = None
    usage: DecisionUsage | None = None
    error_category: DecisionErrorCategory | None = None
    fallback_reason: DecisionFallbackReason | None = None

    @property
    def is_success(self) -> bool:
        return self.selected_candidate is not None and self.error_category is None

    @classmethod
    def fallback(
        cls,
        *,
        provider: str,
        model: str | None,
        fallback_reason: DecisionFallbackReason,
        error_category: DecisionErrorCategory | None = None,
    ) -> "DecisionResult":
        return cls(
            provider=provider,
            model=model,
            fallback_reason=fallback_reason,
            error_category=error_category,
        )


class DecisionProvider(Protocol):
    """A backend-only provider for bounded typed decisions."""

    provider_name: str

    async def decide(self, request: DecisionRequest) -> DecisionResult: ...

    async def aclose(self) -> None: ...
