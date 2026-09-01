from __future__ import annotations

from calendar import monthrange
import json
from datetime import datetime, timezone

import pytest

from tests import fake_langgraph_components
from kafka_a2a.langgraph_processor import (
    _build_inventory_stock_risk_insight,
    _build_pos_best_sales_day_insight,
    _build_host_business_analyst_insight,
    _build_pos_product_comparison_insight,
    _build_pos_sales_by_location_insight,
    _build_pos_sales_overview_insight,
    _build_pos_top_sellers_insight,
    _enrich_top_seller_results_with_variant_context,
    _build_inventory_operation,
    _build_stock_location_operation,
    _build_realtime_dashboard_snapshot_insight,
    _build_product_operation,
    _build_product_import_opportunities_insight,
    _build_staff_activity_insight,
    _build_permission_security_insight,
    _build_subscription_usage_insight,
    _business_review_specialist_payload,
    _compact_business_review_reorder_payload,
    _business_review_specialist_domain,
    _classify_failed_operation,
    _coerce_delegated_response,
    _created_result_ref,
    _corrected_inventory_request,
    _extract_created_result_value,
    _format_delegation_status_text,
    _fresh_host_request_clarification,
    _friendly_agent_label,
    _strong_domain_agent_override,
    _host_named_insight_payload,
    _host_named_insight_from_text,
    _host_orchestration_compose_final_payload,
    _latest_host_clarification_merge,
    _latest_host_clarification_target,
    _latest_insight_follow_up_answer,
    _latest_repeated_question_response_parts,
    _inventory_procurement_named_insight_from_text,
    _host_orchestration_plan,
    _infer_domain_agent_name,
    _onboarding_creation_request,
    _infer_onboarding_scope_from_text,
    _interaction_payload_from_text,
    _inventory_setup_action_from_text,
    _is_host_capability_picker_query,
    _is_host_introspection_query,
    _is_simple_greeting_query,
    _onboarding_operation_summary,
    _normalize_tool_call_payload,
    _inventory_visibility_named_insight_from_text,
    _pos_admin_named_insight_from_text,
    _pos_admin_named_insight_payload,
    _request_explicitly_mentions_time_range,
    _resolve_insight_time_window,
    _refresh_sales_location_labels,
    _normalize_sales_location_payload,
    _render_tool_prompt_block,
    _select_host_delegation_agent,
    _select_router_handoff_agent,
    _select_router_delegation_agent,
    _text_from_parts,
    _users_named_insight_from_text,
    _users_named_insight_payload,
    make_langgraph_chat_processor_from_env,
)
from kafka_a2a.models import Artifact, DataPart, Message, Role, Task, TaskState, TaskStatus, TextPart
from kafka_a2a.tools import ToolContext, ToolSpec


@pytest.fixture(autouse=True)
def _reset_test_state(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_langgraph_components.reset_fake_components()

    monkeypatch.setenv("KA2A_LLM_CREDENTIALS_SOURCE", "env")
    monkeypatch.setenv("KA2A_LLM_PROVIDER", "openai_compat")
    monkeypatch.setenv("KA2A_LLM_API_KEY", "test-key")
    monkeypatch.setenv("KA2A_LLM_BASE_URL", "https://example.com")
    monkeypatch.setenv("KA2A_LLM_FACTORY", "tests.fake_langgraph_components:fake_llm_factory")
    monkeypatch.setenv("KA2A_TOOLS_ENABLED", "true")
    monkeypatch.setenv("KA2A_TOOLS_SOURCE", "custom")
    monkeypatch.setenv("KA2A_TOOL_EXECUTOR", "tests.fake_langgraph_components:build_fake_tool_executor")
    monkeypatch.setenv("KA2A_CONTEXT_MEMORY_STORE", "off")


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

    assert "For greetings or small talk, answer normally in plain text." in prompt
    assert "If the user asks what you can do, what help is available, or wants a list of options to choose from, prefer an interaction tool such as create_multiple_choice." in prompt
    assert 'Never output bare tool names or pseudo-tool JSON such as {"kind":"list_available_agents"}' in prompt


def test_is_simple_greeting_query_matches_plain_greetings() -> None:
    assert _is_simple_greeting_query("hi")
    assert _is_simple_greeting_query("hello there")
    assert _is_simple_greeting_query("hello again")
    assert _is_simple_greeting_query("are you there?")
    assert _is_simple_greeting_query("can you hear me now?")
    assert _is_simple_greeting_query("good evening")
    assert not _is_simple_greeting_query("hi, create my inventory")
    assert not _is_simple_greeting_query("hello, can you analyse sales for last year?")


def test_pos_admin_named_insight_from_text_detects_priority_flows() -> None:
    assert _pos_admin_named_insight_from_text("Show sales by location today") == "sales_by_location_today"
    assert _pos_admin_named_insight_from_text("Show top sellers in seven days") == "top_sellers_seven_days"
    assert _pos_admin_named_insight_from_text("How many sales was made last month?") == "sales_overview"
    assert _pos_admin_named_insight_from_text("can you analyse my sales data for the past 1 year?") == "sales_overview"
    assert _pos_admin_named_insight_from_text("Which products are selling the most?") == "top_sellers_seven_days"
    assert _pos_admin_named_insight_from_text("Show sales trend for barcode 8800000001501 over the past year") == "product_sales_trend"
    assert _pos_admin_named_insight_from_text("Compare product Eva Premium Water across locations for the past year") == "product_sales_trend"
    assert _pos_admin_named_insight_from_text("Compare products Eva Premium Water and Coca-Cola Original Taste for the past year") == "product_comparison"
    assert _pos_admin_named_insight_from_text("Compare Eva Premium Water and Coca-Cola Original Taste for the past 1 year") == "product_comparison"
    assert _host_named_insight_from_text("Compare Eva Premium Water and Coca-Cola Original Taste for the past 1 year") == "pos::product_comparison"
    assert _pos_admin_named_insight_from_text("Compare Eva Premium Water with barcode 8800000001101 for the past year") == "product_comparison"
    assert _host_named_insight_from_text("Compare Eva Premium Water with barcode 8800000001101 for the past year") == "pos::product_comparison"
    assert _pos_admin_named_insight_from_text("Compare variants of Next Pique Polo Shirt for the past year") == "variant_comparison"
    assert _pos_admin_named_insight_from_text("Show sales by location for the past month") == "sales_by_location_today"
    assert _pos_admin_named_insight_from_text("Show top sellers for the past year") == "top_sellers_seven_days"
    assert _pos_admin_named_insight_from_text("What is the highest sales I ever made in a day?") == "best_sales_day"
    assert _pos_admin_named_insight_from_text("Break down today's sales by location.") == "sales_by_location_today"
    assert _pos_admin_named_insight_from_text("Compare revenue across locations today.") == "sales_by_location_today"
    assert _pos_admin_named_insight_from_text("Summarize today's order count by location.") == "sales_by_location_today"
    assert _pos_admin_named_insight_from_text("Which branch is underperforming in sales today?") == "sales_by_location_today"
    assert _pos_admin_named_insight_from_text("Show today's average basket by location.") == "sales_by_location_today"
    assert _pos_admin_named_insight_from_text("Who were the best sellers in 7 days?") == "top_sellers_seven_days"
    assert _pos_admin_named_insight_from_text("Create a new discount") is None


def test_business_review_context_does_not_trigger_product_comparison() -> None:
    context = (
        "Continue the user's multi-domain business review. "
        "The business performance review includes an open cashier session at terminal Sofire."
    )

    assert _pos_admin_named_insight_from_text(context) != "product_comparison"


def test_product_comparison_does_not_turn_unresolved_query_into_product() -> None:
    payload = _build_pos_product_comparison_insight(
        [
            {
                "query": "Sofire",
                "totals": {"quantity_sold": 0, "sales_total": 0, "order_count": 0},
                "products": [],
                "trend": [],
            },
            {
                "query": "Eva Premium Water",
                "totals": {"quantity_sold": 4, "sales_total": 1200, "order_count": 2},
                "products": [
                    {
                        "product_name": "Eva Premium Water",
                        "variant_name": "Eva Premium Water 75cl",
                        "barcode_snapshot": "6151100030011",
                    }
                ],
                "trend": [],
            },
        ],
        window={"label": "last 3 months"},
    )

    rows = payload["widgets"][6]["rows"]
    assert [row["product"] for row in rows] == ["Eva Premium Water 75cl"]
    assert "Sofire" in payload["warnings"][0]


def test_latest_host_clarification_merge_reads_metadata_history_content_strings() -> None:
    history = [
        {"role": "user", "content": "Can you analyze my sales data?"},
        {"role": "assistant", "content": "What time range should I use for the sales analysis?"},
    ]

    assert _latest_host_clarification_merge("for the last one year or so", history) == (
        "Analyze my sales data for the last one year or so"
    )


def test_latest_host_clarification_merge_reads_serialized_message_parts() -> None:
    history = [
        {"role": "user", "parts": [{"kind": "text", "text": "Can you analyze my sales data?"}]},
        {"role": "agent", "parts": [{"kind": "text", "text": "What time range should I use for the sales analysis?"}]},
    ]

    assert _latest_host_clarification_merge("For the last one year.", history) == (
        "Analyze my sales data For the last one year"
    )
    assert _latest_host_clarification_target("For the last one year.", history) == "pos"


def test_time_range_detection_does_not_treat_contextual_from_as_a_time_range() -> None:
    history = [
        {"role": "user", "content": "Can you analyze my sales data?"},
        {"role": "assistant", "content": "What time range should I use for the sales analysis?"},
        {"role": "user", "content": "The last three weeks."},
        {"role": "assistant", "content": "Sales overview for last 3 weeks."},
    ]

    assert not _request_explicitly_mentions_time_range("Which location needs the most attention from that review?")
    assert _request_explicitly_mentions_time_range("from 2026-08-01 to 2026-08-21")
    assert _latest_host_clarification_merge("Which location needs the most attention from that review?", history) is None


def test_business_review_drops_contradictory_empty_pos_section() -> None:
    payload = _host_orchestration_compose_final_payload(
        "Analyze my business performance for the last 1 year",
        [
            {
                "agent_name": "pos_admin",
                "payload": {
                    "kind": "insight_response",
                    "summary": "9 sales were recorded for last 1 year, totaling 73400.",
                    "widgets": [{"type": "metric_grid", "data": [{"label": "Sales Count", "value": 9}]}],
                },
            },
            {
                "agent_name": "pos_admin-1D62F6C47Bd0",
                "payload": {
                    "kind": "insight_response",
                    "summary": "No completed sales were found for last 1 year.",
                    "widgets": [{"type": "metric_grid", "data": [{"label": "Sales Count", "value": 0}]}],
                },
            },
        ],
    )

    sections = payload["widgets"][1]["sections"]
    assert len(sections) == 1
    assert sections[0]["summary"].startswith("9 sales were recorded")


def test_business_review_drops_empty_variant_sales_section() -> None:
    payload = _host_orchestration_compose_final_payload(
        "Analyze my business performance for the last 1 year",
        [
            {
                "agent_name": "pos_admin",
                "payload": {
                    "kind": "insight_response",
                    "summary": "1789 sales were recorded for last 1 year, totaling 103192970.",
                    "widgets": [{"type": "metric_grid", "data": [{"label": "Sales Count", "value": 1789}]}],
                },
            },
            {
                "agent_name": "pos_admin-1D62F6C47Bd0",
                "payload": {
                    "kind": "insight_response",
                    "summary": "No variant sales were found for all time.",
                    "widgets": [{"type": "metric_grid", "data": [{"label": "Sales Count", "value": 0}]}],
                },
            },
        ],
    )

    sections = payload["widgets"][1]["sections"]
    assert len(sections) == 1
    assert sections[0]["summary"].startswith("1789 sales were recorded")


def test_host_routes_stock_snapshot_questions_to_inventory_without_a_time_range() -> None:
    assert _strong_domain_agent_override("Analyze my low-stock products") == "inventory"
    assert _fresh_host_request_clarification("Analyze my low-stock products", "inventory") is None
    assert _host_orchestration_plan(
        "Analyze my low-stock products",
        [{"name": "product"}, {"name": "inventory"}],
    ) == ["inventory"]


def test_host_disambiguates_stock_loss_and_product_risk_without_delegating() -> None:
    assert _fresh_host_request_clarification("We lose stock", "inventory") == (
        "Do you mean a current low-stock check, or stock loss and shrinkage over a time range?"
    )
    assert _fresh_host_request_clarification("I need to assess risk for my products", "inventory") == (
        "Do you want a current inventory-risk check (out of stock, low stock, reorder, and expiry), "
        "or a catalog data-quality review?"
    )


def test_host_rewrites_explicit_stock_transcript_correction() -> None:
    assert _corrected_inventory_request(
        "I did not say new stock. I said low stock products."
    ) == "Show low-stock products."


@pytest.mark.asyncio
async def test_host_executes_the_corrected_low_stock_request_without_a_workflow_prompt() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    request = "I did not say new stock. I said low stock products."
    task = Task(
        id="task-host-low-stock-correction",
        context_id="ctx-host-low-stock-correction",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=request)]),
        ),
    )

    events = [event async for event in processor(task, task.status.message, None, None)]

    assert ("inventory.get_stock_risk", {"limit": 25, "expiring_days": 30}) in fake_langgraph_components.FAKE_TOOL_CALLS
    delegation_calls = [
        arguments
        for name, arguments in fake_langgraph_components.FAKE_TOOL_CALLS
        if name == "delegate_to_agent"
    ]
    assert delegation_calls == [{"request": "Show low-stock products.", "agent_name": "inventory"}]
    result = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert "Continue Workflow" not in _text_from_parts(result.parts)


def test_build_pos_sales_by_location_insight_returns_widget_first_payload() -> None:
    payload = _build_pos_sales_by_location_insight(
        {
            "total_sales": 41400.0,
            "_window_label": "last 30 days",
            "groups": [
                {"label": "Agric", "order_count": 3, "total_sales": 22850.0},
                {"label": "Airport Road", "order_count": 3, "total_sales": 18550.0},
            ],
        }
    )

    assert payload["kind"] == "insight_response"
    assert payload["summary"] == "Agric leads sales for last 30 days."
    assert payload["widgets"][0]["type"] == "metric_grid"
    assert payload["widgets"][1]["type"] == "bar_chart"
    assert payload["widgets"][1]["title"] == "Sales by location for last 30 days"
    assert payload["data_sources"][0]["endpoint_or_topic"] == "get_sales_summary"
    assert payload["widgets"][0]["data"][2]["label"] == "Avg Basket"


def test_sales_location_labels_prefer_current_inventory_name_over_pos_snapshot() -> None:
    payload = {
        "groups": [
            {
                "label": "Gberigbe Store",
                "location_id": "loc-1",
                "order_count": 2,
                "total_sales": 1200.0,
            }
        ]
    }

    normalized = _normalize_sales_location_payload(payload, {"loc-1": "Renamed Store"})

    assert normalized["groups"] == [
        {
            "label": "Renamed Store",
            "location": "Renamed Store",
            "location_name": "Renamed Store",
            "location_id": "loc-1",
            "order_count": 2,
            "total_sales": 1200.0,
        }
    ]
def test_build_pos_sales_overview_insight_returns_widget_first_payload() -> None:
    payload = _build_pos_sales_overview_insight(
        {
            "total_sales": 41400.0,
            "_window_label": "last month",
            "groups": [
                {"label": "Agric", "order_count": 3, "total_sales": 22850.0},
                {"label": "Airport Road", "order_count": 3, "total_sales": 18550.0},
            ],
        },
        top_sellers_payload={
            "results": [
                {"product_name": "Cabin Biscuit 200g", "variant_name": "Cabin Biscuit 200g", "quantity_sold": 8, "sales_total": 16000.0, "order_count": 3, "barcode": "123"},
                {"product_name": "Body Lotion", "variant_name": "Body Lotion 500 ml", "quantity_sold": 5, "sales_total": 12400.0, "order_count": 2},
            ]
        },
        daily_sales_payload={
            "groups": [
                {"label": "2026-06-02", "order_count": 2, "total_sales": 12000.0},
                {"label": "2026-06-12", "order_count": 3, "total_sales": 17000.0},
                {"label": "2026-06-25", "order_count": 1, "total_sales": 12400.0},
            ]
        },
    )

    assert payload["kind"] == "insight_response"
    assert payload["summary"] == "6 sales were recorded for last month, totaling ₦41,400.00."
    assert payload["explanation"] == "Agric contributed the most revenue for last month at 55.2% of sales, and Cabin Biscuit 200g led the product mix."
    assert payload["widgets"][0]["type"] == "metric_grid"
    assert payload["widgets"][1]["type"] == "line_chart"
    assert payload["widgets"][2]["type"] == "comparison_table"
    assert payload["widgets"][3]["type"] == "bar_chart"
    assert payload["widgets"][3]["title"] == "Sales by location for last month"
    assert payload["widgets"][4]["type"] == "donut_chart"
    assert payload["widgets"][4]["title"] == "Order share by location for last month"
    assert payload["widgets"][5]["type"] == "bar_chart"
    assert payload["widgets"][5]["title"] == "Top products by sales amount for last month"
    assert payload["widgets"][6]["type"] == "histogram"
    assert payload["widgets"][7]["type"] == "donut_chart"
    assert payload["widgets"][8]["type"] == "ranked_list"
    ranked_item = payload["widgets"][8]["items"][0]
    assert ranked_item["barcode"] == "123"
    assert ranked_item["image_url"].startswith("data:image/svg+xml")
    assert payload["widgets"][0]["data"][0]["label"] == "Sales Count"
    assert payload["data_sources"][2]["endpoint_or_topic"] == "get_top_sellers"
    assert payload["insights"][1]["title"] == "Best trading day"


def test_build_pos_top_sellers_insight_returns_ranked_widget_payload() -> None:
    payload = _build_pos_top_sellers_insight(
        {
            "_window_label": "3 months ago",
            "results": [
                {
                    "product_name": "Cabin Biscuit 200g",
                    "variant_name": "Cabin Biscuit 200g",
                    "quantity_sold": 12,
                    "sales_total": 7800.0,
                    "order_count": 6,
                }
            ]
        }
    )

    assert payload["kind"] == "insight_response"
    assert payload["summary"] == "Cabin Biscuit 200g is the top seller for 3 months ago."
    assert payload["widgets"][0]["type"] == "metric_grid"
    assert payload["widgets"][1]["type"] == "ranked_list"
    assert payload["widgets"][1]["title"] == "Top sellers for 3 months ago"
    assert payload["widgets"][1]["items"][0]["image_url"].startswith("data:image/svg+xml")
    assert payload["data_sources"][0]["endpoint_or_topic"] == "get_top_sellers"
    assert payload["insights"][1]["detail"] == "12 units contributed ₦7,800.00 in sales across the ranked set."


def test_build_pos_best_sales_day_insight_returns_trend_payload() -> None:
    payload = _build_pos_best_sales_day_insight(
        {
            "_window_label": "all time",
            "groups": [
                {"label": "2026-05-12", "order_count": 3, "total_sales": 5000.0},
                {"label": "2026-06-03", "order_count": 7, "total_sales": 12000.0},
                {"label": "2026-06-28", "order_count": 5, "total_sales": 9000.0},
            ],
        }
    )

    assert payload["kind"] == "insight_response"
    assert payload["summary"] == "2026-06-03 was the strongest sales day in all time."
    assert payload["widgets"][0]["type"] == "metric_grid"
    assert payload["widgets"][1]["type"] == "line_chart"
    assert payload["widgets"][2]["type"] == "ranked_list"
    assert payload["insights"][0]["title"] == "Peak day"


def test_resolve_insight_time_window_supports_relative_ranges() -> None:
    today = datetime.now(timezone.utc).date()
    month_window = _resolve_insight_time_window("show sales by location for the past month", default_days=1, default_label="today")
    assert month_window["days"] == 30
    assert month_window["label"] == "last month"

    last_month_window = _resolve_insight_time_window("how many sales was made last month", default_days=1, default_label="today")
    if today.month == 1:
        assert last_month_window["start_date"] == f"{today.year - 1}-12-01"
        assert last_month_window["end_date"] == f"{today.year - 1}-12-31"
    else:
        previous_month = today.month - 1
        month_end = monthrange(today.year, previous_month)[1]
        assert last_month_window["start_date"] == f"{today.year}-{previous_month:02d}-01"
        assert last_month_window["end_date"] == f"{today.year}-{previous_month:02d}-{month_end:02d}"
    assert last_month_window["label"] == "last month"

    trailing_months_window = _resolve_insight_time_window("show purchase order analysis for the last three months", default_days=30, default_label="last 30 days")
    assert trailing_months_window["label"] == "last 3 months"
    assert trailing_months_window["end_date"] == today.isoformat()

    year_window = _resolve_insight_time_window("show top sellers for the past year", default_days=7, default_label="last 7 days")
    assert year_window["days"] == 365
    assert year_window["label"] == "last year"

    anchored_window = _resolve_insight_time_window("show staff activity from 3 months ago", default_days=30, default_label="last 30 days")
    assert 28 <= anchored_window["days"] <= 31
    assert anchored_window["label"] == "3 months ago"

    explicit_window = _resolve_insight_time_window(
        "show stock value from 2026-06-01 to 2026-06-30",
        default_days=30,
        default_label="last 30 days",
    )
    assert explicit_window["start_date"] == "2026-06-01"
    assert explicit_window["end_date"] == "2026-06-30"
    assert explicit_window["label"] == "2026-06-01 to 2026-06-30"

    named_month_window = _resolve_insight_time_window(
        "show sales by location in june 2026",
        default_days=30,
        default_label="last 30 days",
    )
    assert named_month_window["start_date"] == "2026-06-01"
    assert named_month_window["end_date"] == "2026-06-30"
    assert named_month_window["label"] == "June 2026"

    quarter_window = _resolve_insight_time_window(
        "show purchase order analysis in q1 2026",
        default_days=30,
        default_label="last 30 days",
    )
    assert quarter_window["start_date"] == "2026-01-01"
    assert quarter_window["end_date"] == "2026-03-31"
    assert quarter_window["label"] == "Q1 2026"

    all_time_window = _resolve_insight_time_window(
        "what is the highest sales I ever made in a day",
        default_days=1,
        default_label="today",
    )
    assert all_time_window["label"] == "all time"
    assert all_time_window["days"] == 3651


