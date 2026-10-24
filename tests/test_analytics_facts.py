from __future__ import annotations

from kafka_a2a.analytics_facts import (
    FACT_PACKET_KEY,
    answer_follow_up_from_fact_packet,
    attach_fact_packet,
    build_fact_packet,
    resolve_analytic_follow_up_frame,
    normalize_insight_response_contract,
    validate_analytic_selection,
    validate_fact_packet,
    validate_insight_response_contract,
)


def test_normalize_insight_response_removes_exact_duplicate_widgets_only() -> None:
    payload = {
        "kind": "insight_response",
        "summary": "Stock review",
        "widgets": [
            {"type": "metric_grid", "title": "Stock risk", "data": [{"label": "Out of stock", "value": 2}]},
            {"type": "metric_grid", "title": "Stock risk", "data": [{"label": "Out of stock", "value": 2}]},
            {"type": "metric_grid", "title": "Stock risk", "data": [{"label": "Out of stock", "value": 3}]},
        ],
    }

    normalized = normalize_insight_response_contract(payload=payload)

    assert len(normalized["widgets"]) == 2
    assert normalized["widgets"][1]["data"][0]["value"] == 3
    assert normalized["warnings"] == ["Removed 1 exact duplicate insight widget(s)."]


def _location_sales_payload() -> dict:
    return {
        "kind": "insight_response",
        "currency_code": "NGN",
        "timeframe": {"label": "last 3 months", "start_date": "2026-07-01", "end_date": "2026-09-30"},
        "data_sources": [{"service": "pos", "tool": "get_sales_summary"}],
        "widgets": [
            {
                "type": "comparison_table",
                "title": "Location contribution for last 3 months",
                "rows": [
                    {"location": "Maitama", "sales": 9000, "orders": 12},
                    {"location": "Agric", "sales": 2500, "orders": 4},
                ],
            }
        ],
    }


def test_inventory_scope_metric_grid_is_not_location_sales_evidence() -> None:
    payload = _location_sales_payload()
    payload["widgets"].insert(
        0,
        {
            "type": "metric_grid",
            "title": "Inventory risk posture - All locations",
            "data": [
                {"label": "Out of Stock", "value": 2},
                {"label": "Needs Reorder", "value": 0},
            ],
        },
    )

    packet = build_fact_packet(user_text="Analyze business performance", payload=payload)

    assert packet is not None
    location_sales = [
        fact
        for fact in packet["facts"]
        if fact.get("dimension") == "location" and fact.get("metric") == "sales_revenue"
    ]
    assert {fact["entity_label"] for fact in location_sales} == {"Maitama", "Agric"}


def test_legacy_inventory_scope_facts_cannot_answer_location_sales_ranking() -> None:
    packet = {
        "timeframe": {"label": "last quarter"},
        "query_frame": {"entity": "location", "metric": "sales_revenue"},
        "facts": [
            {
                "dimension": "location",
                "entity_label": "Needs Reorder",
                "metric": "sales_revenue",
                "value": 0,
                "unit": "currency",
                "currency_code": "NGN",
                "source_widget": "Inventory risk posture - All locations",
                "coverage": "complete",
            },
            {
                "dimension": "location",
                "entity_label": "Agric",
                "metric": "sales_revenue",
                "value": 2500,
                "unit": "currency",
                "currency_code": "NGN",
                "source_widget": "Location contribution for last quarter",
                "coverage": "complete",
            },
        ],
    }

    answer = answer_follow_up_from_fact_packet(
        user_text="Which location had the lowest sales?",
        packet=packet,
    )

    assert answer is not None
    assert "Agric" in answer
    assert "Needs Reorder" not in answer


def test_fact_packet_answers_location_ranking_from_frozen_evidence() -> None:
    packet = build_fact_packet(user_text="Analyze sales for the last 3 months", payload=_location_sales_payload())

    assert packet is not None
    assert validate_fact_packet(packet=packet)["valid"] is True
    answer = answer_follow_up_from_fact_packet(
        user_text="What is the location with the lowest sales?",
        packet=packet,
    )

    assert answer is not None
    assert "Agric" in answer
    assert "₦2,500.00" in answer
    assert "Maitama" in answer


