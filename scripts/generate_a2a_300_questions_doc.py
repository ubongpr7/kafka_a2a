from __future__ import annotations

import json
from pathlib import Path

from a2a_eval_corpus import GENERATED_SCENARIOS, GENERATED_SUITES


SUITE_ORDER: list[tuple[str, str]] = [
    ("insight_pos_sales_location_10", "POS: Sales By Location"),
    ("insight_pos_top_sellers_10", "POS: Top Sellers"),
    ("insight_pos_payment_mix_10", "POS: Payment Mix"),
    ("insight_pos_terminal_cashier_10", "POS: Terminal And Cashier Performance"),
    ("insight_pos_sessions_orders_10", "POS: Sessions And Orders"),
    ("insight_pos_exceptions_10", "POS: Exceptions And Risk"),
    ("insight_inventory_out_of_stock_10", "Inventory: Out Of Stock"),
    ("insight_inventory_low_stock_10", "Inventory: Low Stock"),
    ("insight_inventory_movements_10", "Inventory: Stock Movements"),
    ("insight_inventory_location_health_10", "Inventory: Location Health"),
    ("insight_inventory_reorder_10", "Inventory: Reorder"),
    ("insight_inventory_adjustment_risk_10", "Inventory: Adjustment Risk"),
    ("insight_procurement_po_lifecycle_10", "Procurement: PO Lifecycle"),
    ("insight_procurement_receiving_10", "Procurement: Receiving"),
    ("insight_procurement_supplier_10", "Procurement: Supplier Performance"),
    ("insight_procurement_delay_exception_10", "Procurement: Delays And Exceptions"),
    ("insight_procurement_cost_variance_05", "Procurement: Cost Variance"),
    ("insight_audit_staff_activity_10", "Audit: Staff Activity"),
    ("insight_audit_product_activity_10", "Audit: Product Activity"),
    ("insight_audit_pos_activity_10", "Audit: POS Activity"),
    ("insight_audit_support_access_10", "Audit: Support Access"),
    ("insight_audit_permission_security_05", "Audit: Permission And Security"),
    ("insight_product_import_opportunities_10", "Product: Import Opportunities"),
    ("insight_product_variant_lookup_10", "Product: Variant Lookup"),
    ("insight_product_catalog_gaps_10", "Product: Catalog Gaps"),
    ("insight_product_duplicate_codes_10", "Product: Duplicate Codes"),
    ("insight_product_media_category_05", "Product: Media And Category Quality"),
    ("insight_subscription_usage_10", "Subscription: Usage"),
    ("insight_subscription_limits_pressure_10", "Subscription: Limit Pressure"),
    ("insight_host_cross_domain_ops_10", "Host: Cross-Domain Operations"),
    ("insight_host_location_comparison_10", "Host: Location Comparison"),
    ("insight_host_recommendations_05", "Host: Recommendations"),
]


def build_question_pack() -> list[dict[str, object]]:
    scenario_by_key = {scenario["key"]: scenario for scenario in GENERATED_SCENARIOS}
    pack: list[dict[str, object]] = []

    for suite_name, title in SUITE_ORDER:
        keys = GENERATED_SUITES[suite_name]
        questions = [scenario_by_key[key]["text"] for key in keys]
        for index, question in enumerate(questions, start=1):
            follow_ups = [
                questions[(index - 1 + offset) % len(questions)]
                for offset in range(1, min(4, len(questions)))
            ]
            pack.append(
                {
                    "id": f"{len(pack) + 1:03d}",
                    "suite": suite_name,
                    "section": title,
                    "question": question,
                    "follow_ups": follow_ups,
                }
            )
    if len(pack) != 300:
        raise RuntimeError(f"Expected 300 generated questions, found {len(pack)}.")
    return pack


def render_markdown(pack: list[dict[str, object]]) -> str:
    lines: list[str] = [
        "# A2A Insight 300 Questions",
        "",
        "Derived from the passing `insight_300` eval corpus. Every base question and follow-up below comes from suites the current A2A system already passes.",
        "",
    ]

    current_section = None
    for item in pack:
        section = str(item["section"])
        if section != current_section:
            current_section = section
            lines.extend([f"## {section}", ""])
        lines.append(f'{item["id"]}. {item["question"]}')
        lines.append("Ask next:")
        for follow_up in item["follow_ups"]:
            lines.append(f"- {follow_up}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    docs_path = root / "docs" / "a2a-insight-300-questions.md"
    json_path = root / "output" / "a2a-insight-300-questions.json"

    pack = build_question_pack()
    docs_path.write_text(render_markdown(pack), encoding="utf-8")
    json_path.write_text(json.dumps(pack, indent=2), encoding="utf-8")

    print(docs_path)
    print(json_path)


if __name__ == "__main__":
    main()