class _RecordingToolExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
        return []

    async def call_tool(self, *, name: str, arguments: dict[str, object], ctx: ToolContext) -> dict[str, object]:
        self.calls.append((name, arguments))
        if name == "pos.get_sales_summary":
            if arguments.get("group_by") == "day":
                return {
                    "total_sales": 2400.0,
                    "groups": [
                        {"label": "2026-06-01", "order_count": 1, "total_sales": 900.0},
                        {"label": "2026-06-04", "order_count": 1, "total_sales": 1500.0},
                    ],
                }
            return {
                "total_sales": 2400.0,
                "groups": [{"label": "HQ", "order_count": 2, "total_sales": 2400.0}],
            }
        if name == "pos.get_top_sellers":
            return {
                "results": [{"product_name": "Cabin Biscuit 200g", "variant_name": "Cabin Biscuit 200g", "quantity_sold": 5, "sales_total": 2400.0, "order_count": 2}],
            }
        if name == "pos.get_product_sales_trend":
            query = str(arguments.get("query") or "")
            if "Coca" in query:
                product_rows = [
                    {
                        "product_name": "Coca-Cola Original Taste",
                        "variant_name": "Coca-Cola Original Taste 50cl",
                        "sku_snapshot": "COKE-50CL",
                        "barcode_snapshot": "5449000000996",
                        "quantity_sold": 25,
                        "sales_total": 12500.0,
                        "order_count": 10,
                    }
                ]
                trend_rows = [
                    {"label": "2026-01-01", "quantity_sold": 10, "sales_total": 5000.0, "order_count": 4},
                    {"label": "2026-02-01", "quantity_sold": 15, "sales_total": 7500.0, "order_count": 6},
                ]
                return {
                    "query": query,
                    "totals": {"quantity_sold": 25, "sales_total": 12500.0, "order_count": 10, "average_unit_price": 500.0},
                    "trend": trend_rows,
                    "series_trend": [],
                    "locations": [],
                    "products": product_rows,
                    "recent_orders": [],
                }
            if "Eva" in query:
                product_rows = [
                    {
                        "product_name": "Eva Premium Water",
                        "variant_name": "Eva Premium Water 75cl",
                        "sku_snapshot": "EVA-75CL",
                        "barcode_snapshot": "6151100030011",
                        "quantity_sold": 40,
                        "sales_total": 16000.0,
                        "order_count": 16,
                    }
                ]
                trend_rows = [
                    {"label": "2026-01-01", "quantity_sold": 12, "sales_total": 4800.0, "order_count": 5},
                    {"label": "2026-02-01", "quantity_sold": 28, "sales_total": 11200.0, "order_count": 11},
                ]
                return {
                    "query": query,
                    "totals": {"quantity_sold": 40, "sales_total": 16000.0, "order_count": 16, "average_unit_price": 400.0},
                    "trend": trend_rows,
                    "series_trend": [],
                    "locations": [],
                    "products": product_rows,
                    "recent_orders": [],
                }
            return {
                "query": arguments.get("query"),
                "totals": {"quantity_sold": 56, "sales_total": 21000.0, "order_count": 24, "average_unit_price": 375.0},
                "trend": [
                    {"label": "2026-01-01", "quantity_sold": 12, "sales_total": 4200.0, "order_count": 5},
                    {"label": "2026-02-01", "quantity_sold": 44, "sales_total": 16800.0, "order_count": 19},
                ],
                "series_trend": [
                    {"label": "2026-01-01", "product_name": "Next Pique Polo Shirt", "variant_name": "Black L", "sku_snapshot": "NEXT-POLO-BLK-L", "barcode_snapshot": "8800000002502", "quantity_sold": 20, "sales_total": 9000.0, "order_count": 10},
                    {"label": "2026-01-01", "product_name": "Next Pique Polo Shirt", "variant_name": "Navy M", "sku_snapshot": "NEXT-POLO-NAV-M", "barcode_snapshot": "8800000002501", "quantity_sold": 8, "sales_total": 2400.0, "order_count": 2},
                    {"label": "2026-02-01", "product_name": "Next Pique Polo Shirt", "variant_name": "Black L", "sku_snapshot": "NEXT-POLO-BLK-L", "barcode_snapshot": "8800000002502", "quantity_sold": 12, "sales_total": 5700.0, "order_count": 8},
                    {"label": "2026-02-01", "product_name": "Next Pique Polo Shirt", "variant_name": "Navy M", "sku_snapshot": "NEXT-POLO-NAV-M", "barcode_snapshot": "8800000002501", "quantity_sold": 16, "sales_total": 3900.0, "order_count": 4},
                ],
                "locations": [
                    {"location": "Gberigbe Store", "quantity_sold": 28, "sales_total": 9800.0, "order_count": 11},
                    {"location": "Airport Road", "quantity_sold": 28, "sales_total": 11200.0, "order_count": 13},
                ],
                "products": [
                    {
                        "product_name": "Next Pique Polo Shirt",
                        "variant_name": "Black L",
                        "sku_snapshot": "NEXT-POLO-BLK-L",
                        "barcode_snapshot": "8800000002502",
                        "quantity_sold": 32,
                        "sales_total": 14700.0,
                        "order_count": 18,
                    },
                    {
                        "product_name": "Next Pique Polo Shirt",
                        "variant_name": "Navy M",
                        "sku_snapshot": "NEXT-POLO-NAV-M",
                        "barcode_snapshot": "8800000002501",
                        "quantity_sold": 24,
                        "sales_total": 6300.0,
                        "order_count": 6,
                    }
                ],
                "recent_orders": [
                    {
                        "completed_at": "2026-02-03T10:00:00Z",
                        "location": "Gberigbe Store",
                        "terminal_name": "Front POS",
                        "quantity": 2,
                        "unit_price": 350.0,
                        "line_total": 700.0,
                    }
                ],
            }
        if name == "product.get_variant_lookup":
            return {
                "results": [
                    {
                        "product_name": "Next Pique Polo Shirt",
                        "name": "Black L",
                        "sku": "NEXT-POLO-BLK-L",
                        "barcode": "8800000002502",
                        "image_url": "https://example.com/black-l.png",
                    },
                    {
                        "product_name": "Next Pique Polo Shirt",
                        "name": "Navy M",
                        "sku": "NEXT-POLO-NAV-M",
                        "barcode": "8800000002501",
                        "image_url": "https://example.com/navy-m.png",
                    }
                ]
            }
        if name == "product.list_global_catalog_products":
            return {
                "count": 1,
                "results": [
                    {
                        "id": "global-prod-1",
                        "name": "Global Biscuit 200g",
                        "brand": "Global",
                        "category_name": "Snacks",
                        "variant_count": 2,
                        "already_imported": False,
                        "display_image": "https://example.com/global-biscuit.png",
                        "primary_barcode": "8800000000999",
                    }
                ],
            }
        if name == "pos.get_terminal_activity":
            return {
                "results": [
                    {"terminal_name": "Front POS", "cashier_name": "Ada", "order_count": 4, "sales_total": 2400.0, "average_basket": 600.0}
                ],
            }
        if name == "audit.search_events":
            return {
                "count": 2,
                "results": [
                    {"occurred_at": "2026-07-01T10:00:00Z", "summary": "Staff updated role", "action": "update_role", "severity": "warning", "source_service": "users"},
                    {"occurred_at": "2026-07-01T09:00:00Z", "summary": "Inventory adjusted", "action": "adjustment", "severity": "info", "source_service": "inventory"},
                ],
            }
        if name == "audit.get_event_timeline":
            return {
                "count": 2,
                "timeline": [
                    {"timestamp": "2026-07-01T08:00:00Z", "title": "Access granted", "detail": "Support access granted", "severity": "warning"},
                    {"timestamp": "2026-07-01T09:00:00Z", "title": "Role updated", "detail": "Manager role expanded", "severity": "info"},
                ],
            }
        if name == "audit.get_staff_activity":
            return {
                "event_count": 3,
                "actions": [{"key": "login", "count": 2}],
                "source_services": [{"key": "users", "count": 3}],
                "daily_activity": [{"bucket": "2026-07-01", "count": 3}],
                "recent_events": [{"occurred_at": "2026-07-01T10:00:00Z", "summary": "Ada logged in", "action": "login"}],
            }
        if name == "audit.get_permission_security_activity":
            return {
                "event_count": 2,
                "actors": [{"key": "Support Agent", "count": 1}],
                "support_access_grants": [{"key": "grant-1", "count": 1}],
                "severities": [{"key": "warning", "count": 1}],
                "recent_events": [{"occurred_at": "2026-07-01T10:00:00Z", "summary": "Support access granted", "action": "grant"}],
            }
        if name == "audit.get_product_activity":
            return {
                "event_count": 2,
                "actions": [{"key": "update_product", "count": 2}],
                "source_services": [{"key": "products", "count": 2}],
                "daily_activity": [{"bucket": "2026-07-01", "count": 2}],
                "recent_events": [{"occurred_at": "2026-07-01T10:00:00Z", "summary": "Product updated", "action": "update_product"}],
            }
        if name == "audit.get_pos_activity":
            return {
                "event_count": 2,
                "actions": [{"key": "complete_sale", "count": 2}],
                "source_services": [{"key": "pos", "count": 2}],
                "daily_activity": [{"bucket": "2026-07-01", "count": 2}],
                "recent_events": [{"occurred_at": "2026-07-01T10:00:00Z", "summary": "Sale completed", "action": "complete_sale"}],
            }
        if name == "audit.get_purchase_order_activity":
            return {
                "event_count": 2,
                "timeline": [
                    {"timestamp": "2026-07-01T09:00:00Z", "title": "PO approved", "detail": "PO-101 approved", "severity": "info"},
                    {"timestamp": "2026-07-01T11:00:00Z", "title": "PO received", "detail": "PO-101 partially received", "severity": "warning"},
                ],
                "recent_events": [{"occurred_at": "2026-07-01T11:00:00Z", "summary": "PO partially received", "action": "receive"}],
            }
        if name == "audit.get_realtime_dashboard_snapshot":
            return {
                "metrics": {
                    "sales_24h_amount": 12400.5,
                    "sales_24h_orders": 11,
                    "receiving_24h_units": 42,
                    "security_events_24h": 1,
                },
                "alerts": {
                    "total_attention_items": 2,
                    "high_severity_24h": 1,
                    "support_access_24h": 1,
                },
                "charts": {"sales_amount_by_hour": [{"bucket": "09:00", "value": 3200.0}]},
                "leaderboards": {"top_products_24h": [{"title": "Cabin Biscuit 200g", "subtitle": "Units sold", "metric_value": 12}]},
                "feed": [{"occurred_at": "2026-07-01T10:00:00Z", "summary": "Sale completed", "source_service": "pos", "severity": "info"}],
            }
        if name == "notifications.get_alert_summary":
            return {
                "unread_count": 3,
                "category_counts": [{"key": "system", "count": 2}],
                "recent_notifications": [{"title": "Restock soon"}],
            }
        if name == "subscriptions.get_usage_and_limits":
            return {
                "subscription": {"status": "TRIAL", "plan": {"name": "Enterprise"}},
                "features": [{"name": "Staff users", "usage": 8, "limit_value": 10, "remaining": 2, "status": "near_limit"}],
                "warnings": ["Trial ends soon."],
            }
        if name == "inventory.get_stock_risk":
            return {
                "summary": {"out_of_stock_count": 1, "reorder_count": 1, "low_stock_count": 1, "expiring_count": 0},
                "risk_items": {"out_of_stock": [{"name": "Cabin Biscuit 200g", "quantity_available": 0, "location_name": "Lekki", "sku": "CAB-200"}]},
            }
        if name == "inventory.get_stock_movements":
            return {
                "results": [{"occurred_at": "2026-07-01T10:00:00Z", "movement_type": "adjustment", "quantity_delta": -2, "location_name": "HQ", "inventory_item_name": "Cabin Biscuit 200g"}],
            }
        if name == "inventory.get_stock_analytics":
            return {
                "analytics": {
                    "total_stock_value": 18400.0,
                    "total_locations": 2,
                    "location_distribution": [{"location_name": "Lekki", "item_count": 12, "total_quantity": 80, "total_value": 14000.0}],
                    "aging_analysis": {"0-30_days": 4, "31-90_days": 2, "91-365_days": 1, "over_1_year": 1},
                }
            }
        if name == "inventory.get_reorder_candidates":
            return {
                "count": 1,
                "results": [{"name": "Cabin Biscuit 200g", "quantity_available": 1, "quantity": 1, "location_name": "Lekki"}],
            }
        if name == "inventory.search_purchase_orders":
            return {
                "results": [{"reference_number": "PO-101", "supplier_name": "Acme", "status": "issued", "delivery_date": "2026-07-03"}],
            }
        if name == "inventory.search_stock_locations":
            return {
                "results": [{"name": "Lekki", "stock_item_count": 12, "available_quantity": 80}],
            }
        if name == "inventory.get_purchase_order_analytics":
            return {
                "analytics": {
                    "supplier_performance": [{"supplier_name": "Acme", "order_count": 2, "total_value": 3200.0, "avg_delivery_time": 4, "on_time_deliveries": 1}],
                    "on_time_delivery_rate": 50,
                    "average_delivery_time": 4,
                    "total_order_value": 3200.0,
                    "average_order_value": 1600.0,
                    "cost_per_order": 1600.0,
                }
            }
        if name == "product.get_product_dashboard_stats":
            return {
                "dashboard": {"category_distribution": [{"category_name": "Snacks", "count": 4}]},
            }
        if name == "product.get_product_stock_alerts":
            return {
                "alerts": [{"product_name": "Cabin Biscuit 200g", "alert_type": "low_stock"}],
            }
        if name == "product.search_product_variants":
            return {
                "results": [{"product_name": "Cabin Biscuit", "name": "Cabin Biscuit 200g", "sku": "CAB-200", "barcode": "123", "selling_price": 500.0}],
            }
        if name == "product.get_variant_lookup":
            return {
                "results": [{"product_name": "Cabin Biscuit", "name": "Cabin Biscuit 200g", "sku": "CAB-200", "barcode": "123", "selling_price": 500.0}],
            }
        if name == "product.get_top_catalog_matches":
            return {
                "count": 1,
                "results": [{"name": "Cabin Biscuit 200g", "brand": "Cabin", "category_name": "Snacks", "variant_count": 1, "already_imported": False}],
            }
        raise AssertionError(f"Unexpected tool call: {name}")


class _VariantLookupCountingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(self, *, name: str, arguments: dict[str, object], ctx: ToolContext) -> dict[str, object]:
        _ = ctx
        self.calls.append((name, dict(arguments)))
        if name != "product.get_variant_lookup":
            raise AssertionError(f"Unexpected tool call: {name}")
        query = str(arguments.get("query") or "")
        return {
            "results": [
                {
                    "product_name": query,
                    "name": query,
                    "sku": f"{query[:8].upper()}-SKU",
                    "barcode": f"{query[:8].upper()}-BAR",
                    "image_url": f"https://example.com/{query[:8]}.png",
                }
            ]
        }


@pytest.mark.asyncio
async def test_pos_named_insight_payload_threads_relative_date_filters_into_tools() -> None:
    executor = _RecordingToolExecutor()

    overview_payload = await _pos_admin_named_insight_payload(
        insight_key="sales_overview",
        tool_executor=executor,
        tool_ctx=ToolContext(),
        user_text="how many sales was made last month",
    )
    sales_payload = await _pos_admin_named_insight_payload(
        insight_key="sales_by_location_today",
        tool_executor=executor,
        tool_ctx=ToolContext(),
        user_text="show sales by location for the past month",
    )
    top_seller_payload = await _pos_admin_named_insight_payload(
        insight_key="top_sellers_seven_days",
        tool_executor=executor,
        tool_ctx=ToolContext(),
        user_text="show top sellers from 3 months ago",
    )
    best_day_payload = await _pos_admin_named_insight_payload(
        insight_key="best_sales_day",
        tool_executor=executor,
        tool_ctx=ToolContext(),
        user_text="what is the highest sales I ever made in a day?",
    )
    yearly_overview_payload = await _pos_admin_named_insight_payload(
        insight_key="sales_overview",
        tool_executor=executor,
        tool_ctx=ToolContext(),
        user_text="can you analyse my sales data for the past 1 year?",
    )
    product_trend_payload = await _pos_admin_named_insight_payload(
        insight_key="product_sales_trend",
        tool_executor=executor,
        tool_ctx=ToolContext(),
        user_text="show sales trend for barcode 8800000001501 over the past year",
    )
    variant_comparison_payload = await _pos_admin_named_insight_payload(
        insight_key="variant_comparison",
        tool_executor=executor,
        tool_ctx=ToolContext(),
        user_text="compare variants of Next Pique Polo Shirt for the past year",
    )
    product_comparison_payload = await _pos_admin_named_insight_payload(
        insight_key="product_comparison",
        tool_executor=executor,
        tool_ctx=ToolContext(),
        user_text="compare products Eva Premium Water and Coca-Cola Original Taste for the past year",
    )
    mixed_product_comparison_payload = await _pos_admin_named_insight_payload(
        insight_key="product_comparison",
        tool_executor=executor,
        tool_ctx=ToolContext(),
        user_text="compare Eva Premium Water with barcode 8800000001101 for the past year",
    )

    assert overview_payload is not None
    assert sales_payload is not None
    assert top_seller_payload is not None
    assert best_day_payload is not None
    assert yearly_overview_payload is not None
    assert product_trend_payload is not None
    assert product_trend_payload["widgets"][0]["type"] == "entity_preview"
    assert product_trend_payload["widgets"][2]["type"] == "line_chart"
    assert variant_comparison_payload is not None
    assert variant_comparison_payload["widgets"][1]["type"] == "ranked_list"
    assert variant_comparison_payload["widgets"][2]["type"] == "line_chart"
    assert variant_comparison_payload["widgets"][2]["series"][0]["label"] == "Black L"
    assert variant_comparison_payload["widgets"][2]["series"][1]["label"] == "Navy M"
    assert variant_comparison_payload["widgets"][4]["type"] == "line_chart"
    assert variant_comparison_payload["widgets"][7]["type"] == "comparison_table"
    assert "sku" not in variant_comparison_payload["widgets"][7]["columns"]
    assert product_comparison_payload is not None
    assert product_comparison_payload["widgets"][2]["type"] == "line_chart"
    assert product_comparison_payload["widgets"][2]["title"].startswith("Product revenue trend")
    assert product_comparison_payload["widgets"][3]["title"].startswith("Product units trend")
    assert product_comparison_payload["widgets"][4]["title"].startswith("Product order-count trend")
    assert product_comparison_payload["widgets"][6]["type"] == "comparison_table"
    assert "sku" not in product_comparison_payload["widgets"][6]["columns"]
    assert mixed_product_comparison_payload is not None
    assert mixed_product_comparison_payload["widgets"][2]["type"] == "line_chart"
    assert mixed_product_comparison_payload["widgets"][6]["type"] == "comparison_table"
    assert "sku" not in mixed_product_comparison_payload["widgets"][6]["columns"]
    overview_window = _resolve_insight_time_window("how many sales was made last month", default_days=1, default_label="today")
    sales_window = _resolve_insight_time_window("show sales by location for the past month", default_days=1, default_label="today")
    top_sellers_window = _resolve_insight_time_window("show top sellers from 3 months ago", default_days=7, default_label="last 7 days")
    all_time_window = _resolve_insight_time_window("what is the highest sales I ever made in a day?", default_days=1, default_label="today")
    yearly_window = _resolve_insight_time_window("can you analyse my sales data for the past 1 year?", default_days=1, default_label="today")
    product_window = _resolve_insight_time_window("show sales trend for barcode 8800000001501 over the past year", default_days=365, default_label="last 1 year")
    variant_window = _resolve_insight_time_window("compare variants of Next Pique Polo Shirt for the past year", default_days=365, default_label="last 1 year")
    comparison_window = _resolve_insight_time_window("compare Eva Premium Water with barcode 8800000001101 for the past year", default_days=365, default_label="last 1 year")
    pos_calls = [call for call in executor.calls if call[0].startswith("pos.")]
    assert pos_calls[0] == (
        "pos.get_sales_summary",
        {"days": overview_window["days"], "date": overview_window["anchor_date"], "group_by": "location"},
    )
    assert pos_calls[1] == (
        "pos.get_sales_summary",
        {"days": overview_window["days"], "date": overview_window["anchor_date"], "group_by": "day"},
    )
    assert pos_calls[2] == (
        "pos.get_top_sellers",
        {"days": overview_window["days"], "date": overview_window["anchor_date"], "limit": 5},
    )
    assert pos_calls[3] == (
        "pos.get_sales_summary",
        {"days": sales_window["days"], "date": sales_window["anchor_date"], "group_by": "location"},
    )
    assert pos_calls[4][0] == "pos.get_top_sellers"
    assert pos_calls[4][1]["days"] == top_sellers_window["days"]
    assert pos_calls[4][1]["date"] == top_sellers_window["anchor_date"]
    assert pos_calls[5] == (
        "pos.get_sales_summary",
        {"days": all_time_window["days"], "date": all_time_window["anchor_date"], "group_by": "day"},
    )
    assert pos_calls[6] == (
        "pos.get_sales_summary",
        {"days": yearly_window["days"], "date": yearly_window["anchor_date"], "group_by": "location"},
    )
    assert pos_calls[7] == (
        "pos.get_sales_summary",
        {"days": yearly_window["days"], "date": yearly_window["anchor_date"], "group_by": "day"},
    )
    assert pos_calls[8] == (
        "pos.get_top_sellers",
        {"days": yearly_window["days"], "date": yearly_window["anchor_date"], "limit": 5},
    )
    assert ("product.get_variant_lookup", {"query": "8800000001501", "limit": 1, "active_only": True}) in executor.calls
    assert pos_calls[9] == (
        "pos.get_product_sales_trend",
        {"days": product_window["days"], "date": product_window["anchor_date"], "query": "8800000001501", "group_by": "month", "limit": 10},
    )
    assert ("product.get_variant_lookup", {"query": "Next Pique Polo Shirt", "limit": 10, "active_only": True}) in executor.calls
    assert pos_calls[10] == (
        "pos.get_product_sales_trend",
        {
            "days": variant_window["days"],
            "date": variant_window["anchor_date"],
            "query": "Next Pique Polo Shirt",
            "group_by": "month",
            "limit": 25,
            "include_series": True,
            "include_locations": False,
            "include_recent": False,
        },
    )
    assert (
        "pos.get_product_sales_trend",
        {
            "days": comparison_window["days"],
            "date": comparison_window["anchor_date"],
            "query": "Eva Premium Water",
            "group_by": "month",
            "limit": 10,
            "include_series": False,
            "include_locations": False,
            "include_recent": False,
        },
    ) in pos_calls
    assert (
        "pos.get_product_sales_trend",
        {
            "days": comparison_window["days"],
            "date": comparison_window["anchor_date"],
            "query": "8800000001101",
            "group_by": "month",
            "limit": 10,
            "include_series": False,
            "include_locations": False,
            "include_recent": False,
        },
    ) in pos_calls


@pytest.mark.asyncio
async def test_enrich_top_sellers_deduplicates_lookup_queries() -> None:
    executor = _VariantLookupCountingExecutor()

    payload = await _enrich_top_seller_results_with_variant_context(
        results_payload={
            "results": [
                {
                    "variant_name": "Eva Premium Water 75cl",
                    "product_name": "Eva Premium Water 75cl",
                    "quantity_sold": 16,
                    "sales_total": 5600.0,
                },
                {
                    "variant_name": "Eva Premium Water 75cl",
                    "product_name": "Eva Premium Water 75cl",
                    "quantity_sold": 12,
                    "sales_total": 4200.0,
                },
                {
                    "variant_name": "Fanta Orange 50cl",
                    "product_name": "Fanta Orange 50cl",
                    "quantity_sold": 8,
                    "sales_total": 3600.0,
                },
            ]
        },
        tool_executor=executor,
        tool_ctx=ToolContext(),
        limit=3,
    )

    results = payload["results"]
    assert isinstance(results, list)
    assert len(results) == 3
    product_lookup_calls = [call for call in executor.calls if call[0] == "product.get_variant_lookup"]
    assert len(product_lookup_calls) == 2
    assert {call[1]["query"] for call in product_lookup_calls} == {"Eva Premium Water 75cl", "Fanta Orange 50cl"}
    assert results[0]["image_url"] == "https://example.com/Eva Prem.png"
    assert results[2]["barcode"] == "FANTA OR-BAR"


@pytest.mark.asyncio
async def test_enrich_top_sellers_caps_lookup_queries_to_identifier_and_name() -> None:
    class _FallbackLookupExecutor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call_tool(self, *, name: str, arguments: dict[str, object], ctx: ToolContext) -> dict[str, object]:
            _ = ctx
            self.calls.append((name, dict(arguments)))
            query = str(arguments.get("query") or "")
            if query == "Eva Premium Water 75cl":
                return {
                    "results": [
                        {
                            "product_name": "Eva Premium Water",
                            "name": "Eva Premium Water 75cl",
                            "sku": "EVA-75CL",
                            "barcode": "8800000001101",
                            "image_url": "https://example.com/eva.png",
                        }
                    ]
                }
            return {"results": []}

    executor = _FallbackLookupExecutor()

    payload = await _enrich_top_seller_results_with_variant_context(
        results_payload={
            "results": [
                {
                    "variant_name": "Eva Premium Water 75cl",
                    "product_name": "Eva Premium Water",
                    "sku_snapshot": "EVA-STALE",
                    "quantity_sold": 16,
                    "sales_total": 5600.0,
                }
            ]
        },
        tool_executor=executor,
        tool_ctx=ToolContext(),
        limit=1,
    )

    product_lookup_calls = [call for call in executor.calls if call[0] == "product.get_variant_lookup"]
    assert [call[1]["query"] for call in product_lookup_calls] == ["Eva Premium Water 75cl"]
    assert payload["results"][0]["image_url"] == "https://example.com/eva.png"


def test_inventory_visibility_named_insight_from_text_detects_priority_flows() -> None:
    assert _inventory_visibility_named_insight_from_text("Show out-of-stock products.") == "stock_risk_out_of_stock"
    assert _inventory_visibility_named_insight_from_text("Show low-stock products.") == "stock_risk_low_stock"
    assert _inventory_visibility_named_insight_from_text("Show stock risk alerts.") == "stock_risk"
    assert _inventory_visibility_named_insight_from_text("Show reorder candidates now.") == "reorder_candidates"
    assert _inventory_visibility_named_insight_from_text("Show stock value changes for the last three months.") == "stock_value_changes"
    assert _inventory_visibility_named_insight_from_text("Show the realtime dashboard snapshot right now.") == "realtime_snapshot"
    assert _inventory_visibility_named_insight_from_text("Show zero-balance items that need attention now.") == "stock_risk_out_of_stock"
    assert _inventory_visibility_named_insight_from_text("What products are missing from the shelf today?") == "stock_risk_out_of_stock"
    assert _inventory_visibility_named_insight_from_text("Adjust stock for item A") is None


