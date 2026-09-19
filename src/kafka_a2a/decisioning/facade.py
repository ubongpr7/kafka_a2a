from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from kafka_a2a.decisioning.config import DecisioningConfig
from kafka_a2a.decisioning.contracts import ChoiceQuestion, DecisionCapability, DecisionMode, DecisionRequest, DecisionResult
from kafka_a2a.decisioning.policy import (
    final_response_question,
    host_turn_intent_question,
    specialist_result_question,
    specialist_selection_question,
    tool_shortlist_question,
    voice_response_speakability_question,
    voice_turn_readiness_question,
    voice_turn_relation_question,
)
from kafka_a2a.decisioning.service import DecisionService, ShadowDecisionObservation
from kafka_a2a.decisioning.state import build_host_turn_state, decision_attribution


class AgentDecisionFacade:
    """Provider-neutral decisions available to processors without vendor coupling."""

    def __init__(self, *, service: DecisionService, config: DecisioningConfig, agent_slug: str) -> None:
        self._service = service
        self._cfg = config
        self._agent_slug = agent_slug

    def _request(
        self,
        *,
        capability: DecisionCapability,
        question: ChoiceQuestion,
        current_user_message: str,
        history: Iterable[Any] | None,
        workflow_state: Mapping[str, Any] | None,
        awaiting_clarification: bool,
        previous_host_intent: str | None,
        metadata: Mapping[str, Any] | None,
        task_id: str | None,
        context_id: str | None,
        extra_state: Mapping[str, Any] | None = None,
    ) -> DecisionRequest:
        state = build_host_turn_state(
            current_user_message=current_user_message,
            history=history,
            workflow_state=workflow_state,
            awaiting_clarification=awaiting_clarification,
            previous_host_intent=previous_host_intent,
            max_chars=self._cfg.state_max_chars,
            history_items=self._cfg.history_items,
            hash_key=self._cfg.state_hash_key,
            extra_state=extra_state,
        )
        return DecisionRequest(
            capability=capability,
            state=state.state,
            state_hash=state.state_hash,
            state_was_redacted=state.was_redacted,
            state_was_truncated=state.was_truncated,
            question=question,
            attribution=decision_attribution(
                metadata=metadata,
                task_id=task_id,
                context_id=context_id,
                agent_slug=self._agent_slug,
            ),
            mode=self._cfg.mode if self._cfg.mode is not DecisionMode.off else DecisionMode.shadow,
            policy_version=self._cfg.policy_version,
        )

    def _observe(self, request: DecisionRequest, *, baseline_decision: str | None) -> bool:
        if not self._service.is_shadow_active:
            return False
        return self._service.submit_shadow(
            ShadowDecisionObservation(request=request, baseline_decision=baseline_decision)
        )

    def _bounded_candidates(self, candidates: Mapping[str, str]) -> dict[str, str]:
        return {
            str(name).strip(): str(description).strip()[:240]
            for name, description in list(candidates.items())[: self._cfg.max_candidates]
            if str(name).strip()
        }

    def observe_host_turn(
        self,
        *,
        current_user_message: str,
        history: Iterable[Any] | None,
        workflow_state: Mapping[str, Any] | None,
        awaiting_clarification: bool,
        previous_host_intent: str | None,
        baseline_intent: str | None,
        metadata: Mapping[str, Any] | None,
        task_id: str | None,
        context_id: str | None,
    ) -> bool:
        """Queue a host-intent observation; it never changes execution in shadow mode."""

        return self._observe(
            self._request(
                capability=DecisionCapability.host_turn_intent,
                question=host_turn_intent_question(),
                current_user_message=current_user_message,
                history=history,
                workflow_state=workflow_state,
                awaiting_clarification=awaiting_clarification,
                previous_host_intent=previous_host_intent,
                metadata=metadata,
                task_id=task_id,
                context_id=context_id,
            ),
            baseline_decision=baseline_intent,
        )

    async def choose_specialist(
        self,
        *,
        current_user_message: str,
        candidates: Mapping[str, str],
        baseline_agent: str | None,
        history: Iterable[Any] | None,
        workflow_state: Mapping[str, Any] | None,
        metadata: Mapping[str, Any] | None,
        task_id: str | None,
        context_id: str | None,
        permit_enforcement: bool = True,
    ) -> str | None:
        """Choose only among pre-authorized candidates when explicitly enabled."""

        bounded_candidates = self._bounded_candidates(candidates)
        if not bounded_candidates:
            return baseline_agent
        request = self._request(
            capability=DecisionCapability.specialist_selection,
            question=specialist_selection_question(bounded_candidates),
            current_user_message=current_user_message,
            history=history,
            workflow_state=workflow_state,
            awaiting_clarification=False,
            previous_host_intent=None,
            metadata=metadata,
            task_id=task_id,
            context_id=context_id,
        )
        observation = ShadowDecisionObservation(request=request, baseline_decision=baseline_agent)
        if not permit_enforcement or not self._cfg.can_enforce(DecisionCapability.specialist_selection):
            self._observe(request, baseline_decision=baseline_agent)
            return baseline_agent
        result = await self._service.decide_now(observation)
        if (
            result.is_success
            and result.selected_candidate in bounded_candidates
            and result.confidence is not None
            and result.confidence >= self._cfg.enforce_min_confidence
        ):
            return result.selected_candidate
        return baseline_agent

    async def shortlist_tools(
        self,
        *,
        current_user_message: str,
        candidates: Mapping[str, str],
        history: Iterable[Any] | None,
        workflow_state: Mapping[str, Any] | None,
        metadata: Mapping[str, Any] | None,
        task_id: str | None,
        context_id: str | None,
    ) -> list[str]:
        """Return an LLM-visible subset; direct deterministic tools remain untouched."""

        bounded_candidates = self._bounded_candidates(candidates)
        if not bounded_candidates:
            return []
        decision_candidates = {"no_tool_needed": "No tool is needed to answer this request."}
        decision_candidates.update(bounded_candidates)
        request = self._request(
            capability=DecisionCapability.tool_shortlist,
            question=tool_shortlist_question(decision_candidates),
            current_user_message=current_user_message,
            history=history,
            workflow_state=workflow_state,
            awaiting_clarification=False,
            previous_host_intent=None,
            metadata=metadata,
            task_id=task_id,
            context_id=context_id,
        )
        observation = ShadowDecisionObservation(request=request, baseline_decision=None)
        if not self._cfg.can_enforce(DecisionCapability.tool_shortlist):
            self._observe(request, baseline_decision=None)
            return list(bounded_candidates)
        result = await self._service.decide_now(observation)
        if result.confidence is None or result.confidence < self._cfg.enforce_min_confidence:
            return list(bounded_candidates)
        if result.selected_candidate == "no_tool_needed":
            return []
        if result.selected_candidate in bounded_candidates:
            return [result.selected_candidate]
        return list(bounded_candidates)

    async def choose_voice_turn_readiness(
        self,
        *,
        current_user_message: str,
        candidates: Mapping[str, str],
        baseline_action: str,
        history: Iterable[Any] | None,
        metadata: Mapping[str, Any] | None,
        task_id: str | None,
        context_id: str | None,
        extra_state: Mapping[str, Any] | None = None,
    ) -> str:
        return await self._choose_voice_action(
            capability=DecisionCapability.voice_turn_readiness,
            question=voice_turn_readiness_question,
            current_user_message=current_user_message,
            candidates=candidates,
            baseline_action=baseline_action,
            history=history,
            metadata=metadata,
            task_id=task_id,
            context_id=context_id,
            extra_state=extra_state,
        )

    async def choose_voice_turn_relation(
        self,
        *,
        current_user_message: str,
        candidates: Mapping[str, str],
        baseline_relation: str,
        history: Iterable[Any] | None,
        metadata: Mapping[str, Any] | None,
        task_id: str | None,
        context_id: str | None,
        extra_state: Mapping[str, Any] | None = None,
    ) -> str:
        return await self._choose_voice_action(
            capability=DecisionCapability.voice_turn_relation,
            question=voice_turn_relation_question,
            current_user_message=current_user_message,
            candidates=candidates,
            baseline_action=baseline_relation,
            history=history,
            metadata=metadata,
            task_id=task_id,
            context_id=context_id,
            extra_state=extra_state,
        )

    def observe_voice_response_speakability(
        self,
        *,
        current_user_message: str,
        response_text: str,
        baseline_action: str,
        metadata: Mapping[str, Any] | None,
        task_id: str | None,
        context_id: str | None,
        extra_state: Mapping[str, Any] | None = None,
    ) -> bool:
        candidates = {
            "speak": "The text is concise, user-facing, and safe to say aloud.",
            "suppress": "The text is internal lifecycle detail, raw payload content, or unsuitable for speech.",
            "needs_concise_summary": "The text is useful but needs a concise summary before speech.",
        }
        state = dict(extra_state or {})
        state["response"] = response_text
        state["baseline_action"] = baseline_action
        return self._observe(
            self._request(
                capability=DecisionCapability.voice_response_speakability,
                question=voice_response_speakability_question(candidates),
                current_user_message=current_user_message,
                history=None,
                workflow_state=None,
                awaiting_clarification=False,
                previous_host_intent=None,
                metadata=metadata,
                task_id=task_id,
                context_id=context_id,
                extra_state=state,
            ),
            baseline_decision=baseline_action,
        )

    async def _choose_voice_action(
        self,
        *,
        capability: DecisionCapability,
        question,
        current_user_message: str,
        candidates: Mapping[str, str],
        baseline_action: str,
        history: Iterable[Any] | None,
        metadata: Mapping[str, Any] | None,
        task_id: str | None,
        context_id: str | None,
        extra_state: Mapping[str, Any] | None,
    ) -> str:
        """Choose a pre-authorized LiveKit action, or preserve the local baseline."""

        bounded_candidates = self._bounded_candidates(candidates)
        if baseline_action not in bounded_candidates:
            return baseline_action
        request = self._request(
            capability=capability,
            question=question(bounded_candidates),
            current_user_message=current_user_message,
            history=history,
            workflow_state=None,
            awaiting_clarification=False,
            previous_host_intent=None,
            metadata=metadata,
            task_id=task_id,
            context_id=context_id,
            extra_state=extra_state,
        )
        observation = ShadowDecisionObservation(request=request, baseline_decision=baseline_action)
        if not self._cfg.can_enforce(capability):
            self._observe(request, baseline_decision=baseline_action)
            return baseline_action
        result = await self._service.decide_now(observation)
        if (
            result.is_success
            and result.selected_candidate in bounded_candidates
            and result.confidence is not None
            and result.confidence >= self._cfg.enforce_min_confidence
        ):
            return result.selected_candidate
        return baseline_action

    def observe_specialist_result(
        self,
        *,
        current_user_message: str,
        response_text: str,
        metadata: Mapping[str, Any] | None,
        task_id: str | None,
        context_id: str | None,
    ) -> bool:
        return self._observe_response(
            capability=DecisionCapability.specialist_result_evaluation,
            question=specialist_result_question(),
            current_user_message=current_user_message,
            response_text=response_text,
            metadata=metadata,
            task_id=task_id,
            context_id=context_id,
        )

    def observe_final_response(
        self,
        *,
        current_user_message: str,
        response_text: str,
        metadata: Mapping[str, Any] | None,
        task_id: str | None,
        context_id: str | None,
    ) -> bool:
        return self._observe_response(
            capability=DecisionCapability.final_response_evaluation,
            question=final_response_question(),
            current_user_message=current_user_message,
            response_text=response_text,
            metadata=metadata,
            task_id=task_id,
            context_id=context_id,
        )

    async def evaluate_final_response(
        self,
        *,
        current_user_message: str,
        response_text: str,
        metadata: Mapping[str, Any] | None,
        task_id: str | None,
        context_id: str | None,
    ) -> DecisionResult | None:
        """Synchronously evaluate a safe text-only retry candidate when explicitly enabled."""

        request = self._response_request(
            capability=DecisionCapability.final_response_evaluation,
            question=final_response_question(),
            current_user_message=current_user_message,
            response_text=response_text,
            metadata=metadata,
            task_id=task_id,
            context_id=context_id,
        )
        observation = ShadowDecisionObservation(request=request, baseline_decision=None)
        if not (
            self._cfg.response_retry_enabled
            and self._cfg.can_enforce(DecisionCapability.final_response_evaluation)
        ):
            self._observe(request, baseline_decision=None)
            return None
        return await self._service.decide_now(observation)

    def should_retry_final_response(self, result: DecisionResult | None) -> bool:
        return bool(
            self._cfg.response_retry_enabled
            and result is not None
            and result.is_success
            and result.selected_candidate == "incomplete_or_unsupported"
            and result.confidence is not None
            and result.confidence >= self._cfg.enforce_min_confidence
        )

    def _observe_response(
        self,
        *,
        capability: DecisionCapability,
        question: ChoiceQuestion,
        current_user_message: str,
        response_text: str,
        metadata: Mapping[str, Any] | None,
        task_id: str | None,
        context_id: str | None,
    ) -> bool:
        request = self._response_request(
            capability=capability,
            question=question,
            current_user_message=current_user_message,
            response_text=response_text,
            metadata=metadata,
            task_id=task_id,
            context_id=context_id,
        )
        return self._observe(request, baseline_decision=None)

    def _response_request(
        self,
        *,
        capability: DecisionCapability,
        question: ChoiceQuestion,
        current_user_message: str,
        response_text: str,
        metadata: Mapping[str, Any] | None,
        task_id: str | None,
        context_id: str | None,
    ) -> DecisionRequest:
        return self._request(
            capability=capability,
            question=question,
            current_user_message=current_user_message,
            history=None,
            workflow_state=None,
            awaiting_clarification=False,
            previous_host_intent=None,
            metadata=metadata,
            task_id=task_id,
            context_id=context_id,
            extra_state={"response": response_text},
        )
