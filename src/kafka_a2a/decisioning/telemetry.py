from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from kafka_a2a.decisioning.contracts import DecisionRequest, DecisionResult
from kafka_a2a.ops import inc_counter, observe_timing

logger = logging.getLogger("kafka_a2a.decisioning")


@dataclass(frozen=True, slots=True)
class DecisionTelemetryEvent:
    request: DecisionRequest
    result: DecisionResult
    baseline_decision: str | None
    comparison: str
    latency_ms: float
    retry_count: int
    outcome: str
    telemetry_topic: str

    def to_dict(self) -> dict[str, Any]:
        attribution = self.request.attribution
        event_name = "decisioning_enforcement" if self.outcome.startswith("enforcement_") else "decisioning_shadow"
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event_name,
            "telemetryTopic": self.telemetry_topic,
            "profileId": attribution.profile_id,
            "taskId": attribution.task_id,
            "contextId": attribution.context_id,
            "traceId": attribution.trace_id,
            "agentSlug": attribution.agent_slug,
            "capability": self.request.capability.value,
            "mode": self.request.mode.value,
            "policyVersion": self.request.policy_version,
            "candidateIds": list(self.request.question.candidates),
            "baselineDecision": self.baseline_decision,
            "jevDecision": self.result.selected_candidate,
            "probabilities": dict(self.result.probabilities),
            "confidence": self.result.confidence,
            "comparison": self.comparison,
            "provider": self.result.provider,
            "model": self.result.model,
            "latencyMs": round(max(0.0, self.latency_ms), 3),
            "retryCount": self.retry_count,
            "fallbackReason": self.result.fallback_reason.value if self.result.fallback_reason else None,
            "errorCategory": self.result.error_category.value if self.result.error_category else None,
            "usage": (
                {
                    "inputTokens": self.result.usage.input_tokens,
                    "outputTokens": self.result.usage.output_tokens,
                }
                if self.result.usage
                else None
            ),
            "stateHash": self.request.state_hash,
            "stateWasRedacted": self.request.state_was_redacted,
            "stateWasTruncated": self.request.state_was_truncated,
            "outcome": self.outcome,
        }


class DecisionTelemetrySink(Protocol):
    def emit(self, event: DecisionTelemetryEvent) -> None: ...


class StructuredLogDecisionTelemetry:
    """Phase 1 sink: safe structured logs and low-cardinality process metrics."""

    def emit(self, event: DecisionTelemetryEvent) -> None:
        payload = event.to_dict()
        outcome = str(payload["outcome"])
        event_name = str(payload["event"])
        inc_counter("decisioning_total")
        inc_counter(f"decisioning_event_total.{event_name}")
        inc_counter(f"decisioning_outcome_total.{outcome}")
        observe_timing("decisioning_latency", seconds=event.latency_ms / 1000.0)
        logger.info("%s event=%s", event_name, json.dumps(payload, ensure_ascii=False, sort_keys=True))