def test_build_inventory_stock_risk_insight_returns_risk_widgets() -> None:
    payload = _build_inventory_stock_risk_insight(
        {
            "summary": {
                "out_of_stock_count": 2,
                "reorder_count": 3,
                "low_stock_count": 1,
                "expiring_count": 0,
            },
            "risk_items": {
                "out_of_stock": [
                    {
                        "name": "Cabin Biscuit 200g",
                        "quantity_available": 0,
                    "location_name": "Lekki",
                    "sku": "CAB-200",
                    "barcode": "1234567890123",
                    "product_variant_image_url": "https://cdn.example.com/cabin-biscuit.jpg",
                }
            ]
            },
        },
        focus="out_of_stock",
    )

    assert payload["kind"] == "insight_response"
    assert payload["widgets"][0]["type"] == "metric_grid"
    assert payload["widgets"][1]["type"] == "risk_panel"
    assert payload["widgets"][2]["type"] == "ranked_list"
    assert payload["widgets"][2]["items"][0]["image_url"] == "https://cdn.example.com/cabin-biscuit.jpg"
    assert payload["widgets"][3]["type"] == "comparison_table"
    assert payload["widgets"][3]["rows"] == [
        {
            "risk": "Out of Stock",
            "product": "Cabin Biscuit 200g",
            "barcode": "1234567890123",
            "sku": "CAB-200",
            "location": "Lekki",
            "available": 0.0,
            "minimum_stock": 0.0,
            "reorder_point": 0.0,
        }
    ]
    assert payload["data_sources"][0]["endpoint_or_topic"] == "get_stock_risk"


def test_business_review_reorder_section_is_compact() -> None:
    payload = _compact_business_review_reorder_payload(
        _build_inventory_stock_risk_insight(
            {
                "summary": {"out_of_stock_count": 0, "reorder_count": 2, "low_stock_count": 0, "expiring_count": 0},
                "risk_items": {
                    "needs_reorder": [
                        {"name": "Cabin Biscuit 200g", "quantity_available": 1, "location_name": "Lekki"},
                    ]
                },
            },
            focus="needs_reorder",
        )
    )

    assert payload is not None
    assert payload["summary"] == "Replenishment candidates are ready."
    assert [widget["type"] for widget in payload["widgets"]] == ["metric_grid", "ranked_list"]
    assert payload["widgets"][0]["data"] == [{"label": "Products to replenish", "value": 2, "format": "number"}]
    assert payload["widgets"][1]["title"] == "Products to replenish"


def test_build_product_import_opportunities_preserves_catalog_media() -> None:
    payload = _build_product_import_opportunities_insight(
        {
            "count": 1,
            "results": [
                {
                    "name": "Eva Premium Water 75cl",
                    "brand": "Eva",
                    "category_name": "Beverages",
                    "variant_count": 1,
                    "already_imported": False,
                    "display_image": "https://example.com/eva.png",
                    "barcode": "8800000001001",
                    "sku": "EVA-75CL",
                }
            ],
        }
    )

    ranked_item = payload["widgets"][1]["items"][0]
    assert ranked_item["image_url"] == "https://example.com/eva.png"
    assert ranked_item["barcode"] == "8800000001001"
    assert ranked_item["sku"] == "EVA-75CL"


def test_low_stock_summary_surfaces_out_of_stock_items() -> None:
    payload = _build_inventory_stock_risk_insight(
        {
            "summary": {
                "out_of_stock_count": 12,
                "reorder_count": 0,
                "low_stock_count": 0,
                "expiring_count": 0,
            },
            "risk_items": {
                "low_stock": [],
                "out_of_stock": [
                    {
                        "name": "Cabin Biscuit 200g",
                        "barcode": "1234567890123",
                        "sku": "CAB-200",
                        "location_name": "Lekki",
                        "quantity_available": 0,
                    }
                ],
            },
        },
        focus="low_stock",
    )

    assert payload["summary"] == (
        "No products are below their low-stock threshold, but 12 products are out of stock and need attention."
    )
    assert payload["widgets"][3]["rows"][0]["risk"] == "Out of Stock"
    assert payload["widgets"][3]["rows"][0]["barcode"] == "1234567890123"


def test_users_named_insight_from_text_detects_priority_flows() -> None:
    assert _users_named_insight_from_text("Show staff activity from audit events") == "staff_activity"
    assert _users_named_insight_from_text("Show staff activity in June 2026.") == "staff_activity"
    assert _users_named_insight_from_text("Show support access audit") == "support_access_audit"
    assert _users_named_insight_from_text("Show permission and security activity") == "permission_security_activity"
    assert _users_named_insight_from_text("Show recent audit logs for the past month") == "audit_search"
    assert _users_named_insight_from_text("Show the audit timeline for this workspace") == "audit_timeline"
    assert _users_named_insight_from_text("Show subscription usage and limits") == "subscription_usage_limits"
    assert _users_named_insight_from_text("Invite a staff member") is None


def test_host_named_insight_from_text_detects_cross_domain_flows() -> None:
    assert _host_named_insight_from_text("Act as my business analyst and tell me what I am not seeing.") == "business_analyst_review"
    assert _host_named_insight_from_text("Can you analyze the entire data as a data analyst?") == "business_analyst_review"
    assert _host_named_insight_from_text("Give me a strategic business review for the past year.") == "business_analyst_review"
    assert _host_named_insight_from_text("Analyze my business performance for the last quarter.") == "business_analyst_review"
    assert _host_named_insight_from_text("Analyze my business data.") == "business_analyst_review"
    assert _host_named_insight_from_text("Analyze my business for the last quarter.") == "business_analyst_review"
    assert _host_named_insight_from_text("Give me a quarterly business performance review.") == "business_analyst_review"
    assert _host_named_insight_from_text("analyse my entire system for the past 1 year") == "business_analyst_review"
    assert _host_named_insight_from_text("review the whole system for the past year") == "business_analyst_review"
    assert _host_named_insight_from_text("How many sales was made last month?") == "pos::sales_overview"
    assert _host_named_insight_from_text("Give me the sales analysis for last month.") == "pos::sales_overview"
    assert _host_named_insight_from_text("can you analyse my sales data for the past 1 year?") == "pos::sales_overview"
    assert _host_named_insight_from_text("how many goods has been sold today") == "pos::top_sellers_seven_days"
    assert _host_named_insight_from_text("how many products did we sell today") == "pos::top_sellers_seven_days"
    assert _host_named_insight_from_text("How many units were sold today?") == "pos::top_sellers_seven_days"
    assert _host_named_insight_from_text("what is my revenue today") == "pos::sales_overview"
    assert _host_named_insight_from_text("which terminal sold the most today") == "pos::terminal_cashier_activity"
    assert _host_named_insight_from_text("which location sold the most this month") == "pos::sales_by_location_today"
    assert _host_named_insight_from_text("show failed POS payments today") == "pos::pos_exceptions"
    assert _host_named_insight_from_text("show refunds today") == "pos::pos_exceptions"
    assert _host_named_insight_from_text("Show sales trend for barcode 8800000001501 over the past year") == "pos::product_sales_trend"
    assert _host_named_insight_from_text("Show out-of-stock products.") == "inventory_visibility::stock_risk_out_of_stock"
    assert _host_named_insight_from_text("what should I reorder today") == "inventory_visibility::reorder_candidates"
    assert _host_named_insight_from_text("which products are expiring soon") == "inventory_visibility::stock_risk"
    assert _host_named_insight_from_text("show stock transfer activity") == "inventory_visibility::stock_movements"
    assert _host_named_insight_from_text("Show purchase order lifecycle for the past month") == "inventory_procurement::po_lifecycle"
    assert _host_named_insight_from_text("Show PO receiving lifecycle for the past 1 year") == "inventory_procurement::receiving_lifecycle"
    assert _host_named_insight_from_text("Show purchase order receiving timeline last year") == "inventory_procurement::receiving_lifecycle"
    assert _host_named_insight_from_text("analyze my purchase orders for last quarter") == "inventory_procurement::po_lifecycle"
    assert _host_named_insight_from_text("which suppliers have delayed deliveries") == "inventory_procurement::delay_exceptions"
    assert _host_named_insight_from_text("Show staff activity from audit events") == "users::staff_activity"
    assert _host_named_insight_from_text("Show staff activity in June 2026.") == "users::staff_activity"
    assert _host_named_insight_from_text("who accessed support last month") == "users::support_access_audit"
    assert _host_named_insight_from_text("Show global catalog import opportunities") == "product_discovery::import_opportunities"
    assert _host_named_insight_from_text("show products with no image") == "product_discovery::media_category"
    assert _host_named_insight_from_text("show duplicate barcodes") == "product_discovery::duplicate_codes"
    assert _host_named_insight_from_text("show products not visible in POS") == "product_discovery::media_category"
    assert _host_named_insight_from_text("Show subscription usage and limits") == "users::subscription_usage_limits"
    assert _host_named_insight_from_text("Give me a one-screen operational summary for today.") == "cross_domain_ops"
    assert _host_named_insight_from_text("Which areas are strong and weak across inventory and POS?") == "cross_domain_ops"
    assert _host_named_insight_from_text("Show side-by-side location performance for today.") == "location_comparison"
    assert _host_named_insight_from_text("Compare branches by top sellers and stockouts.") == "location_comparison"
    assert _host_named_insight_from_text("Compare Maitama and Agric sales this year") is None
    assert _host_named_insight_from_text("What are the top three actions I should take next?") == "recommendations"


def test_build_host_business_analyst_insight_returns_cross_service_widgets() -> None:
    payload = _build_host_business_analyst_insight(
        {
            "_window_label": "last 1 year",
            "_window_start_date": "2025-07-11",
            "_window_end_date": "2026-07-10",
            "currency_code": "NGN",
            "groups": [
                {"label": "2026-01-01", "total_sales": 10000, "order_count": 8},
                {"label": "2026-01-02", "total_sales": 25000, "order_count": 12},
            ],
        },
        {
            "_window_label": "last 1 year",
            "groups": [
                {"label": "Gberigbe Store", "total_sales": 22000, "order_count": 10},
                {"label": "Airport Road", "total_sales": 13000, "order_count": 10},
            ],
        },
        {
            "results": [
                {
                    "variant_name": "Eva Premium Water 75cl",
                    "product_name": "Eva Premium Water",
                    "sales_total": 18000,
                    "quantity_sold": 60,
                    "order_count": 9,
                    "barcode_snapshot": "8800000001501",
                }
            ]
        },
        {
            "summary": {
                "out_of_stock_count": 2,
                "reorder_count": 4,
                "expiring_count": 1,
            }
        },
        {"status_counts": {"pending": 2, "received": 3}},
        {"features": [{"name": "AI coins", "status": "near_limit"}]},
    )

    assert payload["kind"] == "insight_response"
    assert "Business analyst review" in payload["summary"]
    assert payload["widgets"][0]["type"] == "metric_grid"
    assert payload["widgets"][1]["title"].startswith("Revenue trend")
    assert payload["widgets"][2]["title"].startswith("Order-count trend")
    assert payload["widgets"][3]["type"] == "comparison_table"
    assert payload["widgets"][4]["type"] == "ranked_list"
    assert payload["widgets"][5]["type"] == "risk_panel"
    assert payload["widgets"][6]["title"] == "Recommended owner actions"
    assert payload["widgets"][6]["items"][0]["hide_value"] is True
    assert payload["widgets"][5]["items"][0]["description"]
    assert payload["data_sources"][0]["service"] == "pos"


