from __future__ import annotations

import json
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_mcp_config() -> dict[str, object]:
    return json.loads((_repo_root() / "mcp-tools.local.json").read_text(encoding="utf-8"))


def _agent_tool_names(config: dict[str, object]) -> dict[str, set[str]]:
    shared_servers = {
        item["id"]: item
        for item in config.get("sharedServers", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    tool_names: dict[str, set[str]] = {}
    for agent_name, payload in (config.get("agents") or {}).items():
        if not isinstance(agent_name, str) or not isinstance(payload, dict):
            continue
        names: set[str] = set()
        for server in payload.get("servers") or []:
            if not isinstance(server, dict):
                continue
            ref = server.get("ref")
            shared = shared_servers.get(ref)
            if not isinstance(shared, dict):
                continue
            prefix = str(shared.get("toolNamePrefix") or "")
            for tool in server.get("tools") or []:
                if isinstance(tool, str):
                    names.add(f"{prefix}{tool}")
        tool_names[agent_name] = names
    return tool_names


def test_insight_upgrade_binds_required_read_tools_to_specialists() -> None:
    config = _load_mcp_config()
    agent_tools = _agent_tool_names(config)

    expected = {
        "users": {
            "audit.search_events",
            "audit.get_event_timeline",
            "audit.get_staff_activity",
            "audit.get_permission_security_activity",
            "subscriptions.get_usage_and_limits",
            "users.get_staff_profile",
            "users.get_role_assignments",
        },
        "inventory_visibility": {
            "inventory.get_stock_risk",
            "inventory.get_stock_movements",
            "inventory.get_reorder_candidates",
            "audit.get_realtime_dashboard_snapshot",
            "notifications.get_alert_summary",
        },
        "inventory_procurement": {
            "audit.get_purchase_order_activity",
            "purchasing.get_po_pipeline",
            "purchasing.get_receiving_exceptions",
        },
        "pos_admin": {
            "pos.get_sales_summary",
            "pos.get_top_sellers",
            "pos.get_product_sales_trend",
            "pos.get_terminal_activity",
            "audit.get_pos_activity",
        },
        "product_discovery": {
            "product.get_variant_lookup",
            "product.get_top_catalog_matches",
            "audit.get_product_activity",
        },
    }

    for agent_name, required_tools in expected.items():
        configured = agent_tools.get(agent_name, set())
        missing = sorted(required_tools.difference(configured))
        assert not missing, f"{agent_name} is missing required insight tools: {', '.join(missing)}"


def test_insight_upgrade_binds_required_read_tools_to_host() -> None:
    config = _load_mcp_config()
    agent_tools = _agent_tool_names(config)

    required_tools = {
        "pos.get_sales_summary",
        "pos.get_top_sellers",
        "pos.get_product_sales_trend",
        "pos.get_pos_daily_summary",
        "pos.get_terminal_activity",
        "inventory.get_stock_risk",
        "inventory.get_stock_analytics",
        "inventory.get_reorder_candidates",
        "inventory.get_stock_movements",
        "inventory.search_stock_locations",
        "inventory.search_purchase_orders",
        "inventory.get_purchase_order_analytics",
        "product.search_product_variants",
        "product.get_product_dashboard_stats",
        "product.get_product_stock_alerts",
        "product.get_variant_lookup",
        "product.get_top_catalog_matches",
        "audit.search_events",
        "audit.get_event_timeline",
        "audit.get_staff_activity",
        "audit.get_product_activity",
        "audit.get_pos_activity",
        "audit.get_purchase_order_activity",
        "audit.get_realtime_dashboard_snapshot",
        "audit.get_permission_security_activity",
        "notifications.get_alert_summary",
        "purchasing.get_po_pipeline",
        "purchasing.get_receiving_exceptions",
        "subscriptions.get_usage_and_limits",
    }

    configured = agent_tools.get("host", set())
    missing = sorted(required_tools.difference(configured))
    assert not missing, f"host is missing required insight tools: {', '.join(missing)}"


def test_insight_upgrade_prompts_cover_priority_operational_flows() -> None:
    prompts_dir = _repo_root() / "prompts"
    expected_prompt_text = {
        "host_agent.txt": [
            "sales by location today",
            "top sellers in seven days",
            "out-of-stock products",
            "staff activity from audit events",
            "support access audit",
            "subscription usage/limits",
            "global catalog import opportunities",
        ],
        "inventory_agent.txt": [
            "out-of-stock products",
            "PO receiving lifecycle",
        ],
        "pos_agent.txt": [
            "sales by location today",
            "top sellers in seven days",
        ],
        "product_agent.txt": [
            "global catalog import opportunities",
            "variant lookup",
        ],
        "users_agent.txt": [
            "staff activity from audit events",
            "support access audit",
            "subscription usage and limits",
        ],
    }

    for filename, snippets in expected_prompt_text.items():
        content = (prompts_dir / filename).read_text(encoding="utf-8")
        for snippet in snippets:
            assert snippet in content, f"{filename} is missing prompt guidance for: {snippet}"


def test_insight_upgrade_specialists_remain_widget_first() -> None:
    prompts_dir = _repo_root() / "prompts"
    widget_first_prompts = [
        "host_agent.txt",
        "inventory_visibility_agent.txt",
        "inventory_procurement_agent.txt",
        "pos_admin_agent.txt",
        "product_discovery_agent.txt",
        "users_agent.txt",
    ]

    for filename in widget_first_prompts:
        content = (prompts_dir / filename).read_text(encoding="utf-8")
        assert "insight_response" in content, f"{filename} should explicitly prefer insight_response payloads"


def test_pos_admin_prompt_uses_named_sales_insight_tools() -> None:
    content = (_repo_root() / "prompts" / "pos_admin_agent.txt").read_text(encoding="utf-8")

    assert 'get_sales_summary' in content
    assert 'group_by="location"' in content
    assert 'get_top_sellers' in content
