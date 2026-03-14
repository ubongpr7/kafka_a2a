from kafka_a2a.langgraph_processor import _normalize_tool_call_payload, _render_tool_prompt_block
from kafka_a2a.tools import ToolSpec


def test_render_tool_prompt_block_discourages_tool_calls_for_plain_conversation() -> None:
    prompt = _render_tool_prompt_block(
        [
            ToolSpec(
                name="list_available_agents",
                description="List downstream specialist agents.",
                input_schema={"type": "object", "properties": {}, "required": []},
            )
        ]
    )

    assert "For greetings, small talk, capability questions, or simple summaries, answer normally in plain text." in prompt
    assert 'Never output bare tool names or pseudo-tool JSON such as {"kind":"list_available_agents"}' in prompt


def test_normalize_tool_call_payload_promotes_bare_kind_tool_call() -> None:
    payload = {"kind": "list_available_agents"}

    normalized = _normalize_tool_call_payload(payload, tool_names={"list_available_agents"})

    assert normalized == {
        "kind": "tool-call",
        "name": "list_available_agents",
        "arguments": {},
    }


def test_normalize_tool_call_payload_promotes_wrong_kind_with_arguments() -> None:
    payload = {
        "kind": "create_dynamic_form",
        "name": "create_dynamic_form",
        "arguments": {"title": "Inventory Management"},
    }

    normalized = _normalize_tool_call_payload(payload, tool_names={"create_dynamic_form"})

    assert normalized == {
        "kind": "tool-call",
        "name": "create_dynamic_form",
        "arguments": {"title": "Inventory Management"},
    }


def test_normalize_tool_call_payload_leaves_interaction_payload_untouched() -> None:
    payload = {
        "interaction_type": "dynamic_form",
        "title": "Inventory Management",
        "fields": [{"name": "quantity", "type": "number"}],
    }

    normalized = _normalize_tool_call_payload(payload, tool_names={"create_dynamic_form"})

    assert normalized == payload