def test_latest_insight_follow_up_answer_uses_previous_structured_payload() -> None:
    payload = _build_host_business_analyst_insight(
        {
            "_window_label": "last 1 year",
            "_window_start_date": "2025-07-11",
            "_window_end_date": "2026-07-10",
            "currency_code": "NGN",
            "groups": [
                {"label": "2026-01-01", "total_sales": 10000, "order_count": 8},
                {"label": "2026-01-02", "total_sales": 25000, "order_count": 12},
            ],
        },
        {
            "_window_label": "last 1 year",
            "groups": [
                {"label": "Gberigbe Store", "total_sales": 22000, "order_count": 10},
                {"label": "Airport Road", "total_sales": 13000, "order_count": 10},
            ],
        },
        {
            "results": [
                {
                    "variant_name": "Eva Premium Water 75cl",
                    "product_name": "Eva Premium Water",
                    "sales_total": 18000,
                    "quantity_sold": 60,
                    "order_count": 9,
                    "barcode_snapshot": "8800000001501",
                }
            ]
        },
        {"summary": {"out_of_stock_count": 2, "reorder_count": 4, "expiring_count": 1}},
        {"status_counts": {"pending": 2, "received": 3}},
        {"features": [{"name": "AI coins", "status": "near_limit"}]},
    )
    history = [{"role": "assistant", "content": json.dumps(payload)}]
    frontend_history = [
        {
            "role": "assistant",
            "content": "Business analyst review for last 1 year.",
            "structured_payload": payload,
        }
    ]
    wrapped_history = [
        {
            "role": "assistant",
            "message": {
                "role": "assistant",
                "content": "Business analyst review for last 1 year.",
                "structuredPayload": payload,
            },
        }
    ]
    direct_stream_history = [
        {
            "role": "assistant",
            "content": "Business analyst review for last 1 year.",
            "structuredPayload": payload,
        },
        {"role": "user", "content": "Which location led?"},
    ]
    repeated_history = [
        {"role": "user", "content": "Act as my business analyst and tell me what I am not seeing for the past year"},
        {
            "role": "assistant",
            "content": "Business analyst review for last 1 year.",
            "structuredPayload": payload,
        },
        {"role": "user", "content": "Act as my business analyst and tell me what I am not seeing for the past year"},
    ]

    assert "Gberigbe Store" in (_latest_insight_follow_up_answer("Which location led?", history) or "")
    assert "Gberigbe Store" in (_latest_insight_follow_up_answer("Which location led?", frontend_history) or "")
    assert "Gberigbe Store" in (_latest_insight_follow_up_answer("Which location was far behind in terms of revenue in that period?", wrapped_history) or "")
    assert "Gberigbe Store" in (_latest_insight_follow_up_answer("Which location led?", direct_stream_history) or "")
    assert (_latest_insight_follow_up_answer("So these are my sales reports, right?", frontend_history) or "").startswith("Yes.")
    grouped_locations = _latest_insight_follow_up_answer("Okay, group it by location.", frontend_history) or ""
    assert "already grouped by location" in grouped_locations
    assert "Gberigbe Store" in grouped_locations
    assert "Prioritize replenishment" in (_latest_insight_follow_up_answer("What should I do first?", history) or "")
    assert "Prioritize replenishment" in (
        _latest_insight_follow_up_answer("What should I do first?", frontend_history) or ""
    )
    decisions = _latest_insight_follow_up_answer("What decisions should I be making with this information?", frontend_history) or ""
    assert "Prioritize replenishment" in decisions
    assert _latest_insight_follow_up_answer("I need to import products", frontend_history) is None
    assert _latest_insight_follow_up_answer("next action", frontend_history) is not None
    assert "Stockouts" in (_latest_insight_follow_up_answer("What are the risks?", history) or "")
    assert "Eva Premium Water" in (_latest_insight_follow_up_answer("Which products drove revenue?", history) or "")
    assert "₦35,000.00" in (_latest_insight_follow_up_answer("What was total revenue?", history) or "")
    assert _latest_insight_follow_up_answer("Give me last month sales instead", history) is None
    assert _latest_insight_follow_up_answer("Can you hear me now?", history) is None

    business_review_history = [
        {
            "role": "assistant",
            "content": "Business review ready.",
            "structuredPayload": {
                "kind": "insight_response",
                "timeframe": {"label": "last 3 months"},
                "widgets": [
                    {
                        "type": "section_stack",
                        "sections": [
                            {
                                "title": "Point of Sale (POS)",
                                "widgets": [
                                    {
                                        "type": "comparison_table",
                                        "title": "Location contribution for last 3 months",
                                        "rows": [
                                            {"location": "Maitama, Abuja", "sales": 8652220, "orders": 153},
                                            {"location": "Agric, Ikorodu Store", "sales": 4879620, "orders": 85},
                                        ],
                                    }
                                ],
                            },
                            {
                                "title": "Inventory Management",
                                "widgets": [
                                    {
                                        "type": "risk_panel",
                                        "title": "Highest priority stock risks - All locations",
                                        "items": [
                                            {"label": "Out of Stock: Rice", "detail": "Maitama, Abuja · out of stock"},
                                            {"label": "Out of Stock: Toothpaste", "detail": "Maitama, Abuja · out of stock"},
                                        ],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            },
        }
    ]
    location_attention = _latest_insight_follow_up_answer(
        "Which location needs the most attention from that review?",
        business_review_history,
    ) or ""
    assert "Maitama, Abuja" in location_attention
    assert "2 active stock risks" in location_attention
    assert "₦8,652,220.00" in location_attention

    procurement_payload = {
        "kind": "insight_response",
        "summary": "Purchase-order receiving lifecycle is ready for last 1 year.",
        "timeframe": {"label": "last 1 year"},
        "widgets": [
            {
                "type": "progress_tracker",
                "title": "Receiving progress",
                "steps": [
                    {"label": "PO-1001", "status": "current", "detail": "Issued with supplier Multipro"},
                    {"label": "PO-1002", "status": "completed", "detail": "Received with supplier Nestle"},
                ],
            }
        ],
    }
    procurement_history = [{"role": "assistant", "content": "Receiving lifecycle ready.", "structuredPayload": procurement_payload}]
    assert "unfinished receiving work" in (
        _latest_insight_follow_up_answer("From the receiving lifecycle, what is the bottleneck?", procurement_history) or ""
    )
    assert "current: 1" in (
        _latest_insight_follow_up_answer("From the receiving lifecycle, which statuses need attention?", procurement_history) or ""
    )
    assert "PO-1001" in (
        _latest_insight_follow_up_answer("From the receiving lifecycle, what next action should purchasing take?", procurement_history) or ""
    )

    staff_payload = {
        "kind": "insight_response",
        "summary": "Staff audit activity is ready for last 3 months.",
        "timeframe": {"label": "last 3 months"},
        "widgets": [
            {
                "type": "ranked_list",
                "title": "Most frequent staff actions",
                "items": [{"label": "paid", "value": 8}, {"label": "transferred", "value": 4}],
            },
            {
                "type": "timeline",
                "title": "Audit events for last 3 months",
                "events": [
                    {"title": "Permission posture reviewed", "severity": "high"},
                    {"title": "POS order paid", "severity": "info"},
                ],
            },
        ],
    }
    staff_history = [{"role": "assistant", "content": "Staff activity ready.", "structuredPayload": staff_payload}]
    assert "does not include a staff-member ranking" in (
        _latest_insight_follow_up_answer("Who was the most active staff member?", staff_history) or ""
    )
    assert "paid with 8 events" in (
        _latest_insight_follow_up_answer("What activity type happened the most?", staff_history) or ""
    )
    assert "Permission posture reviewed" in (
        _latest_insight_follow_up_answer("Are there any staff activity risks?", staff_history) or ""
    )
    mixed_history = [
        {"role": "assistant", "content": "Staff activity ready.", "structuredPayload": staff_payload},
        {"role": "assistant", "content": json.dumps(payload)},
        {
            "role": "assistant",
            "content": "Product comparison ready.",
            "structuredPayload": {
                "kind": "insight_response",
                "summary": "Product comparison ready for last 1 year.",
                "timeframe": {"label": "last 1 year"},
                "currency_code": "NGN",
                "widgets": [
                    {
                        "type": "comparison_table",
                        "title": "Product comparison table for last 1 year",
                        "rows": [
                            {"product": "Eva Premium Water", "sales_total": 1200, "quantity_sold": 12, "order_count": 3},
                            {"product": "Coca-Cola", "sales_total": 900, "quantity_sold": 9, "order_count": 2},
                        ],
                    }
                ],
            },
        },
        {"role": "assistant", "content": "Receiving lifecycle ready.", "structuredPayload": procurement_payload},
    ]
    assert "paid with 8 events" in (
        _latest_insight_follow_up_answer(
            "Going back to the staff activity response, what activity type happened the most?",
            mixed_history,
        )
        or ""
    )
    assert "Gberigbe Store" in (
        _latest_insight_follow_up_answer("Going back to the business analyst review, which location led revenue?", mixed_history) or ""
    )
    assert "Eva Premium Water" in (
        _latest_insight_follow_up_answer("Going back to the product comparison, which product generated more revenue?", mixed_history) or ""
    )
    assert "current: 1" in (
        _latest_insight_follow_up_answer("Going back to the receiving lifecycle, which statuses need attention?", mixed_history) or ""
    )

    repeated_parts = _latest_repeated_question_response_parts(
        "Act as my business analyst and tell me what I am not seeing for the past year",
        repeated_history,
    )
    assert repeated_parts
    assert isinstance(repeated_parts[0], DataPart)
    assert repeated_parts[0].data["kind"] == "insight_response"


def test_build_staff_activity_insight_returns_timeline_widgets() -> None:
    payload = _build_staff_activity_insight(
        {
            "event_count": 7,
            "actions": [{"key": "login", "count": 3}, {"key": "invite", "count": 2}],
            "source_services": [{"key": "users", "count": 4}, {"key": "inventory", "count": 3}],
            "daily_activity": [{"bucket": "2026-07-01", "count": 2}, {"bucket": "2026-07-02", "count": 5}],
            "recent_events": [{"occurred_at": "2026-07-02T10:00:00Z", "summary": "Staff logged in", "action": "login"}],
        }
    )

    assert payload["kind"] == "insight_response"
    assert payload["widgets"][1]["type"] == "line_chart"
    assert payload["widgets"][3]["type"] == "timeline"


def test_build_permission_security_insight_returns_risk_panel() -> None:
    payload = _build_permission_security_insight(
        {
            "event_count": 4,
            "actors": [{"key": "Support Agent", "count": 2}],
            "support_access_grants": [{"key": "grant-1", "count": 1}],
            "severities": [{"key": "warning", "count": 1}],
            "recent_events": [{"occurred_at": "2026-07-02T10:00:00Z", "summary": "Support access granted", "action": "grant"}],
        },
        support_access_only=True,
    )

    assert payload["kind"] == "insight_response"
    assert payload["widgets"][1]["type"] == "risk_panel"
    assert payload["data_sources"][0]["endpoint_or_topic"] == "get_permission_security_activity"


def test_build_realtime_dashboard_snapshot_insight_returns_operational_widgets() -> None:
    payload = _build_realtime_dashboard_snapshot_insight(
        {
            "metrics": {
                "sales_24h_amount": 12400.5,
                "sales_24h_orders": 11,
                "receiving_24h_units": 42,
                "security_events_24h": 1,
            },
            "alerts": {
                "total_attention_items": 2,
                "high_severity_24h": 1,
                "support_access_24h": 1,
            },
            "charts": {"sales_amount_by_hour": [{"bucket": "09:00", "value": 3200.0}]},
            "leaderboards": {"top_products_24h": [{"title": "Cabin Biscuit 200g", "subtitle": "Units sold", "metric_value": 12}]},
            "feed": [{"occurred_at": "2026-07-01T10:00:00Z", "summary": "Sale completed", "source_service": "pos", "severity": "info"}],
        },
        {"unread_count": 3},
    )

    assert payload["kind"] == "insight_response"
    assert payload["widgets"][0]["type"] == "metric_grid"
    assert payload["widgets"][1]["type"] == "line_chart"
    assert payload["widgets"][3]["type"] == "risk_panel"
    assert payload["data_sources"][0]["endpoint_or_topic"] == "get_realtime_dashboard_snapshot"
    assert payload["data_sources"][1]["endpoint_or_topic"] == "get_alert_summary"


def test_build_subscription_usage_insight_returns_comparison_table() -> None:
    payload = _build_subscription_usage_insight(
        {
            "subscription": {"status": "TRIAL", "plan": {"name": "Enterprise"}},
            "features": [
                {"name": "Staff users", "usage": 8, "limit_value": 10, "remaining": 2, "status": "near_limit"},
                {"name": "POS terminals", "usage": 3, "limit_value": 10, "remaining": 7, "status": "healthy"},
            ],
            "warnings": ["Trial ends soon."],
        }
    )

    assert payload["kind"] == "insight_response"
    assert payload["widgets"][1]["type"] == "comparison_table"
    assert payload["widgets"][2]["type"] == "risk_panel"
    assert payload["warnings"] == ["Trial ends soon."]


def test_inventory_procurement_named_insight_from_text_detects_priority_flows() -> None:
    assert _inventory_procurement_named_insight_from_text("Show purchase order lifecycle for the past month") == "po_lifecycle"
    assert _inventory_procurement_named_insight_from_text("Show purchase order analysis for the last three months") == "po_lifecycle"
    assert _inventory_procurement_named_insight_from_text("What purchase orders were received last month?") == "receiving_lifecycle"
    assert _inventory_procurement_named_insight_from_text("Create a purchase order") is None


@pytest.mark.asyncio
async def test_users_named_insight_payload_supports_audit_search_and_timeline() -> None:
    executor = _RecordingToolExecutor()

    search_payload = await _users_named_insight_payload(
        insight_key="audit_search",
        tool_executor=executor,
        tool_ctx=ToolContext(),
        user_text="show recent audit logs for the past month",
    )
    timeline_payload = await _users_named_insight_payload(
        insight_key="audit_timeline",
        tool_executor=executor,
        tool_ctx=ToolContext(),
        user_text="show the audit timeline for this workspace",
    )

    assert search_payload is not None
    assert timeline_payload is not None
    assert executor.calls[0][0] == "audit.search_events"
    assert executor.calls[0][1]["occurred_from"] == _resolve_insight_time_window("show recent audit logs for the past month", default_days=30, default_label="last 30 days")["start_date"]
    assert executor.calls[0][1]["occurred_to"] == _resolve_insight_time_window("show recent audit logs for the past month", default_days=30, default_label="last 30 days")["end_date"]
    assert executor.calls[0][1]["period_label"] == "last month"
    assert executor.calls[1] == (
        "audit.get_event_timeline",
        {"search": "show the audit timeline for this workspace", "limit": 100},
    )


@pytest.mark.asyncio
async def test_host_named_insight_payload_supports_users_and_product_passthrough() -> None:
    executor = _RecordingToolExecutor()

    staff_payload = await _host_named_insight_payload(
        insight_key="users::staff_activity",
        tool_executor=executor,
        tool_ctx=ToolContext(),
        user_text="show staff activity from audit events",
    )
    import_payload = await _host_named_insight_payload(
        insight_key="product_discovery::import_opportunities",
        tool_executor=executor,
        tool_ctx=ToolContext(),
        user_text="show global catalog import opportunities",
    )
    catalog_gap_payload = await _host_named_insight_payload(
        insight_key="product_discovery::catalog_gaps",
        tool_executor=executor,
        tool_ctx=ToolContext(),
        user_text="show the catalog opportunity board",
    )

    assert staff_payload is not None
    assert import_payload is not None
    assert catalog_gap_payload is not None
    assert executor.calls[0][0] == "audit.get_staff_activity"
    assert executor.calls[0][1]["date_from"] == _resolve_insight_time_window("show staff activity from audit events", default_days=30, default_label="last 30 days")["start_date"]
    assert executor.calls[0][1]["date_to"] == _resolve_insight_time_window("show staff activity from audit events", default_days=30, default_label="last 30 days")["end_date"]
    catalog_calls = [call for call in executor.calls if call[0] == "product.list_global_catalog_products"]
    assert len(catalog_calls) == 1
    assert all(call[1]["exclude_imported"] is True for call in catalog_calls)
    catalog_widget_titles = [str(widget.get("title") or "") for widget in catalog_gap_payload["widgets"]]
    assert "New catalog opportunities" not in catalog_widget_titles
    assert "Catalog opportunity board" not in catalog_widget_titles


@pytest.mark.asyncio
async def test_product_business_review_sequences_product_mcp_calls() -> None:
    executor = _RecordingToolExecutor()

    payload = await _business_review_specialist_payload(
        domain="product",
        original_request="Analyze my business performance for the last three months",
        tool_executor=executor,
        tool_ctx=ToolContext(),
    )

    assert payload["title"] == "Product catalog health"
    assert [name for name, _arguments in executor.calls] == [
        "product.get_product_dashboard_stats",
        "product.get_product_stock_alerts",
        "product.search_product_variants",
    ]


@pytest.mark.asyncio
async def test_sales_location_refresh_uses_current_search_endpoint() -> None:
    executor = _RecordingToolExecutor()

    await _refresh_sales_location_labels(
        {"groups": [{"location_id": "loc-1", "location": "Legacy location"}]},
        tool_executor=executor,
        tool_ctx=ToolContext(),
    )

    assert executor.calls == [
        (
            "inventory.search_stock_locations",
            {"query": None, "limit": 50, "structural_only": True},
        )
    ]


def test_friendly_agent_label_hides_runtime_card_identifiers() -> None:
    assert _friendly_agent_label("wa-p4-pos_admin-1d62f6c47bd0") == "Point of Sale (POS)"
    assert _friendly_agent_label("wa-p4-inventory_visibility-c6635feb24f1") == "Inventory Management"
    assert _friendly_agent_label("wa-p4-product_discovery-b7774e08b933") == "Product Management"


@pytest.mark.asyncio
async def test_host_procurement_payload_threads_calendar_windows_and_labels() -> None:
    executor = _RecordingToolExecutor()

    payload = await _host_named_insight_payload(
        insight_key="inventory_procurement::po_lifecycle",
        tool_executor=executor,
        tool_ctx=ToolContext(),
        user_text="Show purchase order analysis for the last three months.",
    )

    assert payload is not None
    window = _resolve_insight_time_window(
        "Show purchase order analysis for the last three months.",
        default_days=30,
        default_label="last 30 days",
    )
    assert payload["summary"] == f"Purchase-order lifecycle status is ready for {window['label']}."
    assert payload["widgets"][0]["title"] == f"PO pipeline for {window['label']}"
    assert executor.calls[0] == (
        "inventory.search_purchase_orders",
        {"limit": 20, "date_from": window["start_date"], "date_to": window["end_date"]},
    )


def test_generic_inventory_setup_language_prefers_onboarding_over_direct_mutation() -> None:
    assert _infer_onboarding_scope_from_text("I want to import products") == "product_onboarding"
    assert _infer_onboarding_scope_from_text("Help me setup product import") == "product_onboarding"
    assert _inventory_setup_action_from_text("I want to set up inventory") is None
    assert _inventory_setup_action_from_text("Help me setup inventory") is None
    assert _inventory_setup_action_from_text("List the stock locations in my electronics store setup.") is None
    assert _inventory_setup_action_from_text("Create a new inventory item called Main Inventory") == "create_inventory_item"


def test_host_orchestration_plan_keeps_inventory_grouping_out_of_users_domain() -> None:
    plan = _host_orchestration_plan(
        "Group the inventories into categories and assign items",
        [
            {"name": "inventory"},
            {"name": "users"},
        ],
    )

    assert plan == ["inventory"]


def test_infer_domain_agent_name_prefers_pos_for_sales_by_location_queries() -> None:
    assert _infer_domain_agent_name("Show sales by location today") == "pos"
    assert _infer_domain_agent_name("Break down today's sales by location") == "pos"
    assert _infer_domain_agent_name("Show top sellers in seven days") == "pos"


def test_host_orchestration_plan_does_not_append_inventory_for_pos_insight_query() -> None:
    plan = _host_orchestration_plan(
        "Show sales by location today",
        [
            {"name": "inventory"},
            {"name": "pos"},
            {"name": "users"},
        ],
    )

    assert plan == ["pos"]


def test_host_orchestration_plan_builds_cross_domain_business_review_flow() -> None:
    plan = _host_orchestration_plan(
        "Analyze my business performance for the last quarter",
        [
            {"name": "inventory"},
            {"name": "pos"},
            {"name": "users"},
        ],
    )

    assert plan == ["pos", "inventory", "users"]


def test_delegated_business_review_is_intercepted_by_each_domain_before_llm_fallback() -> None:
    request = (
        "Continue the user's multi-domain business review.\n"
        "Original user request: Analyze my business performance for the last three months\n"
        "Time range to use: last 3 months (2026-05-28 to 2026-08-28).\n"
        "Run the full Point of Sale (POS) portion of the business review for that same time range."
    )

    assert _business_review_specialist_domain("pos_admin", request) == (
        "pos",
        "Analyze my business performance for the last three months",
    )
    assert _business_review_specialist_domain("inventory_visibility", request) == (
        "inventory",
        "Analyze my business performance for the last three months",
    )
    assert _business_review_specialist_domain("users", request) == (
        "users",
        "Analyze my business performance for the last three months",
    )
    assert _business_review_specialist_domain("product_discovery", request) == (
        "product",
        "Analyze my business performance for the last three months",
    )
    assert _business_review_specialist_domain("pos", request) is None
    assert _business_review_specialist_domain("host", request) is None
    assert _business_review_specialist_domain("pos", "Show me sales for last week") is None


def test_business_review_router_targets_concrete_specialists_even_while_the_directory_is_warming() -> None:
    request = (
        "Continue the user's multi-domain business review.\n"
        "Original user request: Analyze my business performance for the last three months"
    )

    assert _select_router_handoff_agent("pos", request, [{"name": "inventory_visibility", "skills": []}]) == "pos_admin"
    assert _select_router_handoff_agent("inventory", request, [{"name": "pos_admin", "skills": []}]) == "inventory_visibility"
    assert _select_router_handoff_agent("product", request, [{"name": "pos_admin", "skills": []}]) == "product_discovery"


def test_strong_domain_override_does_not_force_pos_for_cross_domain_business_review() -> None:
    assert (
        _strong_domain_agent_override(
            "Analyze my business sales and inventory performance for the last quarter"
        )
        is None
    )


@pytest.mark.asyncio
async def test_host_auto_completes_cross_domain_business_review_without_continue_prompt() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    request = "Analyze my business performance for the last quarter"
    task = Task(
        id="task-host-business-review",
        context_id="ctx-host-business-review",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=request)]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    specialist_result_names = {
        event.name
        for event in events
        if isinstance(event, Artifact) and isinstance(event.name, str) and event.name.endswith(".result")
    }
    assert specialist_result_names >= {"pos.result", "inventory.result", "users.result", "product.result"}

    assert fake_langgraph_components.FAKE_TOOL_CALLS[:5] == [
        ("list_available_agents", {}),
        (
            "delegate_to_agent",
            {
                "request": (
                    "Continue the user's multi-domain business review.\n"
                    "Original user request: Analyze my business performance for the last quarter\n"
                    "Time range to use: last quarter (2026-04-01 to 2026-06-30).\n"
                    "Run the full Point of Sale (POS) portion of the business review for that same time range.\n"
                    "Use sensible defaults and do not ask the user for a menu selection unless access is genuinely blocked."
                ),
                "agent_name": "pos",
            },
        ),
        (
            "delegate_to_agent",
            {
                "request": (
                    "Continue the user's multi-domain business review.\n"
                    "Original user request: Analyze my business performance for the last quarter\n"
                    "Completed steps so far: Point of Sale (POS).\n"
                    "Latest completed step result: Sales performance for the last quarter is ready. Revenue was strongest in the final month and repeat purchasing improved.\n"
                    "Time range to use: last quarter (2026-04-01 to 2026-06-30).\n"
                    "Run the full Inventory Management portion of the business review for that same time range.\n"
                    "Cover all of these in one response: stock posture and availability; turnover and velocity; reorder recommendations; ageing and expiry analysis; valuation and carrying cost; fulfillment and reservation issues; and procurement or receiving signals that affect stock health.\n"
                    "Use defaults without asking the user to choose a single focus: include all locations, provide both company-wide and per-location outputs, use POS sales from earlier steps as the demand signal where helpful, and only include lot or expiry-aware analysis if the workspace tracks it.\n"
                    "Do not send a focus picker, a default-confirmation checklist, or any retry prompt unless required permissions or source data are truly unavailable."
                ),
                "agent_name": "inventory",
            },
        ),
        (
            "delegate_to_agent",
            {
                "request": (
                    "Continue the user's multi-domain business review.\n"
                    "Original user request: Analyze my business performance for the last quarter\n"
                    "Completed steps so far: Point of Sale (POS), Inventory Management.\n"
                    "Latest completed step result: Inventory health for the requested review period is ready. Stock posture was stable overall, slow movers tied up capital, several reorder candidates emerged, and fulfillment delays were concentrated in a small set of locations.\n"
                    "Time range to use: last quarter (2026-04-01 to 2026-06-30).\n"
                    "Run the full Users and Workspace Controls portion of the business review for that same time range.\n"
                    "Cover staff activity, audit anomalies, role or permission risks, and subscription or capacity pressure that could affect operations.\n"
                    "Use sensible defaults and do not ask the user to choose a sub-focus unless access is genuinely blocked."
                ),
                "agent_name": "users",
            },
        ),
        (
            "delegate_to_agent",
            {
                    "request": (
                        "Continue the user's multi-domain business review.\n"
                        "Original user request: Analyze my business performance for the last quarter\n"
                        "Completed steps so far: Point of Sale (POS), Inventory Management, User and Workspace Management.\n"
                        "Latest completed step result: Workspace controls for the requested review period are ready. Staff activity was concentrated in a few operators, audit activity stayed normal overall, and there was no immediate subscription-capacity pressure.\n"
                        "Time range to use: last quarter (2026-04-01 to 2026-06-30).\n"
                        "Run the full Product Management portion of the business review for that same time range.\n"
                        "Cover catalog health, assortment gaps, duplicate-code risks, media or merchandising weaknesses, and global catalog import opportunities that could strengthen current demand coverage.\n"
                        "Use sensible defaults and do not ask the user to choose a sub-focus unless access is genuinely blocked."
                ),
                "agent_name": "product",
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    result_payload = result_artifact.parts[0].data
    assert result_payload["kind"] == "insight_response"
    assert result_payload["summary"] == "Business review is ready for last quarter."
    assert result_payload["explanation"] == "I completed the business review for: Analyze my business performance for the last quarter"
    widgets = result_payload["widgets"]
    assert widgets[0]["type"] == "metric_grid"
    assert widgets[1]["type"] == "section_stack"
    section_titles = [section["title"] for section in widgets[1]["sections"]]
    assert "Point of Sale (POS)" in section_titles
    assert "Inventory Management" in section_titles
    assert "User and Workspace Management" in section_titles
    assert "Product Management" in section_titles

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.completed


@pytest.mark.asyncio
async def test_host_cross_domain_business_review_requires_time_range_before_delegation() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    request = "Analyze my business data"
    task = Task(
        id="task-host-business-review-needs-range",
        context_id="ctx-host-business-review-needs-range",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=request)]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == (
        "What time range should I use for the business review: last 7 days, "
        "last month, last quarter, or last year?"
    )

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


def test_delegation_status_uses_public_specialist_name() -> None:
    assert (
        _format_delegation_status_text(
            agent_name="wa-p4-product_catalog_admin-1dd459404fe4",
            state=TaskState.submitted,
            message=None,
        )
        == "The product catalog specialist has accepted the task."
    )


@pytest.mark.asyncio
async def test_host_merges_time_range_follow_up_into_sales_analysis_request() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    original_request = "Can you analyze my sales data?"
    follow_up_answer = "past one year"
    task = Task(
        id="task-host-sales-follow-up",
        context_id="ctx-host-sales-follow-up",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=follow_up_answer)]),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text=original_request)]),
            Message(role=Role.agent, parts=[TextPart(text="What time range should I use for the sales analysis?")]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=follow_up_answer)])

    events = [event async for event in processor(task, message, None, None)]

    assert _latest_host_clarification_merge(follow_up_answer, task.history) == (
        "Analyze my sales data for the past one year"
    )
    assert fake_langgraph_components.FAKE_TOOL_CALLS
    assert fake_langgraph_components.FAKE_TOOL_CALLS[0] == (
        "pos.get_sales_summary",
        {"days": 365, "date": datetime.now(timezone.utc).date().isoformat(), "group_by": "location"},
    )
    assert any(
        name == "pos.get_sales_summary" and args.get("days") == 365
        for name, args in fake_langgraph_components.FAKE_TOOL_CALLS
    )

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts)
    assert "What time range should I use for the sales analysis?" not in _text_from_parts(result_artifact.parts)

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state != TaskState.input_required


@pytest.mark.asyncio
async def test_host_direct_sales_analysis_request_requires_time_range_before_delegation() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    request = "Can you analyze my sales data?"
    task = Task(
        id="task-host-sales-needs-range",
        context_id="ctx-host-sales-needs-range",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=request)]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    assert not any(name == "pos.get_sales_summary" for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS)

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == "What time range should I use for the sales analysis?"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_host_sales_follow_up_merges_from_saved_clarification_workflow_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_CONTEXT_MEMORY_STORE", "memory")

    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    original_request = "Can you analyze my sales data?"

    first_task = Task(
        id="task-host-sales-follow-up-state-first",
        context_id="ctx-host-sales-follow-up-state",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=original_request)]),
        ),
    )
    first_message = Message(role=Role.user, parts=[TextPart(text=original_request)])

    first_events = [event async for event in processor(first_task, first_message, None, None)]

    first_result = next(event for event in first_events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(first_result.parts) == "What time range should I use for the sales analysis?"
    assert not any(name == "pos.get_sales_summary" for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS)

    fake_langgraph_components.reset_fake_components()

    follow_up_answer = "for the last one year or so"
    second_task = Task(
        id="task-host-sales-follow-up-state-second",
        context_id="ctx-host-sales-follow-up-state",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=follow_up_answer)]),
        ),
    )
    second_message = Message(role=Role.user, parts=[TextPart(text=follow_up_answer)])

    second_events = [event async for event in processor(second_task, second_message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS
    assert fake_langgraph_components.FAKE_TOOL_CALLS[0] == (
        "pos.get_sales_summary",
        {"days": 365, "date": datetime.now(timezone.utc).date().isoformat(), "group_by": "location"},
    )
    assert any(
        name == "pos.get_sales_summary" and args.get("days") == 365
        for name, args in fake_langgraph_components.FAKE_TOOL_CALLS
    )

    second_result = next(event for event in second_events if isinstance(event, Artifact) and event.name == "result")
    assert "What time range should I use for the sales analysis?" not in _text_from_parts(second_result.parts)

    second_status_events = [event for event in second_events if isinstance(event, TaskStatus)]
    assert second_status_events[-1].state != TaskState.input_required


@pytest.mark.asyncio
async def test_host_merges_business_review_follow_up_from_task_history() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    original_request = "Can you help me analyze my business performance?"
    follow_up_answer = "last quarter"
    task = Task(
        id="task-host-business-review-follow-up",
        context_id="ctx-host-business-review-follow-up",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=follow_up_answer)]),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text=original_request)]),
            Message(
                role=Role.agent,
                parts=[
                    TextPart(
                        text="What time range should I use for the business review: last 7 days, last month, last quarter, or last year?"
                    )
                ],
            ),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=follow_up_answer)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS[:5] == [
        ("list_available_agents", {}),
        (
            "delegate_to_agent",
            {
                "request": (
                    "Continue the user's multi-domain business review.\n"
                    "Original user request: Analyze my business performance for the last quarter\n"
                    "Time range to use: last quarter (2026-04-01 to 2026-06-30).\n"
                    "Run the full Point of Sale (POS) portion of the business review for that same time range.\n"
                    "Use sensible defaults and do not ask the user for a menu selection unless access is genuinely blocked."
                ),
                "agent_name": "pos",
            },
        ),
        (
            "delegate_to_agent",
            {
                "request": (
                    "Continue the user's multi-domain business review.\n"
                    "Original user request: Analyze my business performance for the last quarter\n"
                    "Completed steps so far: Point of Sale (POS).\n"
                    "Latest completed step result: Sales performance for the last quarter is ready. Revenue was strongest in the final month and repeat purchasing improved.\n"
                    "Time range to use: last quarter (2026-04-01 to 2026-06-30).\n"
                    "Run the full Inventory Management portion of the business review for that same time range.\n"
                    "Cover all of these in one response: stock posture and availability; turnover and velocity; reorder recommendations; ageing and expiry analysis; valuation and carrying cost; fulfillment and reservation issues; and procurement or receiving signals that affect stock health.\n"
                    "Use defaults without asking the user to choose a single focus: include all locations, provide both company-wide and per-location outputs, use POS sales from earlier steps as the demand signal where helpful, and only include lot or expiry-aware analysis if the workspace tracks it.\n"
                    "Do not send a focus picker, a default-confirmation checklist, or any retry prompt unless required permissions or source data are truly unavailable."
                ),
                "agent_name": "inventory",
            },
        ),
        (
            "delegate_to_agent",
            {
                "request": (
                    "Continue the user's multi-domain business review.\n"
                    "Original user request: Analyze my business performance for the last quarter\n"
                    "Completed steps so far: Point of Sale (POS), Inventory Management.\n"
                    "Latest completed step result: Inventory health for the requested review period is ready. Stock posture was stable overall, slow movers tied up capital, several reorder candidates emerged, and fulfillment delays were concentrated in a small set of locations.\n"
                    "Time range to use: last quarter (2026-04-01 to 2026-06-30).\n"
                    "Run the full Users and Workspace Controls portion of the business review for that same time range.\n"
                    "Cover staff activity, audit anomalies, role or permission risks, and subscription or capacity pressure that could affect operations.\n"
                    "Use sensible defaults and do not ask the user to choose a sub-focus unless access is genuinely blocked."
                ),
                "agent_name": "users",
            },
        ),
        (
            "delegate_to_agent",
            {
                "request": (
                    "Continue the user's multi-domain business review.\n"
                    "Original user request: Analyze my business performance for the last quarter\n"
                    "Completed steps so far: Point of Sale (POS), Inventory Management, User and Workspace Management.\n"
                    "Latest completed step result: Workspace controls for the requested review period are ready. Staff activity was concentrated in a few operators, audit activity stayed normal overall, and there was no immediate subscription-capacity pressure.\n"
                    "Time range to use: last quarter (2026-04-01 to 2026-06-30).\n"
                    "Run the full Product Management portion of the business review for that same time range.\n"
                    "Cover catalog health, assortment gaps, duplicate-code risks, media or merchandising weaknesses, and global catalog import opportunities that could strengthen current demand coverage.\n"
                    "Use sensible defaults and do not ask the user to choose a sub-focus unless access is genuinely blocked."
                ),
                "agent_name": "product",
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    result_payload = result_artifact.parts[0].data
    assert result_payload["kind"] == "insight_response"
    assert result_payload["summary"] == "Business review is ready for last quarter."
    assert result_payload["explanation"] == "I completed the business review for: Analyze my business performance for the last quarter"
    section_titles = [section["title"] for section in result_payload["widgets"][1]["sections"]]
    assert "Inventory Management" in section_titles
    assert "Point of Sale (POS)" in section_titles
    assert "User and Workspace Management" in section_titles
    assert "Product Management" in section_titles

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.completed


def test_coerce_delegated_response_treats_plain_text_confirmation_as_input_required() -> None:
    delegated_response = _coerce_delegated_response(
        {
            "selected_agent": "inventory",
            "delegated_task_id": "delegated-inventory-categorize",
            "response_text": (
                "I reviewed the inventory items and prepared a category plan. "
                "Once you confirm, I'll create the categories and assign the items."
            ),
            "result_parts": [
                {
                    "kind": "text",
                    "text": (
                        "I reviewed the inventory items and prepared a category plan. "
                        "Once you confirm, I'll create the categories and assign the items."
                    ),
                }
            ],
            "status_updates": [
                {"state": "submitted", "message": "delegated task submitted", "final": False},
                {"state": "completed", "message": "waiting for confirmation", "final": True},
            ],
        }
    )

    assert delegated_response is not None
    assert delegated_response["delegated_final_state"] == TaskState.input_required


def test_render_tool_prompt_block_includes_relation_lookup_rules() -> None:
    prompt = _render_tool_prompt_block(
        [
            ToolSpec(
                name="inventory.list_inventory_categories",
                description="List categories.",
                input_schema={"type": "object", "properties": {}, "required": []},
            ),
            ToolSpec(
                name="inventory.create_inventory_item",
                description="Create inventory item.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "payload": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "category_id": {
                                    "type": "string",
                                    "description": "UUID of InventoryCategory",
                                },
                            },
                            "required": ["name"],
                        }
                    },
                    "required": ["payload"],
                },
            ),
        ]
    )

    assert "Never ask the user to manually type backend IDs or UUIDs for relational fields." in prompt
    assert "prefer list/get-all tools over search tools whenever both are available." in prompt
    assert "Do not tell the user the backend requires those parameters." in prompt
    assert "omit optional filters/null values" in prompt
    assert "Do not mutate records until the required fields are known" in prompt
    assert "gather-and-confirm flow" in prompt
    assert "`inventory.create_inventory_item.payload.category_id`" in prompt
    assert "`inventory.list_inventory_categories`" in prompt


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


def test_build_product_operation_supports_nested_payload_schema() -> None:
    operation = _build_product_operation(
        tool_specs=[
            ToolSpec(
                name="product.create_product",
                description="Create a product.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "payload": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "category_name": {"type": "string"},
                                "pos_ready": {"type": "boolean"},
                            },
                            "required": ["name"],
                        }
                    },
                    "required": ["payload"],
                },
            )
        ],
        company_context=None,
        product_name="Women's Cotton T-Shirt",
        product_category_id=None,
        product_category="Women's Wear",
        pos_ready=True,
    )

    assert operation["arguments"] == {
        "payload": {
            "name": "Women's Cotton T-Shirt",
            "category_name": "Women's Wear",
            "pos_ready": True,
        }
    }
    assert operation["missing_required"] == []