def test_fact_packet_resolves_unambiguous_elliptical_follow_up_from_prior_frame() -> None:
    packet = build_fact_packet(
        user_text="Show sales by location for the last 3 months",
        payload=_location_sales_payload(),
    )

    assert packet is not None
    frame = resolve_analytic_follow_up_frame(user_text="Which one sold least?", packet=packet)
    answer = answer_follow_up_from_fact_packet(user_text="Which one sold least?", packet=packet)

    assert frame["entity"] == "location"
    assert frame["metric"] == "sales_revenue"
    assert answer is not None
    assert "Agric" in answer


def test_analytic_selection_rejects_the_opposite_ranking_direction() -> None:
    candidates = [
        {"entity_label": "Maitama", "value": 9000},
        {"entity_label": "Agric", "value": 2500},
    ]

    result = validate_analytic_selection(
        frame={"ranking_direction": "minimum"},
        candidates=candidates,
        selected=[candidates[0]],
    )

    assert result["valid"] is False
    assert "ranking direction" in str(result["reason"])


def test_fact_packet_does_not_guess_an_elliptical_follow_up_with_multiple_dimensions() -> None:
    packet = {
        "query_frame": {"entity": "workspace", "metric": "sales_revenue"},
        "facts": [
            {"dimension": "location", "entity_label": "Maitama", "metric": "sales_revenue", "value": 9000},
            {"dimension": "product", "entity_label": "Eva Water", "metric": "sales_revenue", "value": 2500},
        ],
    }

    frame = resolve_analytic_follow_up_frame(user_text="Which one sold least?", packet=packet)

    assert frame["entity"] == "workspace"
    assert answer_follow_up_from_fact_packet(user_text="Which one sold least?", packet=packet) is None


def test_fact_packet_declines_minimum_from_a_truncated_top_seller_list() -> None:
    payload = {
        "kind": "insight_response",
        "currency_code": "NGN",
        "widgets": [
            {
                "type": "ranked_list",
                "title": "Top sellers for last 7 days",
                "items": [
                    {"product_name": "Eva Premium Water", "quantity_sold": 50, "sales_total": 10000},
                    {"product_name": "Coca-Cola", "quantity_sold": 30, "sales_total": 8000},
                ],
            }
        ],
    }
    packet = build_fact_packet(user_text="Show top sellers", payload=payload)

    assert packet is not None
    assert answer_follow_up_from_fact_packet(
        user_text="Which product had the lowest sales?",
        packet=packet,
    ) is None
    answer = answer_follow_up_from_fact_packet(
        user_text="Which product had the highest sales?",
        packet=packet,
    )
    assert answer is not None
    assert "Eva Premium Water" in answer


def test_attach_fact_packet_preserves_existing_widget_contract() -> None:
    payload = _location_sales_payload()
    enriched = attach_fact_packet(user_text="Analyze sales", payload=payload)

    assert enriched["widgets"] == payload["widgets"]
    assert FACT_PACKET_KEY in enriched
    assert enriched[FACT_PACKET_KEY]["schema_version"] == 1


def test_insight_response_contract_rejects_contradictory_review_coverage() -> None:
    result = validate_insight_response_contract(
        payload={
            "kind": "insight_response",
            "widgets": [],
            "review_coverage": {
                "status": "complete",
                "sources_attempted": 3,
                "sources_available": 2,
            },
        }
    )

    assert result["valid"] is False
    assert "marked complete" in result["errors"][0]


def test_insight_response_contract_downgrades_contradictory_review_coverage() -> None:
    payload = {
        "kind": "insight_response",
        "widgets": [],
        "confidence": "high",
        "review_coverage": {"status": "complete", "sources_attempted": 3, "sources_available": 2},
    }

    normalized = normalize_insight_response_contract(payload=payload)

    assert normalized["review_coverage"]["status"] == "partial"
    assert normalized["confidence"] == "low"
    assert "marked partial" in normalized["warnings"][0]
