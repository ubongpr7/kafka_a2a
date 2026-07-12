from __future__ import annotations

from typing import Any


def _scenario(
    *,
    key: str,
    area: str,
    agent: str,
    text: str,
    expect_all: tuple[str, ...],
    expect_any: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "key": key,
        "area": area,
        "agent": agent,
        "text": text,
        "expect_all": expect_all,
        "expect_any": expect_any,
    }


def _register_group(
    scenarios: list[dict[str, Any]],
    suites: dict[str, tuple[str, ...]],
    *,
    suite_name: str,
    key_prefix: str,
    area: str,
    agent: str,
    prompts: list[str],
    expect_all: tuple[str, ...],
    expect_any: tuple[str, ...],
) -> None:
    keys: list[str] = []
    for index, prompt in enumerate(prompts, start=1):
        key = f"{key_prefix}_{index:02d}"
        scenarios.append(
            _scenario(
                key=key,
                area=area,
                agent=agent,
                text=prompt,
                expect_all=expect_all,
                expect_any=expect_any,
            )
        )
        keys.append(key)
    suites[suite_name] = tuple(keys)


def build_generated_eval_corpus() -> tuple[list[dict[str, Any]], dict[str, tuple[str, ...]]]:
    scenarios: list[dict[str, Any]] = []
    suites: dict[str, tuple[str, ...]] = {}

    _register_group(
        scenarios,
        suites,
        suite_name="insight_pos_sales_location_10",
        key_prefix="insight_pos_sales_location",
        area="pos",
        agent="pos_admin",
        prompts=[
            "Show sales by location today.",
            "Compare revenue across locations today.",
            "Which location is leading sales today?",
            "Summarize today's order count by location.",
            "Show today's average basket by location.",
            "Break down today's gross sales across locations.",
            "Which branch is underperforming in sales today?",
            "Give me a location-by-location sales view for today.",
            "Show today's sales by location with order volume.",
            "Rank locations by sales performance today.",
        ],
        expect_all=("insight_response", "metric_grid"),
        expect_any=("bar_chart", "comparison_table"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_pos_top_sellers_10",
        key_prefix="insight_pos_top_sellers",
        area="pos",
        agent="pos_admin",
        prompts=[
            "Show top sellers in seven days.",
            "Rank the best-selling products over the last seven days.",
            "Which products sold the most quantity in seven days?",
            "Which items generated the highest revenue in the last week?",
            "Show the strongest sellers for the past seven days.",
            "List the top-selling variants in the last seven days.",
            "What products are carrying sales this week?",
            "Show the seven-day product leaders by quantity sold.",
            "Give me the weekly top sellers with revenue.",
            "Which SKUs are my top sellers over seven days?",
        ],
        expect_all=("insight_response", "ranked_list"),
        expect_any=("metric_grid", "bar_chart"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_pos_payment_mix_10",
        key_prefix="insight_pos_payment_mix",
        area="pos",
        agent="pos_admin",
        prompts=[
            "Show today's payment mix.",
            "Break down sales by payment method today.",
            "What share of today's sales came from cash versus transfer?",
            "Compare payment methods for the last seven days.",
            "Show payment channel performance by revenue this week.",
            "Which payment method drives the highest ticket size?",
            "How is payment usage split across locations today?",
            "Show failed versus successful payment activity today.",
            "Which payment methods are trending up this week?",
            "Summarize tender mix across recent POS orders.",
        ],
        expect_all=("insight_response", "metric_grid"),
        expect_any=("donut_chart", "comparison_table"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_pos_terminal_cashier_10",
        key_prefix="insight_pos_terminal_cashier",
        area="pos",
        agent="pos_admin",
        prompts=[
            "Show cashier performance today.",
            "Compare sales by cashier in the last seven days.",
            "Which cashier processed the most orders today?",
            "Show terminal activity by location today.",
            "Which POS terminal is busiest right now?",
            "Compare average basket by cashier this week.",
            "Which cashier has the highest refund activity?",
            "Show terminal usage patterns for the last seven days.",
            "Rank cashiers by revenue contribution this week.",
            "Summarize performance across terminals and cashiers.",
        ],
        expect_all=("insight_response", "comparison_table"),
        expect_any=("metric_grid", "ranked_list"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_pos_sessions_orders_10",
        key_prefix="insight_pos_sessions_orders",
        area="pos",
        agent="pos_admin",
        prompts=[
            "Show open versus closed POS sessions today.",
            "How many sessions were opened and closed this week?",
            "Show hourly sales trend for today.",
            "Which hours are driving the most sales today?",
            "Summarize order flow through the day today.",
            "Show order count trend over the last seven days.",
            "How many draft, held, and completed POS orders do we have?",
            "Show session throughput by location today.",
            "Which location has the longest active POS session?",
            "Show trend lines for orders and revenue this week.",
        ],
        expect_all=("insight_response", "metric_grid"),
        expect_any=("line_chart", "timeline"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_pos_exceptions_10",
        key_prefix="insight_pos_exceptions",
        area="pos",
        agent="pos_admin",
        prompts=[
            "Show POS risk signals today.",
            "Where are refunds unusually high this week?",
            "Which terminals have suspicious activity?",
            "Show voided or cancelled POS orders for today.",
            "Which cashier has abnormal discount activity this week?",
            "Show locations with weak conversion today.",
            "Which terminals need operational attention right now?",
            "Summarize POS exceptions across the last seven days.",
            "Show payment or session anomalies in POS activity.",
            "What POS issues should I investigate first?",
        ],
        expect_all=("insight_response", "risk_panel"),
        expect_any=("timeline", "metric_grid"),
    )

    _register_group(
        scenarios,
        suites,
        suite_name="insight_inventory_out_of_stock_10",
        key_prefix="insight_inventory_out_of_stock",
        area="inventory",
        agent="inventory_visibility",
        prompts=[
            "Show out-of-stock products.",
            "Which products are fully out of stock right now?",
            "List out-of-stock inventory items across all locations.",
            "Which locations have the most stockouts today?",
            "Show stockouts by category.",
            "Which fast movers are currently out of stock?",
            "Show zero-balance items that need attention now.",
            "What products are missing from the shelf today?",
            "Show recent stockouts that affect sales availability.",
            "Rank the most urgent out-of-stock items.",
        ],
        expect_all=("insight_response", "risk_panel"),
        expect_any=("ranked_list", "metric_grid"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_inventory_low_stock_10",
        key_prefix="insight_inventory_low_stock",
        area="inventory",
        agent="inventory_visibility",
        prompts=[
            "Show low-stock products.",
            "Which items are below reorder level right now?",
            "Rank low-stock inventory by urgency.",
            "Show low-stock products by location.",
            "Which categories have the most low-stock pressure?",
            "What items will run out soon based on current balances?",
            "Show low-stock items with reorder thresholds.",
            "Which branch is closest to stockout across key items?",
            "Show low-stock variants affecting POS availability.",
            "What low-stock issues should I act on first?",
        ],
        expect_all=("insight_response", "risk_panel"),
        expect_any=("ranked_list", "metric_grid"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_inventory_movements_10",
        key_prefix="insight_inventory_movements",
        area="inventory",
        agent="inventory_visibility",
        prompts=[
            "Show recent stock movements.",
            "Summarize stock in versus stock out this week.",
            "Show movement timeline for the last seven days.",
            "Which items had the most stock movement this week?",
            "Show receiving, transfers, and issues in one timeline.",
            "What inventory movements happened today?",
            "Show movement trends by location this week.",
            "Which categories saw the most stock depletion recently?",
            "Show movement history for recently active items.",
            "Give me a stock movement dashboard for the last seven days.",
        ],
        expect_all=("insight_response", "timeline"),
        expect_any=("line_chart", "metric_grid"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_inventory_location_health_10",
        key_prefix="insight_inventory_location_health",
        area="inventory",
        agent="inventory_visibility",
        prompts=[
            "Compare inventory health across locations.",
            "Which location has the biggest stock risk right now?",
            "Show stock posture by branch.",
            "Compare on-hand, reserved, and available stock by location.",
            "Which locations need replenishment first?",
            "Show category concentration by location.",
            "Which branch is overstocked versus understocked?",
            "Compare inventory value and stock pressure across locations.",
            "Show location-by-location inventory readiness.",
            "Which locations are healthiest from an inventory standpoint?",
        ],
        expect_all=("insight_response", "comparison_table"),
        expect_any=("metric_grid", "risk_panel"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_inventory_reorder_10",
        key_prefix="insight_inventory_reorder",
        area="inventory",
        agent="inventory_visibility",
        prompts=[
            "Show reorder candidates.",
            "Which products should I reorder now?",
            "Rank reorder needs by urgency and stock gap.",
            "Show reorder pressure by category.",
            "Which supplier-linked items need replenishment first?",
            "Show reorder candidates for each location.",
            "Which low-stock items still have supplier defaults set?",
            "What should purchasing prioritize based on current stock?",
            "Show items below safety stock with reorder evidence.",
            "Give me a reorder short list for today.",
        ],
        expect_all=("insight_response", "ranked_list"),
        expect_any=("risk_panel", "comparison_table"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_inventory_adjustment_risk_10",
        key_prefix="insight_inventory_adjustment_risk",
        area="inventory",
        agent="inventory_visibility",
        prompts=[
            "Show inventory adjustment risk signals.",
            "Which items had unusual stock adjustments recently?",
            "Show adjustment-heavy products in the last seven days.",
            "Which locations have abnormal manual corrections?",
            "Summarize adjustment activity by staff and item.",
            "Show negative adjustment trend this week.",
            "Which products have repeated correction patterns?",
            "Where do adjustment events suggest process issues?",
            "Show adjustment risks that may need audit review.",
            "What inventory adjustment anomalies should I inspect first?",
        ],
        expect_all=("insight_response", "risk_panel"),
        expect_any=("timeline", "metric_grid"),
    )

    _register_group(
        scenarios,
        suites,
        suite_name="insight_procurement_po_lifecycle_10",
        key_prefix="insight_procurement_po_lifecycle",
        area="inventory",
        agent="inventory_procurement",
        prompts=[
            "Show purchase-order lifecycle status.",
            "Summarize the purchase-order pipeline.",
            "Which purchase orders are pending, approved, issued, or received?",
            "Show open purchase orders by status.",
            "What stage is each active PO in right now?",
            "Show PO workflow progress for the last seven days.",
            "Which purchase orders are closest to completion?",
            "Show purchase-order status split by supplier.",
            "Give me a timeline view of current PO activity.",
            "Which POs are stalled in the pipeline?",
        ],
        expect_all=("insight_response", "timeline"),
        expect_any=("progress_tracker", "comparison_table"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_procurement_receiving_10",
        key_prefix="insight_procurement_receiving",
        area="inventory",
        agent="inventory_procurement",
        prompts=[
            "Show the PO receiving lifecycle.",
            "Summarize receiving progress for open purchase orders.",
            "Which POs are partially received?",
            "Show receiving timeline for recent purchase orders.",
            "What receipts landed today versus what is still pending?",
            "Show receiving completion progress by PO.",
            "Which suppliers have items still waiting to be received?",
            "Show goods-receipt activity for the last seven days.",
            "Which purchase orders are blocked at receiving?",
            "Give me a receiving progress board for current POs.",
        ],
        expect_all=("insight_response", "progress_tracker"),
        expect_any=("timeline", "metric_grid"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_procurement_supplier_10",
        key_prefix="insight_procurement_supplier",
        area="inventory",
        agent="inventory_procurement",
        prompts=[
            "Compare supplier performance this month.",
            "Which suppliers are delivering on time?",
            "Show fill rate and delay rate by supplier.",
            "Rank suppliers by receiving reliability.",
            "Which suppliers create the most receiving exceptions?",
            "Show PO completion quality by supplier.",
            "Compare supplier responsiveness across recent POs.",
            "Which supplier should I trust most for urgent restocks?",
            "Show supplier scorecards for open and recent POs.",
            "Give me a supplier performance summary for procurement.",
        ],
        expect_all=("insight_response", "comparison_table"),
        expect_any=("metric_grid", "ranked_list"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_procurement_delay_exception_10",
        key_prefix="insight_procurement_delay_exception",
        area="inventory",
        agent="inventory_procurement",
        prompts=[
            "Show delayed purchase orders.",
            "Which POs need escalation right now?",
            "Show receiving exceptions by severity.",
            "Which purchase orders are overdue for receiving?",
            "Show procurement risks across active suppliers.",
            "Which POs have missing or blocked receipt activity?",
            "Summarize procurement exceptions from the audit trail.",
            "What procurement problems should I investigate first?",
            "Show open POs with the highest operational risk.",
            "Where are purchasing delays affecting stock availability?",
        ],
        expect_all=("insight_response", "risk_panel"),
        expect_any=("timeline", "ranked_list"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_procurement_cost_variance_05",
        key_prefix="insight_procurement_cost_variance",
        area="inventory",
        agent="inventory_procurement",
        prompts=[
            "Show cost variance across recent purchase orders.",
            "Which suppliers have the biggest price variance?",
            "Compare expected versus received procurement cost.",
            "Show POs where landed cost is drifting.",
            "Which procurement lines deserve price review first?",
        ],
        expect_all=("insight_response", "comparison_table"),
        expect_any=("metric_grid", "risk_panel"),
    )

    _register_group(
        scenarios,
        suites,
        suite_name="insight_audit_staff_activity_10",
        key_prefix="insight_audit_staff_activity",
        area="users",
        agent="users",
        prompts=[
            "Show staff activity from audit events.",
            "Which staff were most active today?",
            "Show a timeline of recent staff activity.",
            "Who changed the most operational records this week?",
            "Show staff activity by role for the last seven days.",
            "Which users are touching inventory most often?",
            "Show recent staff actions across the workspace.",
            "Rank staff by audited activity volume this week.",
            "Which staff actions need management review?",
            "Give me a staff activity summary from the audit trail.",
        ],
        expect_all=("insight_response", "timeline"),
        expect_any=("metric_grid", "ranked_list"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_audit_product_activity_10",
        key_prefix="insight_audit_product_activity",
        area="product",
        agent="product_discovery",
        prompts=[
            "Show product activity from audit events.",
            "Which products were edited most recently?",
            "Give me the audit timeline for product changes.",
            "Which variants had the most recent catalog activity?",
            "Show barcode or SKU change activity this week.",
            "Which product families are seeing the most edits?",
            "Show recently modified products with audit evidence.",
            "Which product records changed across multiple staff?",
            "Show product audit activity by category.",
            "What product changes should I review first?",
        ],
        expect_all=("insight_response", "timeline"),
        expect_any=("entity_preview", "metric_grid"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_audit_pos_activity_10",
        key_prefix="insight_audit_pos_activity",
        area="pos",
        agent="pos_admin",
        prompts=[
            "Show POS activity from audit events.",
            "Which terminals had the most audited activity today?",
            "Give me a timeline of POS actions this week.",
            "Which users touched POS settings recently?",
            "Show POS configuration changes from the audit trail.",
            "Which cashier actions are most frequent in audit logs?",
            "Show order-related POS events over the last seven days.",
            "Which POS activity patterns look unusual?",
            "Show location-level POS audit activity.",
            "Summarize recent POS operational events.",
        ],
        expect_all=("insight_response", "timeline"),
        expect_any=("metric_grid", "line_chart"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_audit_support_access_10",
        key_prefix="insight_audit_support_access",
        area="users",
        agent="users",
        prompts=[
            "Show the support access audit.",
            "Who granted or used support access recently?",
            "Show support access sessions in a timeline.",
            "Which support access invitations are still active?",
            "Show high-risk support access activity.",
            "When was support access last used in this workspace?",
            "Which users initiated support access workflows?",
            "Show support access activity over the last seven days.",
            "Which support sessions should I review first?",
            "Summarize support access audit events for me.",
        ],
        expect_all=("insight_response", "timeline"),
        expect_any=("risk_panel", "comparison_table"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_audit_permission_security_05",
        key_prefix="insight_audit_permission_security",
        area="users",
        agent="users",
        prompts=[
            "Show permission and security activity.",
            "Which roles or permissions changed recently?",
            "Show MFA, access, and role-change events this week.",
            "What security-sensitive audit events need attention?",
            "Summarize permission changes across the workspace.",
        ],
        expect_all=("insight_response", "risk_panel"),
        expect_any=("timeline", "metric_grid"),
    )

    _register_group(
        scenarios,
        suites,
        suite_name="insight_product_import_opportunities_10",
        key_prefix="insight_product_import_opportunities",
        area="product",
        agent="product_discovery",
        prompts=[
            "Show global catalog import opportunities.",
            "Which global products should I import next?",
            "Show categories with strong import opportunities.",
            "Which global catalog products match my current assortment gaps?",
            "Show top catalog opportunities not yet imported.",
            "Which brands have the biggest import potential?",
            "Show likely import wins for the current workspace.",
            "Which global products can expand my catalog fastest?",
            "Rank import opportunities by relevance.",
            "Give me a global catalog opportunity board.",
        ],
        expect_all=("insight_response", "ranked_list"),
        expect_any=("comparison_table", "metric_grid"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_product_variant_lookup_10",
        key_prefix="insight_product_variant_lookup",
        area="product",
        agent="product_discovery",
        prompts=[
            "Look up product variants by barcode and show the best match.",
            "Show the matching variant for a scanned barcode.",
            "Which product variant matches this SKU or barcode?",
            "Show me the best catalog match for a product code.",
            "Look up a product variant and show its preview.",
            "Find the variant record behind a barcode scan.",
            "Which variant is tied to this catalog code?",
            "Show a variant lookup result with key details.",
            "Find the closest global catalog match for a code.",
            "Show the product family and variant that match a lookup.",
        ],
        expect_all=("insight_response", "entity_preview"),
        expect_any=("comparison_table", "metric_grid"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_product_catalog_gaps_10",
        key_prefix="insight_product_catalog_gaps",
        area="product",
        agent="product_discovery",
        prompts=[
            "Show catalog gaps in my current assortment.",
            "Which categories look underrepresented right now?",
            "What product families are missing versus demand signals?",
            "Show product assortment risks by category.",
            "Which brands are thin in my catalog?",
            "Show gaps between imported catalog and active sales mix.",
            "Which products should exist here but do not yet?",
            "Show assortment weaknesses by location or category.",
            "Where are catalog gaps likely hurting sales coverage?",
            "What catalog gaps should I close first?",
        ],
        expect_all=("insight_response", "risk_panel"),
        expect_any=("ranked_list", "comparison_table"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_product_duplicate_codes_10",
        key_prefix="insight_product_duplicate_codes",
        area="product",
        agent="product_discovery",
        prompts=[
            "Show duplicate barcode risks in the catalog.",
            "Which SKUs or barcodes may conflict across products?",
            "Show product code collisions that need cleanup.",
            "Which variants have risky duplicate identifiers?",
            "Compare barcode conflicts across imported products.",
            "Show duplicate code pressure by brand or category.",
            "Which code conflicts could block imports next?",
            "Show likely duplicate variant records from catalog matching.",
            "What barcode or SKU issues should I resolve first?",
            "Summarize duplicate code risk across the catalog.",
        ],
        expect_all=("insight_response", "comparison_table"),
        expect_any=("risk_panel", "ranked_list"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_product_media_category_05",
        key_prefix="insight_product_media_category",
        area="product",
        agent="product_discovery",
        prompts=[
            "Show products missing strong media coverage.",
            "Which categories need better curated product content?",
            "Show catalog records with weak image coverage.",
            "Which product families need merchandising cleanup first?",
            "Summarize category-level content quality opportunities.",
        ],
        expect_all=("insight_response", "ranked_list"),
        expect_any=("entity_preview", "comparison_table"),
    )

    _register_group(
        scenarios,
        suites,
        suite_name="insight_subscription_usage_10",
        key_prefix="insight_subscription_usage",
        area="users",
        agent="users",
        prompts=[
            "Show subscription usage and limits.",
            "How much of my subscription capacity am I using?",
            "Show current plan, usage, and remaining headroom.",
            "Which subscription resources are near the limit?",
            "Give me a subscription usage summary for this workspace.",
            "Show current plan pressure across staff, locations, and products.",
            "How close am I to plan limits right now?",
            "Show trial or billing status with current usage.",
            "What subscription capacity is still available?",
            "Summarize usage against subscription entitlements.",
        ],
        expect_all=("insight_response", "metric_grid"),
        expect_any=("comparison_table", "risk_panel"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_subscription_limits_pressure_10",
        key_prefix="insight_subscription_limits_pressure",
        area="users",
        agent="users",
        prompts=[
            "Which subscription limits are the biggest risk right now?",
            "Show resources most likely to hit plan limits soon.",
            "What is blocking growth under the current subscription?",
            "Show upgrade pressure based on present usage.",
            "Which features or resources are near exhaustion?",
            "Show plan pressure with the highest operational impact.",
            "What subscription risks should the owner see first?",
            "Which limit breaches are closest for this workspace?",
            "Show limit pressure across the main subscription dimensions.",
            "Give me a subscription risk panel for capacity planning.",
        ],
        expect_all=("insight_response", "comparison_table"),
        expect_any=("risk_panel", "metric_grid"),
    )

    _register_group(
        scenarios,
        suites,
        suite_name="insight_host_cross_domain_ops_10",
        key_prefix="insight_host_cross_domain_ops",
        area="host",
        agent="host",
        prompts=[
            "Give me a one-screen operational summary for today.",
            "Show the most important business signals across sales, stock, and purchasing.",
            "What should I pay attention to first across the whole workspace?",
            "Summarize revenue, stock risk, and PO status together.",
            "Show an executive operational snapshot for today.",
            "Which areas are strong and weak across inventory and POS?",
            "Give me a cross-service health overview for the workspace.",
            "Show business posture across sales, stock, staff, and procurement.",
            "What are the biggest operational changes since yesterday?",
            "Build a workspace summary with the key metrics and risks.",
        ],
        expect_all=("insight_response", "metric_grid"),
        expect_any=("risk_panel", "comparison_table"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_host_location_comparison_10",
        key_prefix="insight_host_location_comparison",
        area="host",
        agent="host",
        prompts=[
            "Compare locations across sales and stock health.",
            "Which branch is winning on sales but weak on inventory?",
            "Show side-by-side location performance for today.",
            "Compare branches by revenue, orders, and stock risk.",
            "Which location needs the most intervention right now?",
            "Show location performance gaps across POS and inventory.",
            "Which branch has strong sales but poor replenishment posture?",
            "Compare branches by top sellers and stockouts.",
            "Show a branch scorecard across operations.",
            "Rank locations by overall operational readiness.",
        ],
        expect_all=("insight_response", "comparison_table"),
        expect_any=("metric_grid", "bar_chart"),
    )
    _register_group(
        scenarios,
        suites,
        suite_name="insight_host_recommendations_05",
        key_prefix="insight_host_recommendations",
        area="host",
        agent="host",
        prompts=[
            "What are the top three actions I should take next?",
            "Show the most urgent operator recommendations right now.",
            "Which actions would improve sales and reduce stock risk fastest?",
            "Give me a prioritized action plan from current insights.",
            "What should I do first based on the latest workspace data?",
        ],
        expect_all=("insight_response", "risk_panel"),
        expect_any=("ranked_list", "progress_tracker"),
    )

    suites["insight_pos_60"] = tuple(
        key
        for suite_name in (
            "insight_pos_sales_location_10",
            "insight_pos_top_sellers_10",
            "insight_pos_payment_mix_10",
            "insight_pos_terminal_cashier_10",
            "insight_pos_sessions_orders_10",
            "insight_pos_exceptions_10",
        )
        for key in suites[suite_name]
    )
    suites["insight_inventory_60"] = tuple(
        key
        for suite_name in (
            "insight_inventory_out_of_stock_10",
            "insight_inventory_low_stock_10",
            "insight_inventory_movements_10",
            "insight_inventory_location_health_10",
            "insight_inventory_reorder_10",
            "insight_inventory_adjustment_risk_10",
        )
        for key in suites[suite_name]
    )
    suites["insight_procurement_45"] = tuple(
        key
        for suite_name in (
            "insight_procurement_po_lifecycle_10",
            "insight_procurement_receiving_10",
            "insight_procurement_supplier_10",
            "insight_procurement_delay_exception_10",
            "insight_procurement_cost_variance_05",
        )
        for key in suites[suite_name]
    )
    suites["insight_audit_45"] = tuple(
        key
        for suite_name in (
            "insight_audit_staff_activity_10",
            "insight_audit_product_activity_10",
            "insight_audit_pos_activity_10",
            "insight_audit_support_access_10",
            "insight_audit_permission_security_05",
        )
        for key in suites[suite_name]
    )
    suites["insight_product_45"] = tuple(
        key
        for suite_name in (
            "insight_product_import_opportunities_10",
            "insight_product_variant_lookup_10",
            "insight_product_catalog_gaps_10",
            "insight_product_duplicate_codes_10",
            "insight_product_media_category_05",
        )
        for key in suites[suite_name]
    )
    suites["insight_subscription_20"] = tuple(
        key
        for suite_name in (
            "insight_subscription_usage_10",
            "insight_subscription_limits_pressure_10",
        )
        for key in suites[suite_name]
    )
    suites["insight_host_25"] = tuple(
        key
        for suite_name in (
            "insight_host_cross_domain_ops_10",
            "insight_host_location_comparison_10",
            "insight_host_recommendations_05",
        )
        for key in suites[suite_name]
    )
    suites["insight_300"] = tuple(spec["key"] for spec in scenarios)

    keys = [spec["key"] for spec in scenarios]
    assert len(keys) == len(set(keys)), "Generated A2A eval keys must be unique."
    assert len(scenarios) == 300, f"Expected 300 generated A2A eval questions, found {len(scenarios)}."
    return scenarios, suites


GENERATED_SCENARIOS, GENERATED_SUITES = build_generated_eval_corpus()