def test_build_inventory_operation_prefers_relation_ids_in_nested_payload_schema() -> None:
    operation = _build_inventory_operation(
        tool_specs=[
            ToolSpec(
                name="inventory.create_inventory_item",
                description="Create inventory item.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "payload": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "stock_location_id": {"type": "string"},
                                "category_id": {"type": "string"},
                            },
                            "required": ["name"],
                        }
                    },
                    "required": ["payload"],
                },
            )
        ],
        company_context=None,
        inventory_name="Main Inventory",
        inventory_description="Primary sellable stock ledger",
        related_location_id="loc-1",
        related_location_name="Main Warehouse",
        category_id="cat-1",
        category_name="Men's Clothes",
    )

    assert operation["arguments"] == {
        "payload": {
            "name": "Main Inventory",
            "description": "Primary sellable stock ledger",
            "stock_location_id": "loc-1",
            "category_id": "cat-1",
        }
    }
    assert operation["missing_required"] == []


def test_build_stock_location_operation_supports_nested_payload_schema_and_parent_ref() -> None:
    operation = _build_stock_location_operation(
        tool_specs=[
            ToolSpec(
                name="inventory.create_stock_location",
                description="Create stock location.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "payload": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "location_type_name": {"type": "string"},
                                "structural": {"type": "boolean"},
                                "parent_id": {"type": "string"},
                            },
                            "required": ["name"],
                        }
                    },
                    "required": ["payload"],
                },
            )
        ],
        company_context=None,
        location_name="Returns Shelf",
        location_type="shelf",
        primary=False,
        structural=False,
        parent_location_ref=_created_result_ref("inventory.create_stock_location:main-warehouse", "location", "id"),
    )

    assert operation["arguments"] == {
        "payload": {
            "name": "Returns Shelf",
            "location_type_name": "shelf",
            "structural": False,
            "parent_id": {
                "__ka2a_created_ref__": {
                    "semantic_key": "inventory.create_stock_location:main-warehouse",
                    "path": ["location", "id"],
                }
            },
        }
    }
    assert operation["missing_required"] == []


def test_build_stock_location_operation_supports_ref_payload_schema() -> None:
    operation = _build_stock_location_operation(
        tool_specs=[
            ToolSpec(
                name="inventory.create_stock_location",
                description="Create stock location.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "payload": {"$ref": "#/$defs/StockLocationPayload"},
                    },
                    "required": ["payload"],
                    "$defs": {
                        "StockLocationPayload": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "location_type_name": {"type": "string"},
                                "structural": {"type": "boolean"},
                            },
                            "required": ["name"],
                        }
                    },
                },
            )
        ],
        company_context=None,
        location_name="Main Electronics Warehouse",
        location_type="warehouse",
        primary=True,
        structural=True,
    )

    assert operation["arguments"] == {
        "payload": {
            "name": "Main Electronics Warehouse",
            "location_type_name": "warehouse",
            "structural": True,
        }
    }
    assert operation["missing_required"] == []


def test_extract_created_result_value_supports_mcp_wrapped_result_payload() -> None:
    payload = {
        "content": [
            {
                "type": "text",
                "text": '{"profile_id":1,"location":{"id":"loc-123","name":"Main Electronics Warehouse"}}',
            }
        ],
        "isError": False,
    }

    assert _extract_created_result_value(payload, ["location", "id"]) == "loc-123"


def test_select_router_delegation_agent_prefers_best_matching_subspecialist() -> None:
    selected = _select_router_delegation_agent(
        "i want you to create inventory for me",
        [
            {
                "name": "inventory_visibility",
                "description": "Focused inventory specialist for stock posture, alerts, reservations, and warehouse visibility.",
                "skills": [
                    {
                        "name": "Inventory Visibility",
                        "description": "Search inventories and inspect stock posture.",
                        "tags": ["inventory", "stock", "warehouse"],
                        "examples": ["Show low-stock inventories."],
                    }
                ],
            },
            {
                "name": "inventory_setup",
                "description": "Focused inventory specialist for stock-location, inventory-category, and inventory-item setup and maintenance workflows.",
                "skills": [
                    {
                        "name": "Inventory Setup Admin",
                        "description": "Create and update stock locations, inventory categories, and inventory items.",
                        "tags": ["inventory", "setup", "create", "categories"],
                        "examples": ["Create the main inventory item for onboarding."],
                    }
                ],
            },
        ],
    )

    assert selected == "inventory_setup"


def test_select_router_handoff_agent_does_not_delegate_back_to_host_for_cross_domain_request() -> None:
    selected = _select_router_handoff_agent(
        "product",
        "please help me set up my inventory",
        [
            {
                "name": "host",
                "description": "Workspace host agent.",
                "skills": [],
            },
            {
                "name": "product_discovery",
                "description": "Focused product specialist for catalog search.",
                "skills": [],
            },
            {
                "name": "product_catalog_admin",
                "description": "Focused product specialist for create and update work.",
                "skills": [],
            },
        ],
    )

    assert selected is None


def test_select_router_handoff_agent_prefers_own_marketplace_specialist_for_mixed_inventory_prompt() -> None:
    selected = _select_router_handoff_agent(
        "product",
        "can you help me search for latest adidas shoes online, i want to buy shoes and start my inventory with them",
        [
            {
                "name": "host",
                "description": "Workspace host agent.",
                "skills": [],
            },
            {
                "name": "marketplace_sourcing",
                "description": "Focused product specialist for online supplier and marketplace search.",
                "skills": [],
            },
            {
                "name": "inventory",
                "description": "Workspace inventory agent.",
                "skills": [],
            },
        ],
    )

    assert selected == "marketplace_sourcing"


def test_select_router_handoff_agent_prefers_inventory_visibility_for_location_read_query() -> None:
    selected = _select_router_handoff_agent(
        "inventory",
        "List the stock locations in my electronics store setup.",
        [
            {
                "name": "inventory_visibility",
                "description": "Focused inventory specialist for stock posture, alerts, reservations, and warehouse visibility.",
                "skills": [],
            },
            {
                "name": "inventory_setup",
                "description": "Focused inventory specialist for stock-location and inventory setup workflows.",
                "skills": [],
            },
            {
                "name": "inventory_procurement",
                "description": "Focused inventory specialist for procurement workflows.",
                "skills": [],
            },
        ],
    )

    assert selected == "inventory_visibility"


def test_classify_failed_operation_reports_tls_discovery_failure() -> None:
    classified = _classify_failed_operation(
        {
            "label": "stock location 'Main Warehouse'",
            "tool_name": "inventory.create_stock_location",
            "reason": "tool_unavailable",
            "discovery_failures": [
                {
                    "server_id": "inventory",
                    "error": "httpx.ConnectError: [SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error (_ssl.c:1032)",
                }
            ],
        }
    )

    assert classified["error_kind"] == "tls"
    assert classified["retryable"] is True
    assert classified["error_summary"] == "inventory: TLS handshake failed while connecting to the upstream service."


def test_onboarding_operation_summary_prioritizes_blocking_issues() -> None:
    summary = _onboarding_operation_summary(
        created_operations={},
        failed_operations=[
            {
                "label": "stock location 'Main Warehouse'",
                "tool_name": "inventory.create_stock_location",
                "reason": "tool_unavailable",
                "discovery_failures": [
                    {
                        "server_id": "inventory",
                        "error": "httpx.ConnectError: [SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error (_ssl.c:1032)",
                    }
                ],
            },
            {
                "label": "inventory category 'Beverages'",
                "tool_name": "inventory.create_inventory_category",
                "reason": "tool_unavailable",
                "discovery_failures": [
                    {
                        "server_id": "inventory",
                        "error": "httpx.ConnectError: [SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error (_ssl.c:1032)",
                    }
                ],
            },
            {
                "label": "product 'Soda'",
                "tool_name": "product.create_product",
                "reason": "missing_required_arguments",
                "missing": ["payload"],
            },
            {
                "label": "delegated onboarding submission",
                "reason": "tool_error",
                "error": "Requested agent 'inventory' is not registered.",
            },
        ],
    )

    assert "Blocking issues: inventory: TLS handshake failed while connecting to the upstream service." in summary
    assert "The tool schema requires additional fields: payload." in summary
    assert "The required specialist agent is not currently visible in the registry." in summary
    assert "Still pending: 4 onboarding steps are blocked." in summary


def test_normalize_tool_call_payload_supports_legacy_tool_code_wrapper() -> None:
    payload = {
        "tool_code": "print(delegate_to_agent(agent_name='users', user_query='How many staff members do we have in total?'))"
    }

    normalized = _normalize_tool_call_payload(payload, tool_names={"delegate_to_agent"})

    assert normalized == {
        "kind": "tool-call",
        "name": "delegate_to_agent",
        "arguments": {
            "agent_name": "users",
            "user_query": "How many staff members do we have in total?",
        },
    }


def test_interaction_payload_from_text_detects_legacy_tool_code_in_json_code_block() -> None:
    text = """
Certainly! Please choose one:

```json
{
  "tool_code": "print(create_multiple_choice(title='Make a selection', description='Pick an area.', options=[{'value':'users','label':'Users'}], multiple=False, allow_input=False))"
}
```
""".strip()

    payload = _interaction_payload_from_text(text)

    assert payload == {
        "interaction_type": "legacy_tool_code",
        "tool_code": "print(create_multiple_choice(title='Make a selection', description='Pick an area.', options=[{'value':'users','label':'Users'}], multiple=False, allow_input=False))",
    }


def test_host_introspection_detection_preserves_domain_requests() -> None:
    assert _is_host_introspection_query("how many agents do you have?")
    assert _is_host_introspection_query("tell me the agents that you have currently that are register")
    assert _is_host_introspection_query("list the specialist agents you can route to")
    assert _is_host_introspection_query("is inventory fulfillment available to you?")
    assert not _is_host_introspection_query("hi there!")
    assert not _is_host_introspection_query("what can u do for me")
    assert not _is_host_introspection_query("who are you")
    assert not _is_host_introspection_query("help me search for the product t-shirt")


def test_host_capability_picker_detection() -> None:
    assert _is_host_capability_picker_query("what can you do for me?")
    assert _is_host_capability_picker_query("what can u do for me")
    assert _is_host_capability_picker_query("send the list of what you can do so I can choose")
    assert not _is_host_capability_picker_query("help me check stock levels")


def test_select_host_delegation_agent_prefers_best_matching_specialist() -> None:
    agents = [
        {
            "name": "onboarding",
            "description": "Onboarding workflow specialist.",
            "skills": [{"name": "Product Import", "description": "Guide setup", "tags": ["onboarding", "setup"]}],
        },
        {
            "name": "product",
            "description": "Product catalog specialist.",
            "skills": [{"name": "Product Search", "description": "Search products", "tags": ["product"]}],
        },
        {
            "name": "inventory",
            "description": "Inventory stock specialist.",
            "skills": [{"name": "Inventory Lookup", "description": "Check stock", "tags": ["inventory"]}],
        },
        {
            "name": "pos",
            "description": "Point of sale specialist.",
            "skills": [{"name": "POS Operations", "description": "Inspect sessions and orders", "tags": ["pos", "session"]}],
        },
    ]

    assert _select_host_delegation_agent("help me set up my inventory workspace from scratch", agents) == "onboarding"
    assert _select_host_delegation_agent("search for a t-shirt product", agents) == "product"
    assert _select_host_delegation_agent(
        "can you help me search for latest adidas shoes online, i want to buy shoes and start my inventory with them",
        agents,
    ) == "product"
    assert _select_host_delegation_agent("can you check shoes on chinese websites", agents) == "product"
    assert _select_host_delegation_agent("check stock alerts for the warehouse", agents) == "inventory"
    assert _select_host_delegation_agent("show my open cashier session", agents) == "pos"


def test_select_host_delegation_agent_forces_onboarding_for_explicit_product_import() -> None:
    agents = [
        {
            "name": "product",
            "description": "Product catalog specialist.",
            "skills": [{"name": "Product Search", "description": "Search products", "tags": ["product"]}],
        },
        {
            "name": "inventory",
            "description": "Inventory stock specialist.",
            "skills": [{"name": "Inventory Lookup", "description": "Check stock", "tags": ["inventory"]}],
        },
    ]

    assert (
        _select_host_delegation_agent(
            "I need to import products into my inventory from the global catalog",
            agents,
        )
        == "onboarding"
    )


@pytest.mark.asyncio
async def test_host_auto_delegates_and_waits_for_specialist_result() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="help me search for the product t-shirt")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="help me search for the product t-shirt")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        ("delegate_to_agent", {"request": "help me search for the product t-shirt", "agent_name": "product"}),
    ]

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert len(status_events) == 4
    assert status_events[0].state == TaskState.working
    assert _text_from_parts(status_events[0].message.parts) == "Delegating this request to the product specialist agent."
    assert _text_from_parts(status_events[1].message.parts) == "product management specialist: delegated task submitted"
    assert _text_from_parts(status_events[2].message.parts) == "product management specialist: searching catalog"
    assert status_events[-1].state == TaskState.completed
    assert _text_from_parts(status_events[-1].message.parts) == "Found 3 products matching t-shirt."

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    delegation_payload = delegation_artifact.parts[0].data
    assert delegation_payload["selectedAgent"] == "product"
    assert delegation_payload["delegatedTaskId"] == "delegated-1"
    assert delegation_payload["finalState"] == "completed"
    assert len(delegation_payload["statusUpdates"]) == 3

    child_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "product.matches")
    assert child_artifact.parts[0].data["count"] == 3

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == "Found 3 products matching t-shirt."


@pytest.mark.asyncio
async def test_marketplace_sourcing_executes_direct_search_without_delegation() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="marketplace_sourcing")
    request = "can you help me search for latest adidas shoes online, i want to buy shoes and start my inventory with them"
    task = Task(
        id="task-marketplace-search",
        context_id="ctx-marketplace-search",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=request)]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("search_marketplace_products", {"query": "latest adidas shoes", "max_results": 10}),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["interaction_type"] == "marketplace_results"
    assert payload["query"] == "latest adidas shoes"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[0].state == TaskState.working
    assert _text_from_parts(status_events[0].message.parts) == "Searching online marketplaces for matching products."
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_marketplace_sourcing_executes_direct_search_for_chinese_websites_query() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="marketplace_sourcing")
    request = "can you check shoes on chinese websites"
    task = Task(
        id="task-marketplace-china",
        context_id="ctx-marketplace-china",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=request)]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "search_marketplace_products",
            {
                "query": "shoes",
                "max_results": 10,
                "marketplaces": ["alibaba", "aliexpress", "temu", "dhgate"],
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["interaction_type"] == "marketplace_results"
    assert payload["query"] == "shoes"


@pytest.mark.asyncio
async def test_marketplace_sourcing_compares_selected_results_directly() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="marketplace_sourcing")
    prior_payload = {
        "interaction_type": "marketplace_results",
        "title": "Marketplace results for “latest adidas shoes”",
        "description": "Found 3 marketplace matches.",
        "query": "latest adidas shoes",
        "products": [
            {"id": "adidas-1", "title": "Adidas Ultraboost Light", "marketplace": "Amazon", "price": "USD 129.99"},
            {"id": "adidas-2", "title": "Adidas Adizero SL 2", "marketplace": "eBay", "price": "USD 119.00"},
        ],
    }
    response_text = json.dumps(
        {
            "type": "marketplace_results_response",
            "action": "compare_selected",
            "query": "latest adidas shoes",
            "selected_items": prior_payload["products"],
        }
    )
    task = Task(
        id="task-marketplace-compare",
        context_id="ctx-marketplace-compare",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=response_text)]),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="search online for adidas shoes")]),
            Message(role=Role.agent, parts=[DataPart(data=prior_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=response_text)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "compare_marketplace_products",
            {
                "items": prior_payload["products"],
                "title": "Compare offers for latest adidas shoes",
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["interaction_type"] == "comparison_view"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_host_capability_query_uses_multiple_choice_tool() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-capability",
        context_id="ctx-capability",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text="what can you do for me? I need to choose from a list")],
            ),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="what can you do for me? I need to choose from a list")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "create_multiple_choice",
            {
                "title": "Choose What You Need Help With",
                "description": "Select the area you want help with. I can continue from your choice.",
                "options": [
                    {"value": "onboarding", "label": "Product Import"},
                    {"value": "product", "label": "Product Management"},
                    {"value": "inventory", "label": "Inventory Management"},
                    {"value": "pos", "label": "Point of Sale (POS)"},
                    {"value": "users", "label": "User and Workspace Management"},
                    {"value": "general", "label": "General Question"},
                ],
                "multiple": False,
                "allow_input": True,
            },
        )
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_host_capability_selection_routes_to_selected_agent() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Choose What You Need Help With",
        "description": "Select the area you want help with. I can continue from your choice.",
        "options": [
            {"value": "onboarding", "label": "Product Import"},
            {"value": "product", "label": "Product Management"},
            {"value": "inventory", "label": "Inventory Management"},
            {"value": "pos", "label": "Point of Sale (POS)"},
            {"value": "users", "label": "User and Workspace Management"},
            {"value": "general", "label": "General Question"},
        ],
        "multiple": False,
        "allow_input": True,
    }
    task = Task(
        id="task-capability-response",
        context_id="ctx-capability-response",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"users","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="hello, what can you do for me")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"users","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "delegate_to_agent",
            {
                "request": "The user selected User and Workspace Management from the host menu. Briefly explain what kinds of tasks you can help with in this domain, using a concise user-facing summary.",
                "agent_name": "users",
            },
        ),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    delegation_payload = delegation_artifact.parts[0].data
    assert delegation_payload["selectedAgent"] == "users"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == (
        "I can help with staff lookup, invitations, roles, groups, permissions, and workspace access."
    )


@pytest.mark.asyncio
async def test_host_capability_selection_routes_onboarding_to_guided_flow_request() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Choose What You Need Help With",
        "description": "Select the area you want help with. I can continue from your choice.",
        "options": [
            {"value": "onboarding", "label": "Product Import"},
            {"value": "product", "label": "Product Management"},
            {"value": "inventory", "label": "Inventory Management"},
            {"value": "pos", "label": "Point of Sale (POS)"},
            {"value": "users", "label": "User and Workspace Management"},
            {"value": "general", "label": "General Question"},
        ],
        "multiple": False,
        "allow_input": True,
    }
    task = Task(
        id="task-capability-onboarding",
        context_id="ctx-capability-onboarding",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"onboarding","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="hello, what can you do for me")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"onboarding","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "delegate_to_agent",
            {
                "request": (
                    "Start a guided product import flow. Ask the user which product categories or brands they want to import first, "
                    "then browse global catalog products in pages, keep already-imported products filtered out, and collect selection step by step using structured interactions."
                ),
                "agent_name": "onboarding",
            },
        ),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "onboarding"
    assert delegation_artifact.parts[0].data["finalState"] == "input-required"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"
    assert result_artifact.parts[0].data["workflow_stage"] == "catalog_scope_prompt"
    assert result_artifact.parts[0].data["delegated_agent"] == "onboarding"
    assert result_artifact.parts[0].data["workflow"] == "product_import"
    assert result_artifact.parts[0].data["delegated_task_id"] == "delegated-onboarding-product-import"


@pytest.mark.asyncio
async def test_host_capability_selection_inventory_opens_domain_area_picker() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Choose What You Need Help With",
        "description": "Select the area you want help with. I can continue from your choice.",
        "options": [
            {"value": "onboarding", "label": "Product Import"},
            {"value": "product", "label": "Product Management"},
            {"value": "inventory", "label": "Inventory Management"},
            {"value": "pos", "label": "Point of Sale (POS)"},
            {"value": "users", "label": "User and Workspace Management"},
            {"value": "general", "label": "General Question"},
        ],
        "multiple": False,
        "allow_input": True,
    }
    task = Task(
        id="task-capability-inventory-menu",
        context_id="ctx-capability-inventory-menu",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="hello, what can you do for me")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "create_multiple_choice",
            {
                "title": "Inventory Management",
                "description": (
                    "Choose the inventory area you want help with. You can also type a specific inventory question."
                ),
                "options": [
                    {"value": "inventory_setup", "label": "Set Up Inventory"},
                    {"value": "inventory_visibility", "label": "Stock and Warehouse Visibility"},
                    {"value": "inventory_procurement", "label": "Purchase Orders and Receiving"},
                    {"value": "inventory_fulfillment", "label": "Transfers, Adjustments, and Fulfillment"},
                ],
                "multiple": False,
                "allow_input": True,
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["interaction_type"] == "multiple_choice"
    assert payload["workflow"] == "host_domain_area_picker"
    assert payload["workflow_stage"] == "area_picker"
    assert payload["domain_agent"] == "inventory"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_host_inventory_domain_picker_selection_delegates_to_inventory_router() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    domain_picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Inventory Management",
        "description": "Choose the inventory area you want help with. You can also type a specific inventory question.",
        "options": [
            {"value": "inventory_setup", "label": "Set Up Inventory"},
            {"value": "inventory_visibility", "label": "Stock and Warehouse Visibility"},
            {"value": "inventory_procurement", "label": "Purchase Orders and Receiving"},
            {"value": "inventory_fulfillment", "label": "Transfers, Adjustments, and Fulfillment"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "host_domain_area_picker",
        "workflow_stage": "area_picker",
        "domain_agent": "inventory",
    }
    task = Task(
        id="task-capability-inventory-route",
        context_id="ctx-capability-inventory-route",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[
                    TextPart(
                        text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}'
                    )
                ],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="hello, what can you do for me")]),
            Message(role=Role.agent, parts=[DataPart(data=domain_picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "delegate_to_agent",
            {
                "request": (
                    "The user selected Set Up Inventory from the Inventory Management menu. "
                    "Help them create or configure stock locations, inventory categories, or inventory items. "
                    "Start with a short structured choice or the next required setup step. "
                    "Never ask for raw internal ids when lookups or selections can be used instead."
                ),
                "agent_name": "inventory",
            },
        ),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "inventory"


@pytest.mark.asyncio
async def test_host_inventory_domain_picker_free_text_stays_in_inventory_domain() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    domain_picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Inventory Management",
        "description": "Choose the inventory area you want help with. You can also type a specific inventory question.",
        "options": [
            {"value": "inventory_setup", "label": "Set Up Inventory"},
            {"value": "inventory_visibility", "label": "Stock and Warehouse Visibility"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "host_domain_area_picker",
        "workflow_stage": "area_picker",
        "domain_agent": "inventory",
    }
    task = Task(
        id="task-capability-inventory-free-text",
        context_id="ctx-capability-inventory-free-text",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="show low stock")]),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="hello, what can you do for me")]),
            Message(role=Role.agent, parts=[DataPart(data=domain_picker_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text="show low stock")])

    events = [event async for event in processor(task, message, None, None)]

    assert ("inventory.get_stock_risk", {"limit": 25, "expiring_days": 30}) in fake_langgraph_components.FAKE_TOOL_CALLS
    assert (
        "delegate_to_agent",
        {
            "request": "show low stock",
            "agent_name": "inventory",
        },
    ) in fake_langgraph_components.FAKE_TOOL_CALLS

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "inventory"


@pytest.mark.asyncio
async def test_host_direct_staff_query_routes_to_users() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-staff-count",
        context_id="ctx-staff-count",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="how many staff do i have")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="how many staff do i have")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        ("delegate_to_agent", {"request": "how many staff do i have", "agent_name": "users"}),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "users"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == "You have 12 staff members in the current workspace."


@pytest.mark.asyncio
async def test_host_direct_setup_query_routes_to_onboarding() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-onboarding",
        context_id="ctx-onboarding",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="help me set up my inventory workspace from scratch")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="help me set up my inventory workspace from scratch")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "delegate_to_agent",
            {"request": "help me set up my inventory workspace from scratch", "agent_name": "onboarding"},
        ),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "onboarding"
    assert delegation_artifact.parts[0].data["finalState"] == "input-required"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"
    assert result_artifact.parts[0].data["workflow_stage"] == "scope_picker"
    assert result_artifact.parts[0].data["delegated_agent"] == "onboarding"
    assert result_artifact.parts[0].data["workflow"] == "inventory_onboarding"
    assert result_artifact.parts[0].data["delegated_task_id"] == "delegated-onboarding-scope"


@pytest.mark.asyncio
async def test_host_explicit_product_import_routes_directly_to_onboarding_without_listing() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-host-product-import",
        context_id="ctx-host-product-import",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "delegate_to_agent",
            {"request": "I need to import products into my inventory", "agent_name": "onboarding"},
        ),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "onboarding"
    assert delegation_artifact.parts[0].data["finalState"] == "input-required"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"
    assert result_artifact.parts[0].data["workflow_stage"] == "catalog_scope_prompt"
    assert result_artifact.parts[0].data["delegated_agent"] == "onboarding"
    assert result_artifact.parts[0].data["workflow"] == "product_import"


