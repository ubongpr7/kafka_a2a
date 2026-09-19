from __future__ import annotations

from kafka_a2a.decisioning.contracts import ChoiceQuestion, DecisionCapability


HOST_TURN_INTENT_CANDIDATES: dict[str, str] = {
    "new_request": "A new request that should be handled independently of the immediately preceding turn.",
    "follow_up": "A request that asks to refine, extend, explain, or act on the immediately preceding result.",
    "clarification_answer": "An answer to a question the host asked to clarify an earlier request.",
    "acknowledgement": "A brief acknowledgement, confirmation, thanks, or conversational response without a new task.",
    "broad_business_review": "A request for a multi-domain business performance or business data review.",
    "import_products": "A request to import products, normally from the global catalog.",
    "other": "None of the listed intents clearly describes the current user turn.",
}

SPECIALIST_RESULT_CANDIDATES: dict[str, str] = {
    "answers_request": "The specialist result materially answers the user's request.",
    "needs_follow_up": "The result is valid but a focused follow-up question is needed before continuing.",
    "incomplete_or_unsupported": "The result does not yet answer the request, lacks support, or is unusable.",
}

FINAL_RESPONSE_CANDIDATES: dict[str, str] = {
    "answers_request": "The final response materially answers the user's request with the available evidence.",
    "needs_follow_up": "The response appropriately asks a focused question needed to continue.",
    "incomplete_or_unsupported": "The response does not answer the request, is unsupported, or is unusable.",
}

VOICE_TURN_READINESS_CANDIDATES: dict[str, str] = {
    "wait_for_more_speech": "The final transcript is likely only part of a request; wait briefly for a continuation.",
    "delegate_to_host": "The caller has supplied a complete business request that should be sent to the host agent.",
    "ask_time_range": "The request is an analysis that needs a time range before it can be usefully handled.",
    "ask_metric_scope": "The request has an ambiguous metric or entity scope that needs a focused clarification.",
    "ask_clarification": "The caller's request is incomplete or ambiguous and needs a focused clarification.",
    "ignore_non_request": "The transcript is not an actionable workspace request and should not be delegated.",
}

VOICE_TURN_RELATION_CANDIDATES: dict[str, str] = {
    "new_request": "The caller has changed to an independent request and any pending clarification should be abandoned.",
    "clarification_answer": "The caller is answering the pending clarification for the previous request.",
    "continue_clarification": "The caller has not yet supplied enough information to resolve the pending clarification.",
}

VOICE_RESPONSE_SPEAKABILITY_CANDIDATES: dict[str, str] = {
    "speak": "The text is concise, user-facing, and safe to say aloud.",
    "suppress": "The text is internal lifecycle detail, raw payload content, or unsuitable for speech.",
    "needs_concise_summary": "The result is useful but too detailed to speak verbatim; a concise existing summary should be used.",
}


def host_turn_intent_question() -> ChoiceQuestion:
    return ChoiceQuestion(
        key="host_turn_intent",
        instructions=(
            "Which intent best describes `current_user_message` in the supplied conversation and workflow context? "
            "Classify only the current turn. Choose `new_request` when it changes to an unrelated task even if a "
            "previous result exists."
        ),
        candidates=HOST_TURN_INTENT_CANDIDATES,
    )


def specialist_selection_question(candidates: dict[str, str]) -> ChoiceQuestion:
    return ChoiceQuestion(
        key="specialist_selection",
        instructions=(
            "Which already-authorized specialist is the best fit for `current_user_message`? "
            "Choose only from the supplied candidates. Do not infer a missing specialist."
        ),
        candidates=candidates,
    )


def tool_shortlist_question(candidates: dict[str, str]) -> ChoiceQuestion:
    return ChoiceQuestion(
        key="tool_shortlist",
        instructions=(
            "Which already-authorized tool is most directly useful for `current_user_message`? "
            "Choose `no_tool_needed` when a tool should not be offered to the model."
        ),
        candidates=candidates,
    )


def specialist_result_question() -> ChoiceQuestion:
    return ChoiceQuestion(
        key="specialist_result_evaluation",
        instructions=(
            "Does `response.text` adequately answer `current_user_message`? "
            "Judge only the supplied final specialist response and do not invent business facts."
        ),
        candidates=SPECIALIST_RESULT_CANDIDATES,
    )


def final_response_question() -> ChoiceQuestion:
    return ChoiceQuestion(
        key="final_response_evaluation",
        instructions=(
            "Does `response.text` adequately answer `current_user_message`? "
            "Judge only the supplied final response and do not invent business facts."
        ),
        candidates=FINAL_RESPONSE_CANDIDATES,
    )


def voice_turn_readiness_question(candidates: dict[str, str]) -> ChoiceQuestion:
    return ChoiceQuestion(
        key="voice_turn_readiness",
        instructions=(
            "Which supplied voice-turn action is safest and most appropriate for the caller's current final transcript? "
            "Use only the listed actions. Do not infer an action that is not listed, and do not invent business facts."
        ),
        candidates=candidates,
    )


def voice_turn_relation_question(candidates: dict[str, str]) -> ChoiceQuestion:
    return ChoiceQuestion(
        key="voice_turn_relation",
        instructions=(
            "How does the caller's current final transcript relate to the pending voice clarification? "
            "Choose only from the supplied candidates."
        ),
        candidates=candidates,
    )


def voice_response_speakability_question(candidates: dict[str, str]) -> ChoiceQuestion:
    return ChoiceQuestion(
        key="voice_response_speakability",
        instructions=(
            "Is the supplied host result appropriate to say aloud to the caller? "
            "Judge only user-facing clarity and speech suitability. Do not assess business correctness or invent facts."
        ),
        candidates=candidates,
    )


def capability_question(capability: DecisionCapability) -> ChoiceQuestion:
    if capability is DecisionCapability.host_turn_intent:
        return host_turn_intent_question()
    if capability is DecisionCapability.voice_turn_readiness:
        return voice_turn_readiness_question(VOICE_TURN_READINESS_CANDIDATES)
    if capability is DecisionCapability.voice_turn_relation:
        return voice_turn_relation_question(VOICE_TURN_RELATION_CANDIDATES)
    if capability is DecisionCapability.voice_response_speakability:
        return voice_response_speakability_question(VOICE_RESPONSE_SPEAKABILITY_CANDIDATES)
    raise ValueError(f"Unsupported decision capability: {capability.value}")
