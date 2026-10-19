from __future__ import annotations

import pytest

from kafka_a2a.langgraph_processor import (
    _host_named_insight_from_text,
    _infer_domain_agent_name,
    _select_host_delegation_agent,
    _select_router_specialist_agent,
)


DIRECT_DEMO_PROMPTS = {
    "How many sales have we made today across all locations?": "pos::sales_by_location_today",
    "Which location is leading by value?": "pos::sales_by_location",
    "Which of my products are currently out of stock?": "inventory_visibility::stock_risk_out_of_stock",
    "Which products have sold best in the past seven days?": "pos::top_sellers_seven_days",
    "I just created my workspace. What should I set up first so inventory works properly?": "onboarding::setup_path",
    "What should I reorder this week based on sales and current stock?": "inventory_visibility::reorder_candidates",
    "A cashier says a product is not showing on the POS. What should we check?": "pos::pos_visibility_checklist",
    "Which terminal sold the most today?": "pos::terminal_cashier_activity",
    "Who changed the reorder point for Sure Men Invisible Ice?": "users::audit_search",
    "Show me risky permission changes this week.": "users::permission_security_activity",
    "Give me a morning briefing before I open the stores.": "morning_briefing",
    "Which supplier is delaying my replenishment most often?": "inventory_procurement::supplier_performance",
}


@pytest.mark.parametrize(("prompt", "expected_insight"), list(DIRECT_DEMO_PROMPTS.items()))
def test_published_demo_read_prompts_have_deterministic_specialist_contract(
    prompt: str,
    expected_insight: str,
) -> None:
    assert _host_named_insight_from_text(prompt) == expected_insight


@pytest.mark.parametrize(
    ("prompt", "expected_host", "expected_inferred_domain", "router", "expected_specialist"),
    [
        ("Can you prepare the form for adding my first warehouse?", "inventory", "inventory", "inventory", "inventory_setup"),
        ("Create a draft purchase order grouped by supplier.", "inventory", None, "inventory", "inventory_procurement"),
        (
            "I want my cashier to sell products but not approve purchase orders. Can you check the role?",
            "users",
            "users",
            None,
            "users",
        ),
        ("Notify the staff member when the role is updated.", "users", "users", None, "users"),
    ],
)
def test_published_demo_workflow_prompts_preserve_domain_and_specialist_routing(
    prompt: str,
    expected_host: str,
    expected_inferred_domain: str | None,
    router: str | None,
    expected_specialist: str,
) -> None:
    agents = [
        {"name": "inventory"},
        {"name": "inventory_procurement"},
        {"name": "inventory_setup"},
        {"name": "users"},
        {"name": "product"},
        {"name": "marketplace_sourcing"},
    ]

    assert _infer_domain_agent_name(prompt) == expected_inferred_domain
    assert _select_host_delegation_agent(prompt, agents) == expected_host
    if router is not None:
        assert _select_router_specialist_agent(router, prompt, agents) == expected_specialist


def test_contextual_demo_follow_ups_are_not_misclassified_as_fresh_domains() -> None:
    assert _host_named_insight_from_text("What should I review first?") is None
    assert _host_named_insight_from_text("What should I act on first?") is None
    assert _host_named_insight_from_text("What alternative action should I take?") is None