@pytest.mark.asyncio
async def test_host_continues_delegated_onboarding_interaction_with_same_task() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")

    first_task = Task(
        id="task-onboarding-follow-up-start",
        context_id="ctx-onboarding-follow-up",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="help me set up my inventory workspace from scratch")]),
        ),
    )
    first_message = Message(role=Role.user, parts=[TextPart(text="help me set up my inventory workspace from scratch")])

    first_events = [event async for event in processor(first_task, first_message, None, None)]
    first_payload = next(
        event.parts[0].data for event in first_events if isinstance(event, Artifact) and event.name == "result"
    )

    fake_langgraph_components.reset_fake_components()

    second_task = Task(
        id="task-onboarding-follow-up-continue",
        context_id="ctx-onboarding-follow-up",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"product_onboarding","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me set up my inventory workspace from scratch")]),
            Message(role=Role.agent, parts=[DataPart(data=first_payload)]),
        ],
    )
    second_message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"product_onboarding","additional_input":null}')],
    )

    second_events = [event async for event in processor(second_task, second_message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "delegate_to_agent",
            {
                "request": '{"type":"multiple_choice_response","selected":"product_onboarding","additional_input":null}',
                "agent_name": "onboarding",
                "delegated_task_id": "delegated-onboarding-scope",
            },
        ),
    ]

    delegation_artifact = next(event for event in second_events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "onboarding"
    assert delegation_artifact.parts[0].data["delegatedTaskId"] == "delegated-onboarding-wizard"
    assert delegation_artifact.parts[0].data["finalState"] == "input-required"

    result_artifact = next(event for event in second_events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "wizard_flow"
    assert result_artifact.parts[0].data["workflow_stage"] == "wizard"
    assert result_artifact.parts[0].data["delegated_agent"] == "onboarding"
    assert result_artifact.parts[0].data["delegated_task_id"] == "delegated-onboarding-wizard"

    status_events = [event for event in second_events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_host_registered_agents_query_stays_with_host() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-host-agents-list",
        context_id="ctx-host-agents-list",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text="tell me the agents that you have currently that are register")],
            ),
        ),
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text="tell me the agents that you have currently that are register")],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == (
        "Currently registered specialist agents: Inventory Management, Product Import, "
        "Point of Sale (POS), Product Management, User and Workspace Management."
    )


@pytest.mark.asyncio
async def test_host_registered_agents_query_reports_hidden_agents_when_not_host_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KA2A_TOOL_EXECUTOR",
        "tests.fake_langgraph_components:build_fake_tool_executor_without_users_or_onboarding",
    )

    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-host-agents-list-hidden",
        context_id="ctx-host-agents-list-hidden",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text="tell me the agents that you have currently that are register")],
            ),
        ),
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text="tell me the agents that you have currently that are register")],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == (
        "Currently registered specialist agents: Inventory Management, Product Import, "
        "Point of Sale (POS), Product Management, User and Workspace Management. "
        "The host is currently configured to route to: Inventory Management, Point of Sale (POS), Product Management."
    )


@pytest.mark.asyncio
async def test_onboarding_agent_starts_with_scope_picker() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    task = Task(
        id="task-onboarding-start",
        context_id="ctx-onboarding-start",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "users.get_active_company_profile",
        "create_multiple_choice",
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"
    assert result_artifact.parts[0].data["workflow_stage"] == "scope_picker"
    assert result_artifact.parts[0].data["description"].startswith("Current company: Intera Demo Company")

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_onboarding_agent_scope_selection_opens_wizard() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Start Product Import",
        "description": "Choose the setup area you want to complete first. I will guide you step by step.",
        "options": [
            {"value": "product_onboarding", "label": "Product Import"},
            {"value": "stock_locations", "label": "Stock Locations"},
            {"value": "inventory_categories", "label": "Inventory Categories"},
            {"value": "inventory_setup", "label": "Inventory Setup"},
            {"value": "product_onboarding", "label": "Product Onboarding"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "product_import",
        "workflow_stage": "scope_picker",
    }
    task = Task(
        id="task-onboarding-scope",
        context_id="ctx-onboarding-scope",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"product_onboarding","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"product_onboarding","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "users.get_active_company_profile",
        "create_wizard_flow",
        "inventory.list_stock_locations",
        "inventory.list_inventory_categories",
        "product.get_product_categories",
    ]
    assert fake_langgraph_components.FAKE_TOOL_CALLS[1][1]["title"] == "Product Import Wizard"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "wizard_flow"
    assert result_artifact.parts[0].data["workflow_stage"] == "wizard"
    assert result_artifact.parts[0].data["onboarding_scope"] == "product_onboarding"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_onboarding_agent_inventory_setup_scope_populates_relation_selects() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Start Product Import",
        "description": "Choose the setup area you want to complete first. I will guide you step by step.",
        "options": [
            {"value": "product_onboarding", "label": "Product Import"},
            {"value": "stock_locations", "label": "Stock Locations"},
            {"value": "inventory_categories", "label": "Inventory Categories"},
            {"value": "inventory_setup", "label": "Inventory Setup"},
            {"value": "product_onboarding", "label": "Product Onboarding"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "product_import",
        "workflow_stage": "scope_picker",
    }
    task = Task(
        id="task-onboarding-scope-relations",
        context_id="ctx-onboarding-scope-relations",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me set up inventory")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "users.get_active_company_profile",
        "create_wizard_flow",
        "inventory.list_stock_locations",
        "inventory.list_inventory_categories",
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["interaction_type"] == "wizard_flow"
    assert payload["workflow_stage"] == "wizard"
    assert payload["onboarding_scope"] == "inventory_setup"

    fields = {field["name"]: field for field in payload["steps"][0]["fields"]}
    assert fields["default_inventory_name"]["type"] == "text"
    assert fields["related_stock_location_id"]["type"] == "select"
    assert fields["related_stock_location_id"]["options"][0]["label"] == "Main Warehouse"
    assert fields["related_stock_location_id"]["options"][0]["value"] == "loc-1"
    assert fields["inventory_category_id"]["type"] == "select"
    assert fields["inventory_category_id"]["options"][0]["label"] == "Men's Clothes"
    assert fields["inventory_category_id"]["options"][0]["value"] == "cat-1"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_onboarding_agent_descriptive_request_opens_prefilled_wizard() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    task = Task(
        id="task-onboarding-direct-prefill",
        context_id="ctx-onboarding-direct-prefill",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[
                    TextPart(
                        text=(
                            "I want to onboard a new inventory.\n"
                            "Primary location: Main Warehouse\n"
                            "Additional locations: Front Store, Returns Shelf\n"
                            "Inventory categories: Men's Clothes, Shoes\n"
                            "Inventory name: Fashion Master Inventory\n"
                            "Inventory description: Primary stock ledger for apparel.\n"
                            "Initial products: Oxford Shirt, Canvas Sneakers\n"
                            "Product category: Footwear"
                        )
                    )
                ],
            ),
        ),
        history=[],
    )
    message = Message(
        role=Role.user,
        parts=[
            TextPart(
                text=(
                    "I want to onboard a new inventory.\n"
                    "Primary location: Main Warehouse\n"
                    "Additional locations: Front Store, Returns Shelf\n"
                    "Inventory categories: Men's Clothes, Shoes\n"
                    "Inventory name: Fashion Master Inventory\n"
                    "Inventory description: Primary stock ledger for apparel.\n"
                    "Initial products: Oxford Shirt, Canvas Sneakers\n"
                    "Product category: Footwear"
                )
            )
        ],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "users.get_active_company_profile",
        "create_wizard_flow",
        "inventory.list_stock_locations",
        "inventory.list_inventory_categories",
        "product.get_product_categories",
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["interaction_type"] == "wizard_flow"
    assert payload["workflow_stage"] == "wizard"
    assert payload["onboarding_scope"] == "product_onboarding"
    assert payload["description"].startswith("I prefilled this setup from your message.")

    existing_responses = payload["existing_responses"]
    assert existing_responses["step_0"] == {
        "primary_location_mode": "new",
        "primary_location_name": "Main Warehouse",
        "primary_location_type": "warehouse",
        "additional_locations": "Front Store\nReturns Shelf",
    }
    assert existing_responses["step_1"] == {
        "category_names": "Men's Clothes\nShoes",
    }
    assert existing_responses["step_2"] == {
        "default_inventory_name": "Fashion Master Inventory",
        "inventory_description": "Primary stock ledger for apparel.",
        "related_stock_location_id": "loc-1",
        "inventory_category_id": "cat-1",
    }
    assert existing_responses["step_3"] == {
        "continue_to_product_onboarding": True,
        "initial_product_names": "Oxford Shirt\nCanvas Sneakers",
        "product_category_id": "prod-cat-2",
    }

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_onboarding_agent_inventory_setup_scope_populates_relation_selects_from_wrapped_lookup_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KA2A_TOOL_EXECUTOR",
        "tests.fake_langgraph_components:build_fake_wrapped_lookup_tool_executor",
    )

    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Start Product Import",
        "description": "Choose the setup area you want to complete first. I will guide you step by step.",
        "options": [
            {"value": "product_onboarding", "label": "Product Import"},
            {"value": "stock_locations", "label": "Stock Locations"},
            {"value": "inventory_categories", "label": "Inventory Categories"},
            {"value": "inventory_setup", "label": "Inventory Setup"},
            {"value": "product_onboarding", "label": "Product Onboarding"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "product_import",
        "workflow_stage": "scope_picker",
    }
    task = Task(
        id="task-onboarding-scope-relations-wrapped",
        context_id="ctx-onboarding-scope-relations-wrapped",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me set up inventory")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    fields = {field["name"]: field for field in payload["steps"][0]["fields"]}

    assert fields["default_inventory_name"]["type"] == "text"
    assert fields["related_stock_location_id"]["type"] == "select"
    assert fields["related_stock_location_id"]["options"][0]["label"] == "Main Warehouse"
    assert fields["related_stock_location_id"]["options"][0]["value"] == "loc-1"
    assert fields["inventory_category_id"]["type"] == "select"
    assert fields["inventory_category_id"]["options"][0]["label"] == "Men's Clothes"
    assert fields["inventory_category_id"]["options"][0]["value"] == "cat-1"


@pytest.mark.asyncio
async def test_onboarding_agent_inventory_setup_scope_populates_category_options_from_text_wrapped_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KA2A_TOOL_EXECUTOR",
        "tests.fake_langgraph_components:build_fake_category_text_wrapped_tool_executor",
    )

    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Start Product Import",
        "description": "Choose the setup area you want to complete first. I will guide you step by step.",
        "options": [
            {"value": "product_onboarding", "label": "Product Import"},
            {"value": "stock_locations", "label": "Stock Locations"},
            {"value": "inventory_categories", "label": "Inventory Categories"},
            {"value": "inventory_setup", "label": "Inventory Setup"},
            {"value": "product_onboarding", "label": "Product Onboarding"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "product_import",
        "workflow_stage": "scope_picker",
    }
    task = Task(
        id="task-onboarding-scope-relations-category-text-wrapped",
        context_id="ctx-onboarding-scope-relations-category-text-wrapped",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me set up inventory")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"inventory_setup","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    fields = {field["name"]: field for field in payload["steps"][0]["fields"]}

    assert fields["related_stock_location_id"]["type"] == "select"
    assert fields["related_stock_location_id"]["options"][0]["label"] == "Main Warehouse"
    assert fields["inventory_category_id"]["type"] == "select"
    assert fields["inventory_category_id"]["options"][0]["label"] == "Men's Clothes"
    assert fields["inventory_category_id"]["options"][0]["value"] == "cat-1"


@pytest.mark.asyncio
async def test_relation_uuid_tool_error_returns_select_options_instead_of_asking_for_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_LLM_FACTORY", "tests.fake_langgraph_components:fake_uuid_failure_llm_factory")
    monkeypatch.setenv("KA2A_TOOL_EXECUTOR", "tests.fake_langgraph_components:build_fake_tool_executor_with_uuid_failure")

    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_setup")
    form_payload = {
        "interaction_type": "dynamic_form",
        "title": "Create Inventory Item",
        "description": "Confirm the inventory details before I create anything.",
        "fields": [],
        "workflow": "inventory_setup_mutation",
        "workflow_stage": "form",
        "mutation_action": "create_inventory_item",
    }
    form_response = (
        '{"type":"form_response","data":{"default_inventory_name":"Fashion Master Inventory",'
        '"inventory_description":"Primary stock ledger for apparel.","inventory_category_id":"not-a-uuid"},'
        '"message":"Form submitted successfully"}'
    )
    task = Task(
        id="task-inventory-uuid-recovery",
        context_id="ctx-inventory-uuid-recovery",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=form_response)]),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="create the first inventory item")]),
            Message(role=Role.agent, parts=[DataPart(data=form_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=form_response)])

    events = [event async for event in processor(task, message, None, None)]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    fields = {field["name"]: field for field in payload["fields"]}

    assert payload["interaction_type"] == "dynamic_form"
    assert "raw IDs" not in payload["description"].lower()
    assert "category_id" in fields
    assert fields["category_id"]["type"] == "select"
    assert fields["category_id"]["options"][0]["label"] == "Men's Clothes"
    assert fields["category_id"]["options"][0]["value"] == "cat-1"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_onboarding_agent_wizard_completion_prompts_for_review() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    wizard_payload = {
        "interaction_type": "wizard_flow",
        "title": "Product Import Wizard",
        "description": "Fill in the setup details and I will prepare the onboarding action plan.",
        "steps": [],
        "allow_back": True,
        "show_progress": True,
        "workflow": "product_import",
        "workflow_stage": "wizard",
        "onboarding_scope": "product_onboarding",
    }
    response_text = (
        '{"type":"wizard_flow_response","completed":true,"all_responses":'
        '{"step_0":{"primary_location_name":"Main Warehouse","primary_location_type":"warehouse","additional_locations":"Front Store"},'
        '"step_1":{"category_names":"Beverages\\nSnacks\\nCleaning Supplies"},'
        '"step_2":{"default_inventory_name":"Main Inventory","inventory_description":"Primary sellable stock ledger"},'
        '"step_3":{"continue_to_product_onboarding":true,"initial_product_names":"Coca-Cola 50cl\\nFanta 50cl"}}}'
    )
    task = Task(
        id="task-onboarding-review",
        context_id="ctx-onboarding-review",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=response_text)]),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")]),
            Message(role=Role.agent, parts=[DataPart(data=wizard_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=response_text)])

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "users.get_active_company_profile",
        "create_multiple_choice",
    ]
    assert fake_langgraph_components.FAKE_TOOL_CALLS[1][1]["title"] == "Review Onboarding Plan"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"
    assert result_artifact.parts[0].data["workflow_stage"] == "review"
    assert result_artifact.parts[0].data["onboarding_data"]["flat"]["default_inventory_name"] == "Main Inventory"
    assert "Main Warehouse" in result_artifact.parts[0].data["onboarding_summary"]
    assert result_artifact.parts[0].data["onboarding_data"]["company_context"]["name"] == "Intera Demo Company"


@pytest.mark.asyncio
async def test_onboarding_agent_review_uses_relation_labels_from_wizard_selection() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    wizard_payload = {
        "interaction_type": "wizard_flow",
        "title": "Inventory Setup Wizard",
        "description": "Fill in the setup details and I will prepare the onboarding action plan.",
        "steps": [
            {
                "id": "inventory",
                "title": "Inventory Setup",
                "description": "Define the first inventory item you want to create.",
                "fields": [
                    {
                        "name": "default_inventory_name",
                        "type": "text",
                        "label": "Inventory Name",
                        "required": True,
                    },
                    {
                        "name": "related_stock_location_id",
                        "type": "select",
                        "label": "Primary Location for This Inventory",
                        "required": False,
                        "options": [
                            {"value": "loc-1", "label": "Main Warehouse"},
                            {"value": "loc-2", "label": "Front Store"},
                        ],
                    },
                    {
                        "name": "inventory_category_id",
                        "type": "select",
                        "label": "Default Category",
                        "required": False,
                        "options": [
                            {"value": "cat-1", "label": "Men's Clothes"},
                            {"value": "cat-2", "label": "Shoes"},
                        ],
                    },
                ],
            }
        ],
        "allow_back": True,
        "show_progress": True,
        "workflow": "product_import",
        "workflow_stage": "wizard",
        "onboarding_scope": "inventory_setup",
    }
    response_text = (
        '{"type":"wizard_flow_response","completed":true,"all_responses":'
        '{"step_0":{"default_inventory_name":"Main Inventory","related_stock_location_id":"loc-1","inventory_category_id":"cat-2"}}}'
    )
    task = Task(
        id="task-onboarding-review-relations",
        context_id="ctx-onboarding-review-relations",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=response_text)]),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me set up inventory")]),
            Message(role=Role.agent, parts=[DataPart(data=wizard_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=response_text)])

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "users.get_active_company_profile",
        "create_multiple_choice",
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["interaction_type"] == "multiple_choice"
    assert payload["workflow_stage"] == "review"
    assert payload["onboarding_data"]["flat"]["related_stock_location_id"] == "loc-1"
    assert payload["onboarding_data"]["flat"]["related_stock_location_label"] == "Main Warehouse"
    assert payload["onboarding_data"]["flat"]["inventory_category_id"] == "cat-2"
    assert payload["onboarding_data"]["flat"]["inventory_category_label"] == "Shoes"
    assert "Ledger location: Main Warehouse" in payload["onboarding_summary"]
    assert "Default category: Shoes" in payload["onboarding_summary"]
    assert "loc-1" not in payload["onboarding_summary"]
    assert "cat-2" not in payload["onboarding_summary"]


@pytest.mark.asyncio
async def test_onboarding_agent_review_confirmation_creates_inventory_setup_directly() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    review_payload = {
        "interaction_type": "multiple_choice",
        "title": "Review Onboarding Plan",
        "description": "Review your onboarding plan.",
        "options": [
            {"value": "create_now", "label": "Create This Setup"},
            {"value": "cancel_onboarding", "label": "Cancel For Now"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "product_import",
        "workflow_stage": "review",
        "onboarding_scope": "product_onboarding",
        "onboarding_data": {
            "scope": "full_setup",
            "steps": {},
            "flat": {
                "primary_location_name": "Main Warehouse",
                "primary_location_type": "warehouse",
                "additional_locations": "Front Store\nReturns Shelf",
                "category_names": "Beverages\nSnacks\nCleaning Supplies",
                "default_inventory_name": "Main Inventory",
                "continue_to_product_onboarding": True,
            },
            "raw_response": {},
        },
        "onboarding_summary": "Scope: Product Import",
    }
    task = Task(
        id="task-onboarding-confirm",
        context_id="ctx-onboarding-confirm",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"create_now","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")]),
            Message(role=Role.agent, parts=[DataPart(data=review_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"create_now","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    tool_calls = list(fake_langgraph_components.FAKE_TOOL_CALLS)
    create_calls = [(name, args) for name, args in tool_calls if ".create_" in name]
    create_call_names = [name for name, _ in create_calls]

    assert tool_calls[0][0] == "users.get_active_company_profile"
    assert "inventory.create_stock_location" in create_call_names
    assert "inventory.create_inventory_category" in create_call_names
    assert "inventory.create_inventory_item" in create_call_names
    assert create_call_names.count("inventory.create_stock_location") >= 1
    assert create_call_names.count("inventory.create_inventory_category") >= 1

    second_location_args = next(
        args["payload"]
        for name, args in create_calls
        if name == "inventory.create_stock_location" and args["payload"].get("parent_id")
    )
    first_category_args = next(args["payload"] for name, args in create_calls if name == "inventory.create_inventory_category")
    inventory_args = next(args["payload"] for name, args in create_calls if name == "inventory.create_inventory_item")

    assert second_location_args["parent_id"]
    assert first_category_args["default_location_id"]
    assert inventory_args["inventory_category_id"]
    assert not any(isinstance(event, Artifact) and event.name == "delegation" for event in events)

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == (
        "Created 3 stock locations, 3 inventory categories, and 1 inventory item for onboarding."
    )
    created_artifact = next(
        event for event in events if isinstance(event, Artifact) and event.name == "onboarding.created_operations"
    )
    assert len(created_artifact.parts[0].data["operations"]) == 7

@pytest.mark.asyncio
async def test_onboarding_agent_saved_workflow_repeats_catalog_scope_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_CONTEXT_MEMORY_STORE", "memory")

    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    first_task = Task(
        id="task-onboarding-save",
        context_id="ctx-onboarding-save",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")]),
        ),
    )
    first_message = Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")])

    _ = [event async for event in processor(first_task, first_message, None, None)]

    fake_langgraph_components.reset_fake_components()

    second_task = Task(
        id="task-onboarding-resume",
        context_id="ctx-onboarding-save",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="continue onboarding")]),
        ),
    )
    second_message = Message(role=Role.user, parts=[TextPart(text="continue onboarding")])

    events = [event async for event in processor(second_task, second_message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("users.get_active_company_profile", {}),
        (
            "create_multiple_choice",
            {
                "title": "Start Product Import",
                "description": "Current company: Intera Demo Company\n\nChoose the setup area you want to complete first. I will guide you step by step.",
                "options": [
                    {"value": "product_onboarding", "label": "Product Import"},
                    {"value": "stock_locations", "label": "Stock Locations"},
                    {"value": "inventory_categories", "label": "Inventory Categories"},
                    {"value": "inventory_setup", "label": "Inventory Setup"},
                ],
                "multiple": False,
                "allow_input": True,
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["workflow_stage"] == "scope_picker"
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_onboarding_agent_explicit_product_import_skips_saved_resume_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_CONTEXT_MEMORY_STORE", "memory")

    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    first_task = Task(
        id="task-onboarding-save-import",
        context_id="ctx-onboarding-save-import",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")]),
        ),
    )
    first_message = Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")])

    _ = [event async for event in processor(first_task, first_message, None, None)]

    fake_langgraph_components.reset_fake_components()

    second_task = Task(
        id="task-onboarding-import-fresh",
        context_id="ctx-onboarding-save-import",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")]),
        ),
    )
    second_message = Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")])

    events = [event async for event in processor(second_task, second_message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("users.get_active_company_profile", {}),
        (
            "create_multiple_choice",
            {
                "title": "Choose Catalog Filters",
                "description": "Current company: Intera Demo Company\n\nI can import products from the global catalog. Do you want to browse by category or by brand?",
                "options": [
                    {"value": "category", "label": "Product Category"},
                    {"value": "brand", "label": "Brand"},
                ],
                "multiple": False,
                "allow_input": True,
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["workflow_stage"] == "catalog_scope_prompt"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_product_router_explicit_product_import_reuses_structured_onboarding_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_CONTEXT_MEMORY_STORE", "memory")

    processor = make_langgraph_chat_processor_from_env(agent_name="product")
    task = Task(
        id="task-product-router-import",
        context_id="ctx-product-router-import",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "create_multiple_choice",
            {
                "title": "Choose Catalog Filters",
                "description": "I can import products from the global catalog. Do you want to browse by category or by brand?",
                "options": [
                    {"value": "category", "label": "Product Category"},
                    {"value": "brand", "label": "Brand"},
                ],
                "multiple": False,
                "allow_input": True,
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["workflow_stage"] == "catalog_scope_prompt"
    assert result_artifact.parts[0].data["workflow"] == "product_import"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_namespaced_product_agent_explicit_product_import_reuses_structured_onboarding_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_CONTEXT_MEMORY_STORE", "memory")

    processor = make_langgraph_chat_processor_from_env(agent_name="wa-p4-product-d4a6fa2ad877")
    task = Task(
        id="task-namespaced-product-router-import",
        context_id="ctx-namespaced-product-router-import",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "create_multiple_choice",
            {
                "title": "Choose Catalog Filters",
                "description": "I can import products from the global catalog. Do you want to browse by category or by brand?",
                "options": [
                    {"value": "category", "label": "Product Category"},
                    {"value": "brand", "label": "Brand"},
                ],
                "multiple": False,
                "allow_input": True,
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["workflow_stage"] == "catalog_scope_prompt"
    assert result_artifact.parts[0].data["workflow"] == "product_import"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_namespaced_inventory_agent_explicit_product_import_reuses_structured_onboarding_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_CONTEXT_MEMORY_STORE", "memory")

    processor = make_langgraph_chat_processor_from_env(agent_name="wa-p4-inventory-d4a6fa2ad877")
    task = Task(
        id="task-namespaced-inventory-router-import",
        context_id="ctx-namespaced-inventory-router-import",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "create_multiple_choice",
            {
                "title": "Choose Catalog Filters",
                "description": "I can import products from the global catalog. Do you want to browse by category or by brand?",
                "options": [
                    {"value": "category", "label": "Product Category"},
                    {"value": "brand", "label": "Brand"},
                ],
                "multiple": False,
                "allow_input": True,
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["workflow_stage"] == "catalog_scope_prompt"
    assert result_artifact.parts[0].data["workflow"] == "product_import"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_product_catalog_admin_explicit_product_import_reuses_structured_onboarding_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_CONTEXT_MEMORY_STORE", "memory")

    processor = make_langgraph_chat_processor_from_env(agent_name="product_catalog_admin")
    task = Task(
        id="task-product-catalog-admin-import",
        context_id="ctx-product-catalog-admin-import",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "create_multiple_choice",
            {
                "title": "Choose Catalog Filters",
                "description": "I can import products from the global catalog. Do you want to browse by category or by brand?",
                "options": [
                    {"value": "category", "label": "Product Category"},
                    {"value": "brand", "label": "Brand"},
                ],
                "multiple": False,
                "allow_input": True,
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["workflow_stage"] == "catalog_scope_prompt"
    assert result_artifact.parts[0].data["workflow"] == "product_import"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_inventory_setup_explicit_product_import_reuses_structured_onboarding_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_CONTEXT_MEMORY_STORE", "memory")

    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_setup")
    task = Task(
        id="task-inventory-setup-import",
        context_id="ctx-inventory-setup-import",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "create_multiple_choice",
            {
                "title": "Choose Catalog Filters",
                "description": "I can import products from the global catalog. Do you want to browse by category or by brand?",
                "options": [
                    {"value": "category", "label": "Product Category"},
                    {"value": "brand", "label": "Brand"},
                ],
                "multiple": False,
                "allow_input": True,
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["workflow_stage"] == "catalog_scope_prompt"
    assert result_artifact.parts[0].data["workflow"] == "product_import"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_namespaced_product_agent_category_scope_returns_category_widget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_CONTEXT_MEMORY_STORE", "memory")

    processor = make_langgraph_chat_processor_from_env(agent_name="wa-p4-product-d4a6fa2ad877")
    catalog_scope_payload = {
        "interaction_type": "multiple_choice",
        "title": "Choose Catalog Filters",
        "description": "I can import products from the global catalog. Do you want to browse by category or by brand?",
        "options": [
            {"value": "category", "label": "Product Category"},
            {"value": "brand", "label": "Brand"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "product_import",
        "workflow_stage": "catalog_scope_prompt",
        "onboarding_scope": "product_onboarding",
    }
    task = Task(
        id="task-namespaced-product-router-category-prompt",
        context_id="ctx-namespaced-product-router-category-prompt",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"category","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")]),
            Message(role=Role.agent, parts=[DataPart(data=catalog_scope_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"category","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("product.list_global_catalog_categories", {"limit": 100}),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["workflow_stage"] == "category_selection"
    assert payload["interaction_type"] == "searchable_selection"
    assert payload["title"] == "Choose Product Categories"
    assert [item["name"] for item in payload["items"]] == ["Beverages", "Groceries"]

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_namespaced_product_agent_category_selection_resumes_from_saved_workflow_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_CONTEXT_MEMORY_STORE", "memory")

    processor = make_langgraph_chat_processor_from_env(agent_name="wa-p4-product-d4a6fa2ad877")
    scope_payload = {
        "interaction_type": "multiple_choice",
        "title": "Choose Catalog Filters",
        "description": "I can import products from the global catalog. Do you want to browse by category or by brand?",
        "options": [
            {"value": "category", "label": "Product Category"},
            {"value": "brand", "label": "Brand"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "product_import",
        "workflow_stage": "catalog_scope_prompt",
        "onboarding_scope": "product_onboarding",
    }
    first_task = Task(
        id="task-namespaced-product-router-category",
        context_id="ctx-namespaced-product-router-category",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"category","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="I need to import products into my inventory")]),
            Message(role=Role.agent, parts=[DataPart(data=scope_payload)]),
        ],
    )
    first_message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"category","additional_input":null}')],
    )

    first_events = [event async for event in processor(first_task, first_message, None, None)]

    first_result = next(event for event in first_events if isinstance(event, Artifact) and event.name == "result")
    first_payload = first_result.parts[0].data
    assert first_payload["workflow_stage"] == "category_selection"

    fake_langgraph_components.reset_fake_components()

    second_task = Task(
        id="task-namespaced-product-router-category-continue",
        context_id="ctx-namespaced-product-router-category",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"searchable_selection_response","selected_items":["Beverages"]}')],
            ),
        ),
    )
    second_message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"searchable_selection_response","selected_items":["Beverages"]}')],
    )

    second_events = [event async for event in processor(second_task, second_message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "product.list_global_catalog_products",
            {
                "categories": ["Beverages"],
                "brands": None,
                "page": 1,
                "page_size": 30,
                "exclude_imported": True,
            },
        ),
    ]

    second_result = next(event for event in second_events if isinstance(event, Artifact) and event.name == "result")
    second_payload = second_result.parts[0].data
    assert second_payload["workflow_stage"] == "product_selection"
    assert second_payload["interaction_type"] == "searchable_selection"
    assert second_payload["title"] == "Choose Products to Import"
    assert [item["name"] for item in second_payload["items"]] == [
        "Eva Premium Water 75cl",
        "Coca-Cola Original Taste 50cl",
    ]

    second_status_events = [event for event in second_events if isinstance(event, TaskStatus)]
    assert second_status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_product_import_failure_surfaces_targeted_mcp_auth_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KA2A_TOOL_EXECUTOR",
        "tests.fake_langgraph_components:build_fake_tool_executor_with_global_import_failure",
    )

    processor = make_langgraph_chat_processor_from_env(agent_name="product")
    product_selection_payload = {
        "interaction_type": "searchable_selection",
        "title": "Choose Products to Import",
        "description": "Page 1 of 1. Select the products you want to import from categories: Beverages.",
        "items": [
            {"id": "global-prod-1", "name": "Eva Premium Water 75cl"},
            {"id": "global-prod-2", "name": "Coca-Cola Original Taste 50cl"},
        ],
        "search_fields": ["name", "description", "category"],
        "multiple": True,
        "max_selections": 30,
        "allow_additional_input": False,
        "workflow": "product_import",
        "workflow_stage": "product_selection",
        "onboarding_scope": "product_onboarding",
        "catalog_scope": "category",
        "selected_category_names": ["Beverages"],
        "selected_brand_names": [],
        "page": 1,
        "total_pages": 1,
        "total_count": 2,
        "imported_count": 0,
    }
    task = Task(
        id="task-product-import-failure",
        context_id="ctx-product-import-failure",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"searchable_selection_response","selected_items":["global-prod-1"]}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="I want to import products")]),
            Message(role=Role.agent, parts=[DataPart(data=product_selection_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"searchable_selection_response","selected_items":["global-prod-1"]}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("product.import_global_catalog_products", {"global_product_ids": ["global-prod-1"]}),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].text == (
        "I couldn't import the selected products from the global catalog. "
        "The global catalog connection rejected the request. Reconnect the product MCP credentials and retry the import."
    )

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.failed


@pytest.mark.asyncio
async def test_product_import_duplicate_selection_does_not_reimport_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_CONTEXT_MEMORY_STORE", "memory")

    processor = make_langgraph_chat_processor_from_env(agent_name="product")
    product_selection_payload = {
        "interaction_type": "searchable_selection",
        "title": "Choose Products to Import",
        "description": "Page 1 of 2. Select the products you want to import from categories: Beverages.",
        "items": [
            {"id": "global-prod-1", "name": "Eva Premium Water 75cl"},
            {"id": "global-prod-2", "name": "Coca-Cola Original Taste 50cl"},
        ],
        "search_fields": ["name", "description", "category"],
        "multiple": True,
        "max_selections": 30,
        "allow_additional_input": False,
        "workflow": "product_import",
        "workflow_stage": "product_selection",
        "onboarding_scope": "product_onboarding",
        "catalog_scope": "category",
        "selected_category_names": ["Beverages"],
        "selected_brand_names": [],
        "page": 1,
        "total_pages": 2,
        "total_count": 60,
        "imported_count": 0,
    }
    first_task = Task(
        id="task-product-import-duplicate-first",
        context_id="ctx-product-import-duplicate",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"searchable_selection_response","selected_items":["global-prod-1"]}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="I want to import products")]),
            Message(role=Role.agent, parts=[DataPart(data=product_selection_payload)]),
        ],
    )
    first_message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"searchable_selection_response","selected_items":["global-prod-1"]}')],
    )

    first_events = [event async for event in processor(first_task, first_message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("product.import_global_catalog_products", {"global_product_ids": ["global-prod-1"]}),
    ]

    first_result = next(event for event in first_events if isinstance(event, Artifact) and event.name == "result")
    assert first_result.parts[0].data["workflow_stage"] == "page_continue"

    first_status_events = [event for event in first_events if isinstance(event, TaskStatus)]
    assert first_status_events[-1].state == TaskState.input_required

    fake_langgraph_components.reset_fake_components()

    second_task = Task(
        id="task-product-import-duplicate-second",
        context_id="ctx-product-import-duplicate",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"searchable_selection_response","selected_items":["global-prod-1"]}')],
            ),
        ),
    )
    second_message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"searchable_selection_response","selected_items":["global-prod-1"]}')],
    )

    second_events = [event async for event in processor(second_task, second_message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == []

    second_result = next(event for event in second_events if isinstance(event, Artifact) and event.name == "result")
    assert second_result.parts[0].text == (
        "Those products were already imported from this catalog page. "
        "Use the current prompt to continue to the next page or finish the import."
    )

    second_status_events = [event for event in second_events if isinstance(event, TaskStatus)]
    assert second_status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_onboarding_agent_partial_failures_prompt_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KA2A_TOOL_EXECUTOR",
        "tests.fake_langgraph_components:build_fake_tool_executor_with_category_failures",
    )

    processor = make_langgraph_chat_processor_from_env(agent_name="onboarding")
    review_payload = {
        "interaction_type": "multiple_choice",
        "title": "Review Onboarding Plan",
        "description": "Review your onboarding plan.",
        "options": [
            {"value": "create_now", "label": "Create This Setup"},
            {"value": "cancel_onboarding", "label": "Cancel For Now"},
        ],
        "multiple": False,
        "allow_input": True,
        "workflow": "product_import",
        "workflow_stage": "review",
        "onboarding_scope": "product_onboarding",
        "onboarding_data": {
            "scope": "full_setup",
            "steps": {},
            "flat": {
                "primary_location_name": "Main Warehouse",
                "primary_location_type": "warehouse",
                "category_names": "Beverages\nSnacks\nCleaning Supplies",
                "default_inventory_name": "Main Inventory",
            },
            "raw_response": {},
        },
        "onboarding_summary": "Scope: Product Import",
    }
    task = Task(
        id="task-onboarding-retry",
        context_id="ctx-onboarding-retry",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"create_now","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="help me get started with onboarding")]),
            Message(role=Role.agent, parts=[DataPart(data=review_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"create_now","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "users.get_active_company_profile",
        "inventory.search_stock_locations",
        "inventory.list_inventory_categories",
        "inventory.create_inventory_category",
        "inventory.create_inventory_category",
        "inventory.create_inventory_category",
        "inventory.create_inventory_item",
        "create_multiple_choice",
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"
    assert result_artifact.parts[0].data["workflow_stage"] == "retry"
    assert len(result_artifact.parts[0].data["created_operations"]) == 2
    assert len(result_artifact.parts[0].data["failed_operations"]) == 3

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_host_unavailable_selected_agent_reprompts_instead_of_misrouting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_TOOL_EXECUTOR", "tests.fake_langgraph_components:build_fake_tool_executor_without_users")

    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Choose What You Need Help With",
        "description": "Select the area you want help with. I can continue from your choice.",
        "options": [
            {"value": "onboarding", "label": "Product Import"},
            {"value": "product", "label": "Product Management"},
            {"value": "inventory", "label": "Inventory Management"},
            {"value": "pos", "label": "Point of Sale (POS)"},
            {"value": "users", "label": "User and Workspace Management"},
            {"value": "general", "label": "General Question"},
        ],
        "multiple": False,
        "allow_input": True,
    }
    task = Task(
        id="task-capability-missing-users",
        context_id="ctx-capability-missing-users",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text='{"type":"multiple_choice_response","selected":"users","additional_input":null}')],
            ),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="hello, what can you do for me")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text='{"type":"multiple_choice_response","selected":"users","additional_input":null}')],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "create_multiple_choice",
            {
                "title": "Choose What You Need Help With",
                "description": (
                    "User and Workspace Management is not currently available. "
                    "Choose one of the areas that is available right now."
                ),
                "options": [
                    {"value": "onboarding", "label": "Product Import"},
                    {"value": "product", "label": "Product Management"},
                    {"value": "inventory", "label": "Inventory Management"},
                    {"value": "pos", "label": "Point of Sale (POS)"},
                    {"value": "general", "label": "General Question"},
                ],
                "multiple": False,
                "allow_input": True,
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_host_direct_sales_analysis_request_does_not_fall_back_to_capability_picker_when_pos_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_TOOL_EXECUTOR", "tests.fake_langgraph_components:build_fake_tool_executor_without_pos")

    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-pos-unavailable-sales-analysis",
        context_id="ctx-pos-unavailable-sales-analysis",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text="Analyse my sales data for the past one year")],
            ),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="Analyse my sales data for the past one year")])

    events = [event async for event in processor(task, message, None, None)]

    assert not any(name == "create_multiple_choice" for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS)

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    result_text = _text_from_parts(result_artifact.parts)
    assert "Choose one of the areas that is available right now." not in result_text

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state != TaskState.input_required


@pytest.mark.asyncio
async def test_host_free_text_onboarding_reply_after_picker_reprompts_when_onboarding_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KA2A_TOOL_EXECUTOR",
        "tests.fake_langgraph_components:build_fake_tool_executor_without_users_or_onboarding",
    )

    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    picker_payload = {
        "interaction_type": "multiple_choice",
        "title": "Choose What You Need Help With",
        "description": "Select the area you want help with. I can continue from your choice.",
        "options": [
            {"value": "product", "label": "Product Management"},
            {"value": "inventory", "label": "Inventory Management"},
            {"value": "pos", "label": "Point of Sale (POS)"},
            {"value": "general", "label": "General Question"},
        ],
        "multiple": False,
        "allow_input": True,
    }
    task = Task(
        id="task-capability-free-text-onboarding",
        context_id="ctx-capability-free-text-onboarding",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="i want to do inventory onoarding")]),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="what can you do?")]),
            Message(role=Role.agent, parts=[DataPart(data=picker_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text="i want to do inventory onoarding")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "create_multiple_choice",
            {
                "title": "Choose What You Need Help With",
                "description": (
                    "Product Import is not currently available. "
                    "Choose one of the areas that is available right now."
                ),
                "options": [
                    {"value": "product", "label": "Product Management"},
                    {"value": "inventory", "label": "Inventory Management"},
                    {"value": "pos", "label": "Point of Sale (POS)"},
                    {"value": "general", "label": "General Question"},
                ],
                "multiple": False,
                "allow_input": True,
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_host_answers_unavailable_agent_diagnostics_without_misrouting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KA2A_TOOL_EXECUTOR",
        "tests.fake_langgraph_components:build_fake_tool_executor_without_users_or_onboarding",
    )

    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-capability-unavailable-diagnostics",
        context_id="ctx-capability-unavailable-diagnostics",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text="why is the onboarding agent not active and can I see the error message")],
            ),
        ),
    )
    message = Message(
        role=Role.user,
        parts=[TextPart(text="why is the onboarding agent not active and can I see the error message")],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == (
        "Product Import is registered in the current agent directory, but it is not currently exposed to the host "
        "for routing. The host currently routes to these available areas: Inventory Management, Point of Sale (POS), "
        "Product Management. "
        "There is no specialist error message to show here because the host did not delegate this request. "
        "This looks like a host or gateway configuration issue, such as the downstream allowlist, not a downstream "
        "task failure."
    )

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.completed


@pytest.mark.asyncio
async def test_host_propagates_input_required_from_specialist() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-2",
        context_id="ctx-2",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="ambiguous product stock question")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="ambiguous product stock question")])

    events = [event async for event in processor(task, message, None, None)]

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    delegation_payload = delegation_artifact.parts[0].data
    assert delegation_payload["selectedAgent"] == "product"
    assert delegation_payload["finalState"] == "input-required"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].data["interaction_type"] == "multiple_choice"
    assert result_artifact.parts[0].data["delegated_agent"] == "product"
    assert result_artifact.parts[0].data["delegated_task_id"] == "delegated-2"


@pytest.mark.asyncio
async def test_host_multi_domain_request_prompts_to_continue_with_next_specialist() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    request = "Set up inventory locations and then create products"
    task = Task(
        id="task-host-multidomain",
        context_id="ctx-host-multidomain",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=request)]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        ("delegate_to_agent", {"request": request, "agent_name": "inventory"}),
        (
            "create_multiple_choice",
            {
                "title": "Continue Workflow",
                "description": (
                    "Inventory Management finished the current step.\n\n"
                    "Latest result: Inventory locations created successfully.\n\n"
                    "Continue with Product Management now?"
                ),
                "options": [
                    {"value": "continue_next", "label": "Continue to Product Management"},
                    {"value": "stop_here", "label": "Stop Here"},
                ],
                "multiple": False,
                "allow_input": False,
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["workflow"] == "host_orchestration"
    assert payload["workflow_stage"] == "continue_prompt"
    assert payload["next_agent"] == "product"

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_host_multi_domain_continue_response_delegates_next_specialist() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    seed_request = "Set up inventory locations and then create products"
    seed_task = Task(
        id="task-host-multidomain-seed",
        context_id="ctx-host-multidomain-continue",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=seed_request)]),
        ),
    )
    seed_message = Message(role=Role.user, parts=[TextPart(text=seed_request)])
    seed_events = [event async for event in processor(seed_task, seed_message, None, None)]
    prompt_payload = next(
        event for event in seed_events if isinstance(event, Artifact) and event.name == "result"
    ).parts[0].data
    fake_langgraph_components.reset_fake_components()

    form_response = '{"type":"multiple_choice_response","selected":"continue_next","additional_input":null}'
    task = Task(
        id="task-host-multidomain-continue",
        context_id="ctx-host-multidomain-continue",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=form_response)]),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text=seed_request)]),
            Message(role=Role.agent, parts=[DataPart(data=prompt_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=form_response)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "delegate_to_agent",
            {
                "request": (
                    "Continue the user's multi-domain workflow.\n"
                    "Original user request: Set up inventory locations and then create products\n"
                    "Completed steps so far: Inventory Management.\n"
                    "Latest completed step result: Inventory locations created successfully.\n"
                    "Focus now on the Product Management part of the workflow.\n"
                    "Use structured interactions if more information is still required."
                ),
                "agent_name": "product",
            },
        ),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "product"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == "Initial products created successfully."


@pytest.mark.asyncio
async def test_host_inventory_confirmation_stays_with_inventory_specialist() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    request = "Group my inventories into categories and assign items"
    task = Task(
        id="task-host-inventory-confirm",
        context_id="ctx-host-inventory-confirm",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=request)]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        ("delegate_to_agent", {"request": request, "agent_name": "inventory"}),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "inventory"
    assert delegation_artifact.parts[0].data["finalState"] == "input-required"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert (
        _text_from_parts(result_artifact.parts)
        == "I reviewed the inventory items and prepared a category plan. Once you confirm, I'll create the categories and assign the items."
    )

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_host_inventory_confirmation_follow_up_routes_back_to_same_specialist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_CONTEXT_MEMORY_STORE", "memory")
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    seed_request = "Group my inventories into categories and assign items"
    seed_task = Task(
        id="task-host-inventory-confirm-seed",
        context_id="ctx-host-inventory-confirm-follow-up",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=seed_request)]),
        ),
    )
    seed_message = Message(role=Role.user, parts=[TextPart(text=seed_request)])
    seed_events = [event async for event in processor(seed_task, seed_message, None, None)]
    seed_result_text = _text_from_parts(
        next(event for event in seed_events if isinstance(event, Artifact) and event.name == "result").parts
    )
    fake_langgraph_components.reset_fake_components()

    request = "Yes, proceed."
    task = Task(
        id="task-host-inventory-confirm-follow-up",
        context_id="ctx-host-inventory-confirm-follow-up",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=request)]),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text=seed_request)]),
            Message(role=Role.agent, parts=[TextPart(text=seed_result_text)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "delegate_to_agent",
            {
                "request": request,
                "agent_name": "inventory",
                "delegated_task_id": "delegated-inventory-categorize",
            },
        ),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "inventory"
    assert delegation_artifact.parts[0].data["finalState"] == "completed"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == "Created 3 inventory categories and assigned 14 inventory items."

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.completed


@pytest.mark.asyncio
async def test_inventory_router_auto_delegates_to_inventory_setup_subspecialist() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory")
    task = Task(
        id="task-router-inventory-create",
        context_id="ctx-router-inventory-create",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="I want you to create an inventory for me")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="I want you to create an inventory for me")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "delegate_to_agent",
            {
                "request": "I want you to create an inventory for me",
                "agent_name": "inventory_setup",
            },
        ),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "inventory_setup"


@pytest.mark.asyncio
async def test_inventory_router_rewrites_comprehensive_business_review_for_visibility_specialist() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory")
    request = (
        "Continue the user's multi-domain business review.\n"
        "Original user request: Analyze my business performance for the last quarter\n"
        "Completed steps so far: Point of Sale (POS).\n"
        "Latest completed step result: Sales performance for the requested review period is ready.\n"
        "Time range to use: last quarter (2026-04-01 to 2026-06-30).\n"
        "Run the full Inventory Management portion of the business review for that same time range.\n"
        "Cover all of these in one response: stock posture and availability; turnover and velocity; reorder recommendations; ageing and expiry analysis; valuation and carrying cost; fulfillment and reservation issues; and procurement or receiving signals that affect stock health.\n"
        "Use defaults without asking the user to choose a single focus: include all locations, provide both company-wide and per-location outputs, use POS sales from earlier steps as the demand signal where helpful, and only include lot or expiry-aware analysis if the workspace tracks it.\n"
        "Do not send a focus picker, a default-confirmation checklist, or any retry prompt unless required permissions or source data are truly unavailable."
    )
    task = Task(
        id="task-router-inventory-business-review",
        context_id="ctx-router-inventory-business-review",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=request)]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("list_available_agents", {}),
        (
            "delegate_to_agent",
            {
                "request": (
                    "Run a comprehensive inventory health review for the requested time range.\n"
                    "Original request: Continue the user's multi-domain business review.\n"
                    "Original user request: Analyze my business performance for the last quarter\n"
                    "Completed steps so far: Point of Sale (POS).\n"
                    "Latest completed step result: Sales performance for the requested review period is ready.\n"
                    "Time range to use: last quarter (2026-04-01 to 2026-06-30).\n"
                    "Run the full Inventory Management portion of the business review for that same time range.\n"
                    "Cover all of these in one response: stock posture and availability; turnover and velocity; reorder recommendations; ageing and expiry analysis; valuation and carrying cost; fulfillment and reservation issues; and procurement or receiving signals that affect stock health.\n"
                    "Use defaults without asking the user to choose a single focus: include all locations, provide both company-wide and per-location outputs, use POS sales from earlier steps as the demand signal where helpful, and only include lot or expiry-aware analysis if the workspace tracks it.\n"
                    "Do not send a focus picker, a default-confirmation checklist, or any retry prompt unless required permissions or source data are truly unavailable.\n"
                    "Time range to use: last quarter (2026-04-01 to 2026-06-30).\n"
                    "Cover all of these in one consolidated response: stock posture and availability; turnover and velocity; reorder recommendations; ageing and expiry analysis; valuation and carrying cost; fulfillment and reservation issues; and procurement or receiving signals that affect stock health.\n"
                    "Use defaults without asking the user to choose a focus: include all locations, provide both company-wide and per-location outputs, use established POS demand signals where helpful, and only include lot or expiry-aware analysis if the workspace tracks it.\n"
                    "Do not send a focus picker, a default-confirmation checklist, or a retry-with-defaults prompt unless access is genuinely blocked."
                ),
                "agent_name": "inventory_visibility",
            },
        ),
    ]

    delegation_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "delegation")
    assert delegation_artifact.parts[0].data["selectedAgent"] == "inventory_visibility"

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert "Comprehensive inventory health is ready." in _text_from_parts(result_artifact.parts)


@pytest.mark.asyncio
async def test_inventory_setup_rewrites_relation_text_fields_to_select_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KA2A_LLM_FACTORY", "tests.fake_langgraph_components:fake_relation_interaction_llm_factory")

    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_setup")
    request_text = "help me create an inventory item called Main Inventory"
    task = Task(
        id="task-relation-form",
        context_id="ctx-relation-form",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=request_text)]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request_text)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("inventory.list_inventory_categories", {"query": "", "limit": 25}),
        (
            "create_dynamic_form",
            {
                "title": "Create Inventory Item",
                "description": "I translated your request into an inventory setup form. Confirm or edit the details before I create anything. Category is optional and will be left blank unless you choose one.",
                "fields": [
                    {
                        "name": "default_inventory_name",
                        "type": "text",
                        "label": "Inventory Name",
                        "required": True,
                        "placeholder": "Main Inventory",
                    },
                    {
                        "name": "inventory_description",
                        "type": "textarea",
                        "label": "Inventory Description",
                        "required": False,
                        "placeholder": "Primary stock ledger for the business.",
                    },
                    {
                        "name": "inventory_category_id",
                        "type": "select",
                        "label": "Inventory Category",
                        "required": False,
                        "options": [
                            {"value": "cat-1", "label": "Men's Clothes", "description": "Menswear"},
                            {"value": "cat-2", "label": "Shoes", "description": "Footwear"},
                        ],
                        "placeholder": "Select a category if this inventory should belong to one",
                    },
                ],
            },
        ),
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["interaction_type"] == "dynamic_form"
    assert payload["mutation_action"] == "create_inventory_item"
    assert "IDs" not in payload["description"]

    fields = {field["name"]: field for field in payload["fields"]}
    assert fields["inventory_category_id"]["type"] == "select"
    assert fields["inventory_category_id"]["options"][0]["label"] == "Men's Clothes"
    assert fields["inventory_category_id"]["options"][0]["value"] == "cat-1"


@pytest.mark.asyncio
async def test_inventory_setup_descriptive_create_request_opens_prefilled_dynamic_form() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_setup")
    task = Task(
        id="task-inventory-setup-create-form",
        context_id="ctx-inventory-setup-create-form",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[
                    TextPart(
                        text=(
                            "Create a new inventory.\n"
                            "Inventory name: Fashion Master Inventory\n"
                            "Description: Primary stock ledger for apparel.\n"
                            "Inventory category: Men's Clothes"
                        )
                    )
                ],
            ),
        ),
    )
    message = Message(
        role=Role.user,
        parts=[
            TextPart(
                text=(
                    "Create a new inventory.\n"
                    "Inventory name: Fashion Master Inventory\n"
                    "Description: Primary stock ledger for apparel.\n"
                    "Inventory category: Men's Clothes"
                )
            )
        ],
    )

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "inventory.list_inventory_categories",
        "create_dynamic_form",
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["interaction_type"] == "dynamic_form"
    assert payload["workflow"] == "inventory_setup_mutation"
    assert payload["workflow_stage"] == "form"
    assert payload["mutation_action"] == "create_inventory_item"
    assert payload["current_values"] == {
        "default_inventory_name": "Fashion Master Inventory",
        "inventory_description": "Primary stock ledger for apparel.",
        "inventory_category_id": "cat-1",
    }

    fields = {field["name"]: field for field in payload["fields"]}
    assert fields["inventory_category_id"]["type"] == "select"
    assert fields["inventory_category_id"]["options"][0]["label"] == "Men's Clothes"


@pytest.mark.asyncio
async def test_inventory_setup_location_request_prefills_name_type_and_parent() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_setup")
    request = "Create a stock location called Bulb Display Shelf with location type shelf under Main Warehouse."
    task = Task(
        id="task-inventory-setup-location-form",
        context_id="ctx-inventory-setup-location-form",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=request)])),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    payload = next(event for event in events if isinstance(event, Artifact) and event.name == "result").parts[0].data
    assert payload["mutation_action"] == "create_stock_location"
    assert payload["current_values"] == {
        "location_name": "Bulb Display Shelf",
        "location_type_name": "Shelf",
        "parent_id": "loc-1",
    }


@pytest.mark.asyncio
async def test_inventory_setup_inventory_item_request_prefills_name_category_and_description() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_setup")
    request = (
        "Create an inventory item called LED Bulb Stock in category Men's Clothes "
        "at Main Warehouse with description for boxed LED lighting inventory."
    )
    task = Task(
        id="task-inventory-setup-item-form",
        context_id="ctx-inventory-setup-item-form",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=request)])),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    payload = next(event for event in events if isinstance(event, Artifact) and event.name == "result").parts[0].data
    assert payload["mutation_action"] == "create_inventory_item"
    assert payload["current_values"] == {
        "default_inventory_name": "LED Bulb Stock",
        "inventory_description": "for boxed LED lighting inventory",
        "inventory_category_id": "cat-1",
    }


@pytest.mark.asyncio
async def test_inventory_setup_list_categories_query_uses_direct_lookup() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_setup")
    request = "List the inventory categories for my store."
    task = Task(
        id="task-inventory-setup-list-categories",
        context_id="ctx-inventory-setup-list-categories",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=request)])),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        ("inventory.list_inventory_categories", {}),
    ]
    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert "Men's Clothes" in result_artifact.parts[0].text


@pytest.mark.asyncio
async def test_inventory_setup_form_response_executes_inventory_create() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_setup")
    form_payload = {
        "interaction_type": "dynamic_form",
        "title": "Create Inventory Item",
        "description": "Confirm the inventory details before I create anything.",
        "fields": [],
        "workflow": "inventory_setup_mutation",
        "workflow_stage": "form",
        "mutation_action": "create_inventory_item",
    }
    form_response = (
        '{"type":"form_response","data":{"default_inventory_name":"Fashion Master Inventory",'
        '"inventory_description":"Primary stock ledger for apparel.","inventory_category_id":"cat-1"},'
        '"message":"Form submitted successfully"}'
    )
    task = Task(
        id="task-inventory-setup-form-submit",
        context_id="ctx-inventory-setup-form-submit",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=form_response)]),
        ),
        history=[
            Message(role=Role.user, parts=[TextPart(text="Create a new inventory")]),
            Message(role=Role.agent, parts=[DataPart(data=form_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=form_response)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "inventory.create_inventory_item",
            {
                "payload": {
                    "name": "Fashion Master Inventory",
                    "description": "Primary stock ledger for apparel.",
                    "category_id": "cat-1",
                }
            },
        )
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].text == "Inventory item created successfully."
    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.completed


@pytest.mark.asyncio
async def test_inventory_setup_executes_approved_onboarding_creation_request_without_reconfirming() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_setup")
    onboarding_data = {
        "scope": "full_setup",
        "flat": {
            "primary_location_name": "Main Warehouse",
            "primary_location_type": "warehouse",
            "additional_locations": "Front Store\nReturns Shelf",
            "category_names": "Beverages\nSnacks",
            "default_inventory_name": "Main Inventory",
        },
    }
    request_text = _onboarding_creation_request("full_setup", onboarding_data)
    task = Task(
        id="task-inventory-setup-approved-onboarding",
        context_id="ctx-inventory-setup-approved-onboarding",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text=request_text)]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request_text)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "delegate_to_agent",
            {
                "request": request_text,
                "agent_name": "onboarding",
            },
        )
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == (
        "Created 3 stock locations, 2 inventory categories, and 1 inventory item for onboarding."
    )
    assert not any(
        isinstance(event, Artifact)
        and event.name == "result"
        and isinstance(event.parts[0], DataPart)
        and event.parts[0].data.get("interaction_type") == "dynamic_form"
        for event in events
    )


@pytest.mark.asyncio
async def test_inventory_setup_parent_update_request_opens_prefilled_form() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_setup")
    task = Task(
        id="task-inventory-setup-parent-form",
        context_id="ctx-inventory-setup-parent-form",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(
                role=Role.user,
                parts=[TextPart(text="Set parent of Front Store to Main Warehouse")],
            ),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="Set parent of Front Store to Main Warehouse")])

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "inventory.list_stock_locations",
        "create_dynamic_form",
    ]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    payload = result_artifact.parts[0].data
    assert payload["mutation_action"] == "update_stock_location_parent"
    assert payload["current_values"] == {
        "location_id": "loc-2",
        "parent_id": "loc-1",
    }
    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_product_catalog_admin_descriptive_create_request_opens_prefilled_form() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="product_catalog_admin")
    request = (
        "Create a new product.\n"
        "Product name: Men's Oxford Shirt\n"
        "Description: Long-sleeve formal shirt.\n"
        "Category: Apparel\n"
        "Base price: 25000\n"
        "Enable quick sale"
    )
    task = Task(
        id="task-product-create-form",
        context_id="ctx-product-create-form",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=request)])),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "product.get_product_categories",
        "product.search_products",
        "create_dynamic_form",
    ]
    payload = next(event for event in events if isinstance(event, Artifact) and event.name == "result").parts[0].data
    assert payload["workflow"] == "product_catalog_admin_mutation"
    assert payload["mutation_action"] == "create_product"
    assert payload["current_values"] == {
        "name": "Men's Oxford Shirt",
        "description": "Long-sleeve formal shirt.",
        "category_ref_id": "prod-cat-1",
        "base_price": "25000",
        "quick_sale": True,
    }


@pytest.mark.asyncio
async def test_product_catalog_admin_form_response_executes_update_product() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="product_catalog_admin")
    form_payload = {
        "interaction_type": "dynamic_form",
        "workflow": "product_catalog_admin_mutation",
        "workflow_stage": "form",
        "mutation_action": "update_product",
    }
    form_response = (
        '{"type":"form_response","data":{"product_id":"prod-1","name":"Men\'s Oxford Shirt","category_ref_id":"prod-cat-1","base_price":"26000","quick_sale":true},'
        '"message":"Form submitted successfully"}'
    )
    task = Task(
        id="task-product-update-form-submit",
        context_id="ctx-product-update-form-submit",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=form_response)])),
        history=[
            Message(role=Role.user, parts=[TextPart(text="Update the Men's Oxford Shirt product")]),
            Message(role=Role.agent, parts=[DataPart(data=form_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=form_response)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "product.update_product",
            {
                "product_id": "prod-1",
                "payload": {
                    "name": "Men's Oxford Shirt",
                    "category_ref_id": "prod-cat-1",
                    "base_price": "26000",
                    "quick_sale": True,
                },
            },
        )
    ]
    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].text == "Product updated successfully."


@pytest.mark.asyncio
async def test_inventory_fulfillment_transfer_request_opens_prefilled_form() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_fulfillment")
    request = "Transfer Men's Oxford Shirt Inventory from Main Warehouse to Front Store quantity 12"
    task = Task(
        id="task-fulfillment-transfer-form",
        context_id="ctx-fulfillment-transfer-form",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=request)])),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "inventory.list_stock_locations",
        "inventory.list_inventory_items",
        "create_dynamic_form",
    ]
    payload = next(event for event in events if isinstance(event, Artifact) and event.name == "result").parts[0].data
    assert payload["workflow"] == "inventory_fulfillment_mutation"
    assert payload["mutation_action"] == "transfer_location_stock"
    assert payload["current_values"] == {
        "inventory_item_id": "inv-1",
        "from_location_id": "loc-1",
        "to_location_id": "loc-2",
        "quantity": "12",
    }


@pytest.mark.asyncio
async def test_inventory_fulfillment_reservation_request_opens_prefilled_form() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_fulfillment")
    request = "Create a stock reservation for 2 units of Men's Oxford Shirt Inventory at Main Warehouse for showroom transfer."
    task = Task(
        id="task-fulfillment-reservation-form",
        context_id="ctx-fulfillment-reservation-form",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=request)])),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    payload = next(event for event in events if isinstance(event, Artifact) and event.name == "result").parts[0].data
    assert payload["workflow"] == "inventory_fulfillment_mutation"
    assert payload["mutation_action"] == "create_stock_reservation"
    assert payload["current_values"] == {
        "inventory_item_id": "inv-1",
        "stock_location_id": "loc-1",
        "quantity": "2",
        "external_order_type": "sales_order",
        "external_order_id": "showroom-transfer",
        "notes": "Showroom transfer reservation",
    }


@pytest.mark.asyncio
async def test_inventory_fulfillment_form_response_executes_adjustment() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_fulfillment")
    form_payload = {
        "interaction_type": "dynamic_form",
        "workflow": "inventory_fulfillment_mutation",
        "workflow_stage": "form",
        "mutation_action": "adjust_inventory_item_stock",
    }
    form_response = (
        '{"type":"form_response","data":{"inventory_item_id":"inv-1","stock_location_id":"loc-1","quantity":"5","adjustment_type":"add","reason":"Cycle count","notes":"Top-up after count"},'
        '"message":"Form submitted successfully"}'
    )
    task = Task(
        id="task-fulfillment-adjust-form-submit",
        context_id="ctx-fulfillment-adjust-form-submit",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=form_response)])),
        history=[
            Message(role=Role.user, parts=[TextPart(text="Add 5 units to Men's Oxford Shirt Inventory at Main Warehouse")]),
            Message(role=Role.agent, parts=[DataPart(data=form_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=form_response)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "inventory.adjust_inventory_item_stock",
            {
                "inventory_item_id": "inv-1",
                "payload": {
                    "adjustments": [
                        {
                            "inventory_item_id": "inv-1",
                            "stock_location_id": "loc-1",
                            "quantity": "5",
                            "adjustment_type": "add",
                            "notes": "Top-up after count",
                        }
                    ],
                    "reason": "Cycle count",
                    "notes": "Top-up after count",
                },
            },
        )
    ]
    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].text == "Inventory adjustment completed successfully."


@pytest.mark.asyncio
async def test_inventory_fulfillment_form_response_executes_reservation_create() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_fulfillment")
    form_payload = {
        "interaction_type": "dynamic_form",
        "workflow": "inventory_fulfillment_mutation",
        "workflow_stage": "form",
        "mutation_action": "create_stock_reservation",
    }
    form_response = (
        '{"type":"form_response","data":{"inventory_item_id":"inv-1","stock_location_id":"loc-1","quantity":"2",'
        '"external_order_type":"sales_order","external_order_id":"showroom-transfer","external_order_line_id":"line-1","notes":"Showroom transfer reservation"},'
        '"message":"Form submitted successfully"}'
    )
    task = Task(
        id="task-fulfillment-reservation-form-submit",
        context_id="ctx-fulfillment-reservation-form-submit",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=form_response)])),
        history=[
            Message(role=Role.user, parts=[TextPart(text="Reserve 2 units for showroom transfer")]),
            Message(role=Role.agent, parts=[DataPart(data=form_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=form_response)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "inventory.create_stock_reservation",
            {
                "payload": {
                    "inventory_item_id": "inv-1",
                    "stock_location_id": "loc-1",
                    "reserved_quantity": "2",
                    "external_order_type": "sales_order",
                    "external_order_id": "showroom-transfer",
                    "external_order_line_id": "line-1",
                    "notes": "Showroom transfer reservation",
                }
            },
        )
    ]
    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].text == "Stock reservation created successfully."


@pytest.mark.asyncio
async def test_inventory_fulfillment_form_response_preserves_structural_scope_from_selected_location() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_fulfillment")
    form_payload = {
        "interaction_type": "dynamic_form",
        "workflow": "inventory_fulfillment_mutation",
        "workflow_stage": "form",
        "mutation_action": "create_stock_reservation",
        "fields": [
            {
                "name": "stock_location_id",
                "type": "select",
                "options": [
                    {
                        "value": "loc-1",
                        "label": "Main Warehouse",
                        "metadata": {
                            "structural_location_id": "struct-1",
                            "structural_location_name": "Main Warehouse",
                        },
                    }
                ],
            }
        ],
    }
    form_response = (
        '{"type":"form_response","data":{"inventory_item_id":"inv-1","stock_location_id":"loc-1","quantity":"2",'
        '"external_order_type":"sales_order","external_order_id":"showroom-transfer","external_order_line_id":"line-1","notes":"Showroom transfer reservation"},'
        '"message":"Form submitted successfully"}'
    )
    task = Task(
        id="task-fulfillment-reservation-form-submit-structural-scope",
        context_id="ctx-fulfillment-reservation-form-submit-structural-scope",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=form_response)])),
        history=[
            Message(role=Role.user, parts=[TextPart(text="Reserve 2 units for showroom transfer")]),
            Message(role=Role.agent, parts=[DataPart(data=form_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=form_response)])

    _ = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "inventory.create_stock_reservation",
            {
                "payload": {
                    "inventory_item_id": "inv-1",
                    "stock_location_id": "loc-1",
                    "structural_location_id": "struct-1",
                    "reserved_quantity": "2",
                    "external_order_type": "sales_order",
                    "external_order_id": "showroom-transfer",
                    "external_order_line_id": "line-1",
                    "notes": "Showroom transfer reservation",
                }
            },
        )
    ]


@pytest.mark.asyncio
async def test_inventory_procurement_request_opens_prefilled_form() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_procurement")
    request = (
        "Add Men's Oxford Shirt Inventory to purchase order PO-1001\n"
        "Quantity: 20\n"
        "Unit price: 15000"
    )
    task = Task(
        id="task-procurement-line-item-form",
        context_id="ctx-procurement-line-item-form",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=request)])),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "inventory.search_purchase_orders",
        "inventory.list_inventory_items",
        "create_dynamic_form",
    ]
    payload = next(event for event in events if isinstance(event, Artifact) and event.name == "result").parts[0].data
    assert payload["workflow"] == "inventory_procurement_mutation"
    assert payload["mutation_action"] == "add_purchase_order_line_item"
    assert payload["current_values"] == {
        "purchase_order_id": "po-1",
        "inventory_item_id": "inv-1",
        "quantity": "20",
        "unit_price": "15000",
    }


@pytest.mark.asyncio
async def test_inventory_procurement_form_response_executes_line_item_addition() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory_procurement")
    form_payload = {
        "interaction_type": "dynamic_form",
        "workflow": "inventory_procurement_mutation",
        "workflow_stage": "form",
        "mutation_action": "add_purchase_order_line_item",
    }
    form_response = (
        '{"type":"form_response","data":{"purchase_order_id":"po-1","inventory_item_id":"inv-1","quantity":"20","unit_price":"15000","description":"Opening buy"},'
        '"message":"Form submitted successfully"}'
    )
    task = Task(
        id="task-procurement-line-item-submit",
        context_id="ctx-procurement-line-item-submit",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=form_response)])),
        history=[
            Message(role=Role.user, parts=[TextPart(text="Add a line item to PO-1001")]),
            Message(role=Role.agent, parts=[DataPart(data=form_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=form_response)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "inventory.add_purchase_order_line_item",
            {
                "purchase_order_id": "po-1",
                "payload": {
                    "inventory_item_id": "inv-1",
                    "quantity": "20",
                    "unit_price": "15000",
                    "description": "Opening buy",
                },
            },
        )
    ]
    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].text == "Purchase-order line item added successfully."


@pytest.mark.asyncio
async def test_product_merchandising_request_opens_prefilled_form() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="product_merchandising")
    request = (
        "Update merchandising for product Men's Oxford Shirt\n"
        "Category: Apparel\n"
        "Enable quick sale\n"
        "Mark as featured\n"
        "POS Category: Clothing"
    )
    task = Task(
        id="task-product-merchandising-form",
        context_id="ctx-product-merchandising-form",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=request)])),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "product.search_products",
        "product.get_product_categories",
        "create_dynamic_form",
    ]
    payload = next(event for event in events if isinstance(event, Artifact) and event.name == "result").parts[0].data
    assert payload["workflow"] == "product_merchandising_mutation"
    assert payload["mutation_action"] == "update_product_merchandising"
    assert payload["current_values"] == {
        "product_id": "prod-1",
        "category_ref_id": "prod-cat-1",
        "quick_sale": True,
        "is_featured": True,
        "pos_category": "Clothing",
    }


@pytest.mark.asyncio
async def test_product_merchandising_form_response_executes_update() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="product_merchandising")
    form_payload = {
        "interaction_type": "dynamic_form",
        "workflow": "product_merchandising_mutation",
        "workflow_stage": "form",
        "mutation_action": "update_product_merchandising",
    }
    form_response = (
        '{"type":"form_response","data":{"product_id":"prod-1","category_ref_id":"prod-cat-1","quick_sale":true,"is_featured":true,"pos_category":"Clothing"},'
        '"message":"Form submitted successfully"}'
    )
    task = Task(
        id="task-product-merchandising-submit",
        context_id="ctx-product-merchandising-submit",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=form_response)])),
        history=[
            Message(role=Role.user, parts=[TextPart(text="Update the product merchandising")]),
            Message(role=Role.agent, parts=[DataPart(data=form_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=form_response)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "product.update_product",
            {
                "product_id": "prod-1",
                "payload": {
                    "category_ref_id": "prod-cat-1",
                    "quick_sale": True,
                    "is_featured": True,
                    "pos_category": "Clothing",
                },
            },
        )
    ]
    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].text == "Product merchandising updated successfully."


@pytest.mark.asyncio
async def test_product_pricing_strategy_request_opens_prefilled_form() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="product_pricing")
    request = (
        "Strategy name: Fashion Margin Strategy\n"
        "Product: Men's Oxford Shirt\n"
        "Margin: 25"
    )
    task = Task(
        id="task-product-pricing-strategy-form",
        context_id="ctx-product-pricing-strategy-form",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=request)])),
    )
    message = Message(role=Role.user, parts=[TextPart(text=request)])

    events = [event async for event in processor(task, message, None, None)]

    assert [name for name, _ in fake_langgraph_components.FAKE_TOOL_CALLS] == [
        "product.search_products",
        "product.get_product_categories",
        "create_dynamic_form",
    ]
    payload = next(event for event in events if isinstance(event, Artifact) and event.name == "result").parts[0].data
    assert payload["workflow"] == "product_pricing_mutation"
    assert payload["mutation_action"] == "create_pricing_strategy"
    assert payload["current_values"] == {
        "name": "Fashion Margin Strategy",
        "strategy": "margin",
        "product_id": "prod-1",
        "margin_percentage": "25",
    }


@pytest.mark.asyncio
async def test_product_pricing_rule_form_response_executes_create_rule() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="product_pricing")
    form_payload = {
        "interaction_type": "dynamic_form",
        "workflow": "product_pricing_mutation",
        "workflow_stage": "form",
        "mutation_action": "create_pricing_rule",
    }
    form_response = (
        '{"type":"form_response","data":{"name":"Weekend Promo","rule_type":"PROMO","product_id":"prod-1","category_ref_id":"prod-cat-1","discount_type":"PERCENTAGE","value":"10","description":"Weekend markdown"},'
        '"message":"Form submitted successfully"}'
    )
    task = Task(
        id="task-product-pricing-rule-submit",
        context_id="ctx-product-pricing-rule-submit",
        status=TaskStatus(state=TaskState.submitted, message=Message(role=Role.user, parts=[TextPart(text=form_response)])),
        history=[
            Message(role=Role.user, parts=[TextPart(text="Create a pricing rule")]),
            Message(role=Role.agent, parts=[DataPart(data=form_payload)]),
        ],
    )
    message = Message(role=Role.user, parts=[TextPart(text=form_response)])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_TOOL_CALLS == [
        (
            "product.create_pricing_rule",
            {
                "payload": {
                    "name": "Weekend Promo",
                    "rule_type": "PROMO",
                    "product_id": "prod-1",
                    "category_ref_id": "prod-cat-1",
                    "discount_type": "PERCENTAGE",
                    "value": "10",
                    "description": "Weekend markdown",
                },
            },
        )
    ]
    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].text == "Pricing rule created successfully."


@pytest.mark.asyncio
async def test_inventory_agent_greeting_short_circuits_tool_and_llm_work() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="inventory")
    task = Task(
        id="task-greeting-inventory",
        context_id="ctx-greeting-inventory",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="hello there")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="hello there")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == []

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].text == "I’m your Inventory Management agent. What can I help you with?"


@pytest.mark.asyncio
async def test_host_status_check_short_circuits_tool_and_llm_work() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="host")
    task = Task(
        id="task-status-host",
        context_id="ctx-status-host",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="hello again, are you there?")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="hello again, are you there?")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 0
    assert fake_langgraph_components.FAKE_TOOL_CALLS == []

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert result_artifact.parts[0].text == "I’m your workspace host agent. What can I help you with?"


@pytest.mark.asyncio
async def test_specialist_interaction_payload_yields_input_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KA2A_TOOLS_ENABLED", "false")
    monkeypatch.setenv("KA2A_LLM_FACTORY", "tests.fake_langgraph_components:fake_interaction_llm_factory")

    processor = make_langgraph_chat_processor_from_env(agent_name="product")
    task = Task(
        id="task-3",
        context_id="ctx-3",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="help me pick inventory")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="help me pick inventory")])

    events = [event async for event in processor(task, message, None, None)]

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert "interaction_type" in _text_from_parts(result_artifact.parts)

    status_events = [event for event in events if isinstance(event, TaskStatus)]
    assert status_events[-1].state == TaskState.input_required


@pytest.mark.asyncio
async def test_specialist_tool_loop_passes_tool_specs_to_model() -> None:
    processor = make_langgraph_chat_processor_from_env(agent_name="product")
    task = Task(
        id="task-4",
        context_id="ctx-4",
        status=TaskStatus(
            state=TaskState.submitted,
            message=Message(role=Role.user, parts=[TextPart(text="summarize your available tooling")]),
        ),
    )
    message = Message(role=Role.user, parts=[TextPart(text="summarize your available tooling")])

    events = [event async for event in processor(task, message, None, None)]

    assert fake_langgraph_components.FAKE_LLM_CALL_COUNT == 1
    assert set(fake_langgraph_components.FAKE_LLM_LAST_TOOLS) == {
        "list_available_agents",
        "delegate_to_agent",
        "create_multiple_choice",
        "create_wizard_flow",
        "create_dynamic_form",
    }

    result_artifact = next(event for event in events if isinstance(event, Artifact) and event.name == "result")
    assert _text_from_parts(result_artifact.parts) == "This should not be used for delegated host requests."
