from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import parse
from urllib import error, request

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from a2a_eval_corpus import GENERATED_SCENARIOS, GENERATED_SUITES


DEFAULT_BASE_URL = "http://localhost:7006"
DEFAULT_TIMEOUT_S = 240.0
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESET_SCRIPT = REPO_ROOT / "scripts" / "reset_workspace_profile.sh"

COMMON_FAILURE_MARKERS: tuple[str, ...] = (
    "MCP call_tool failed",
    "Error executing tool",
    "tool is not exposed",
    "Unable to retrieve",
    "connection failed",
    "service connection failed",
    "SSL error",
    "Blocking issues:",
    "Still pending:",
    "Unable to resolve dependency output",
    "Missing created operation dependency",
)

DUPLICATE_DISAMBIGUATION_MARKERS: tuple[str, ...] = (
    "I found two",
    "I found 2",
    "I found multiple",
    "There are two",
    "which one do you mean",
    "pick the matching",
    "choose the matching",
    "select the matching",
    "more than one active",
)


@dataclass(frozen=True, slots=True)
class Scenario:
    key: str
    area: str
    agent: str
    text: str
    expect_all: tuple[str, ...] = ()
    expect_any: tuple[str, ...] = ()
    reject_any: tuple[str, ...] = ()
    continue_texts: tuple[str, ...] = ()
    history_length: int | None = 10


MARKET_READINESS_39: tuple[str, ...] = (
    "host_onboarding_wizard",
    "host_marketplace_adidas_search",
    "host_marketplace_china_search",
    "onboarding_full_setup_execution",
    "inventory_locations_list",
    "inventory_location_search",
    "inventory_location_summary",
    "inventory_categories_list",
    "inventory_category_tree",
    "inventory_items_list",
    "inventory_item_details",
    "inventory_alerts",
    "inventory_balances",
    "inventory_adjust_stock",
    "inventory_create_category",
    "inventory_update_category",
    "inventory_create_location",
    "inventory_update_location",
    "inventory_create_item",
    "inventory_update_item",
    "inventory_procurement_search_po",
    "inventory_fulfillment_sales_orders",
    "inventory_fulfillment_return_orders",
    "inventory_create_reservation",
    "product_search",
    "product_details",
    "product_variants_search",
    "product_dashboard_stats",
    "product_create",
    "product_update",
    "product_create_variant",
    "product_pricing_rules",
    "product_create_pricing_rule",
    "marketplace_search",
    "marketplace_search_china",
    "pos_configuration",
    "pos_create_configuration",
    "pos_current_session",
    "pos_draft_order",
)

SCENARIO_SUITES: dict[str, tuple[str, ...]] = {
    "market_readiness_39": MARKET_READINESS_39,
    **GENERATED_SUITES,
}


APPROVED_ONBOARDING_TEXT = """Resolve Onboarding Issues
Scope: Full Inventory Setup
Primary location: Main Electronics Warehouse (Warehouse)
Additional locations: Retail Showroom, Repair & Service Center, Receiving Area, Dispatch Bay, Returns & Testing Zone, Secure Storage Room
Categories: Mobile Phones, Laptops & Computers, Tablets, Televisions, Audio Systems, Gaming Consoles, Computer Accessories, Networking Equipment, Home Appliances, Cameras & Photography
Inventory item: Electronics Core Inventory
Inventory description: Primary inventory ledger for consumer electronics, accessories, networking devices, appliances, and repair stock.
Products: Samsung Galaxy S25, Apple iPhone 17, Dell Latitude Laptop, HP LaserJet Printer, LG Smart TV 55 Inch, Sony PlayStation 5, JBL Bluetooth Speaker, TP-Link Wi-Fi Router, Logitech Wireless Mouse, Canon EOS Camera
Continue to product onboarding: Yes

Create This Setup

{
  "type": "wizard_flow_response",
  "completed": true,
  "all_responses": {
    "step_0": {
      "primary_location_name": "Main Electronics Warehouse",
      "primary_location_type": "warehouse",
      "additional_locations": "Retail Showroom\\nRepair & Service Center\\nReceiving Area\\nDispatch Bay\\nReturns & Testing Zone\\nSecure Storage Room"
    },
    "step_1": {
      "category_names": "Mobile Phones\\nLaptops & Computers\\nTablets\\nTelevisions\\nAudio Systems\\nGaming Consoles\\nComputer Accessories\\nNetworking Equipment\\nHome Appliances\\nCameras & Photography"
    },
    "step_2": {
      "default_inventory_name": "Electronics Core Inventory",
      "inventory_description": "Primary inventory ledger for consumer electronics, accessories, networking devices, appliances, and repair stock."
    },
    "step_3": {
      "continue_to_product_onboarding": true,
      "initial_product_names": "Samsung Galaxy S25\\nApple iPhone 17\\nDell Latitude Laptop\\nHP LaserJet Printer\\nLG Smart TV 55 Inch\\nSony PlayStation 5\\nJBL Bluetooth Speaker\\nTP-Link Wi-Fi Router\\nLogitech Wireless Mouse\\nCanon EOS Camera"
    }
  }
}"""

APPROVED_ONBOARDING_CREATION_REQUEST = """Create the requested inventory foundation setup using the available inventory write tools if possible. Create stock locations, inventory categories, and inventory items as applicable to the collected data, rather than only describing them. If any required detail is missing, ask one concise follow-up question.
Collected onboarding data JSON:
{
  "scope": "full_setup",
  "flat": {
    "primary_location_mode": "new",
    "primary_location_name": "Main Electronics Warehouse",
    "primary_location_type": "warehouse",
    "additional_locations": "Retail Showroom\\nRepair & Service Center\\nReceiving Area\\nDispatch Bay\\nReturns & Testing Zone\\nSecure Storage Room",
    "category_names": "Mobile Phones\\nLaptops & Computers\\nTablets\\nTelevisions\\nAudio Systems\\nGaming Consoles\\nComputer Accessories\\nNetworking Equipment\\nHome Appliances\\nCameras & Photography",
    "default_inventory_name": "Electronics Core Inventory",
    "inventory_description": "Primary inventory ledger for consumer electronics, accessories, networking devices, appliances, and repair stock.",
    "continue_to_product_onboarding": true,
    "initial_product_names": "Samsung Galaxy S25\\nApple iPhone 17\\nDell Latitude Laptop\\nHP LaserJet Printer\\nLG Smart TV 55 Inch\\nSony PlayStation 5\\nJBL Bluetooth Speaker\\nTP-Link Wi-Fi Router\\nLogitech Wireless Mouse\\nCanon EOS Camera"
  }
}"""


def _scenario_matrix() -> list[Scenario]:
    return [
        Scenario(
            key="host_onboarding_wizard",
            area="onboarding",
            agent="host",
            text="I want to set up inventory for my electronics store.",
            expect_any=("Start Inventory Onboarding", "Inventory setup", "inventory onboarding"),
            reject_any=("confirm the stock location details", "Blocking issues:"),
        ),
        Scenario(
            key="host_marketplace_adidas_search",
            area="product",
            agent="host",
            text="can you help me search for latest adidas shoes online, i want to buy shoes and start my inventory with them",
            expect_any=("marketplace", "adidas", "latest adidas shoes"),
            reject_any=("Requested agent", "I can’t browse external websites", "I can't browse external websites"),
        ),
        Scenario(
            key="host_marketplace_china_search",
            area="product",
            agent="host",
            text="can you check shoes on chinese websites",
            expect_any=("marketplace", "Alibaba", "AliExpress", "DHgate", "Temu"),
            reject_any=("pos workspace", "I can’t browse external websites", "I can't browse external websites"),
        ),
        Scenario(
            key="onboarding_full_setup_execution",
            area="onboarding",
            agent="onboarding",
            text=APPROVED_ONBOARDING_CREATION_REQUEST,
            expect_any=("Created 7 stock locations", "reused existing", "Created 7 stock locations, 10 inventory categories"),
            reject_any=("Blocking issues:", "Still pending:", "confirm the stock location details"),
            history_length=4,
        ),
        Scenario(
            key="inventory_locations_list",
            area="inventory",
            agent="inventory",
            text="List the stock locations in my electronics store setup.",
            expect_any=("Main Electronics Warehouse", "Retail Showroom"),
        ),
        Scenario(
            key="inventory_location_search",
            area="inventory",
            agent="inventory",
            text="Search stock locations for returns.",
            expect_any=("Returns & Testing Zone", "Returns Rack", "returns"),
        ),
        Scenario(
            key="inventory_location_summary",
            area="inventory",
            agent="inventory",
            text="Show the stock location summary for Main Electronics Warehouse.",
            expect_any=("Main Electronics Warehouse", "summary"),
        ),
        Scenario(
            key="inventory_categories_list",
            area="inventory",
            agent="inventory",
            text="List the inventory categories for my electronics store.",
            expect_any=("Mobile Phones", "Laptops & Computers"),
        ),
        Scenario(
            key="inventory_category_tree",
            area="inventory",
            agent="inventory_setup",
            text="Show the inventory category tree.",
            expect_any=("Mobile Phones", "Televisions", "category"),
        ),
        Scenario(
            key="inventory_items_list",
            area="inventory",
            agent="inventory",
            text="List my inventory items.",
            expect_any=("Electronics Core Inventory",),
        ),
        Scenario(
            key="inventory_item_details",
            area="inventory",
            agent="inventory",
            text="Show the inventory item details for Electronics Core Inventory.",
            expect_any=("Electronics Core Inventory", "Primary inventory ledger"),
            reject_any=DUPLICATE_DISAMBIGUATION_MARKERS,
        ),
        Scenario(
            key="inventory_alerts",
            area="inventory",
            agent="inventory_visibility",
            text="Show current inventory alerts for my electronics store.",
            expect_any=("alert", "out of stock", "No inventory", "no inventory", "No alerts"),
        ),
        Scenario(
            key="inventory_low_stock",
            area="inventory",
            agent="inventory_visibility",
            text="Show low-stock inventory items.",
            expect_any=("low-stock", "No inventory item", "no inventory item"),
        ),
        Scenario(
            key="inventory_balances",
            area="inventory",
            agent="inventory_visibility",
            text="Show stock balances for Main Electronics Warehouse.",
            expect_any=("Main Electronics Warehouse", "stock balance", "No stock balance"),
        ),
        Scenario(
            key="inventory_movements",
            area="inventory",
            agent="inventory_visibility",
            text="Show recent stock movements.",
            expect_any=("stock movement", "No stock movement", "no stock movement"),
        ),
        Scenario(
            key="inventory_reservations",
            area="inventory",
            agent="inventory_visibility",
            text="Show stock reservations.",
            expect_any=("reservation", "No stock reservation", "no stock reservation"),
        ),
        Scenario(
            key="inventory_tracking_history",
            area="inventory",
            agent="inventory_visibility",
            text="Show the tracking history for Electronics Core Inventory.",
            expect_any=("tracking", "Electronics Core Inventory", "No tracking"),
        ),
        Scenario(
            key="inventory_analytics",
            area="inventory",
            agent="inventory_visibility",
            text="Summarize stock analytics for my electronics store.",
            expect_any=("analytics", "stock value", "inventory posture", "No analytics"),
        ),
        Scenario(
            key="inventory_adjust_stock",
            area="inventory",
            agent="inventory_fulfillment",
            text="Add 10 units of Electronics Core Inventory to Main Electronics Warehouse for opening stock.",
            expect_any=(
                "Inventory adjustment completed successfully.",
                "adjustment completed",
                "stock adjustment",
                "opening stock added",
                "added 10 units",
            ),
            reject_any=COMMON_FAILURE_MARKERS + DUPLICATE_DISAMBIGUATION_MARKERS,
            continue_texts=("Confirm and apply the adjustment.",),
        ),
        Scenario(
            key="inventory_create_category",
            area="inventory",
            agent="inventory_setup",
            text="Create a new inventory category called LED Bulbs.",
            expect_any=("LED Bulbs", "created", "already exists"),
            reject_any=COMMON_FAILURE_MARKERS,
        ),
        Scenario(
            key="inventory_update_category",
            area="inventory",
            agent="inventory_setup",
            text="Update the inventory category LED Bulbs description to Energy efficient lighting products.",
            expect_any=("LED Bulbs", "updated", "Energy efficient"),
            reject_any=COMMON_FAILURE_MARKERS,
        ),
        Scenario(
            key="inventory_create_location",
            area="inventory",
            agent="inventory_setup",
            text="Create a stock location called Bulb Display Shelf with location type shelf under Main Electronics Warehouse.",
            expect_any=("Bulb Display Shelf", "created", "already exists"),
            reject_any=("confirm the stock location details",) + COMMON_FAILURE_MARKERS,
        ),
        Scenario(
            key="inventory_update_location",
            area="inventory",
            agent="inventory_setup",
            text="Update the stock location Bulb Display Shelf description to Front-of-store bulb showcase shelf.",
            expect_any=("Bulb Display Shelf", "updated", "showcase"),
            reject_any=COMMON_FAILURE_MARKERS + DUPLICATE_DISAMBIGUATION_MARKERS,
        ),
        Scenario(
            key="inventory_create_item",
            area="inventory",
            agent="inventory_setup",
            text="Create an inventory item called LED Bulb Stock in category LED Bulbs at Main Electronics Warehouse with description for boxed LED lighting inventory.",
            expect_any=("LED Bulb Stock", "created", "already exists"),
            reject_any=("confirm the inventory",) + COMMON_FAILURE_MARKERS,
        ),
        Scenario(
            key="inventory_update_item",
            area="inventory",
            agent="inventory_setup",
            text="Update the inventory item LED Bulb Stock description to Core warehouse stock for LED bulbs.",
            expect_any=("LED Bulb Stock", "updated", "Core warehouse stock"),
            reject_any=COMMON_FAILURE_MARKERS + DUPLICATE_DISAMBIGUATION_MARKERS,
        ),
        Scenario(
            key="inventory_procurement_search_po",
            area="inventory",
            agent="inventory_procurement",
            text="Show open purchase orders.",
            expect_any=("purchase order", "No purchase order", "no purchase order"),
        ),
        Scenario(
            key="inventory_procurement_analytics",
            area="inventory",
            agent="inventory_procurement",
            text="Summarize purchase order analytics.",
            expect_any=("purchase order", "analytics", "No purchase order"),
        ),
        Scenario(
            key="inventory_fulfillment_sales_orders",
            area="inventory",
            agent="inventory_fulfillment",
            text="Show recent sales orders.",
            expect_any=("sales order", "No sales order", "no sales order"),
        ),
        Scenario(
            key="inventory_fulfillment_return_orders",
            area="inventory",
            agent="inventory_fulfillment",
            text="Show return orders.",
            expect_any=("return order", "No return order", "no return order"),
        ),
        Scenario(
            key="inventory_create_reservation",
            area="inventory",
            agent="inventory_fulfillment",
            text="Create a stock reservation for 2 units of Electronics Core Inventory at Main Electronics Warehouse for showroom transfer.",
            expect_any=("Stock reservation created successfully.", "reservation created successfully"),
            reject_any=COMMON_FAILURE_MARKERS + DUPLICATE_DISAMBIGUATION_MARKERS + ("unable", "insufficient", "not enough"),
        ),
        Scenario(
            key="product_search",
            area="product",
            agent="product",
            text="Search my catalog for Galaxy.",
            expect_any=("Samsung Galaxy S25", "Galaxy"),
        ),
        Scenario(
            key="product_details",
            area="product",
            agent="product",
            text="Show product details for Samsung Galaxy S25.",
            expect_any=("Samsung Galaxy S25", "product"),
            reject_any=DUPLICATE_DISAMBIGUATION_MARKERS,
        ),
        Scenario(
            key="product_variants_search",
            area="product",
            agent="product_discovery",
            text="Search product variants for Samsung Galaxy S25.",
            expect_any=("Samsung Galaxy S25", "variant", "No variant"),
        ),
        Scenario(
            key="product_dashboard_stats",
            area="product",
            agent="product_discovery",
            text="Show product dashboard stats.",
            expect_any=("dashboard", "product", "stats"),
        ),
        Scenario(
            key="product_stock_alerts",
            area="product",
            agent="product_discovery",
            text="Show product stock alerts.",
            expect_any=("stock alert", "No stock alert", "no stock alert"),
        ),
        Scenario(
            key="product_analytics",
            area="product",
            agent="product_discovery",
            text="Show product analytics for Samsung Galaxy S25.",
            expect_any=("Samsung Galaxy S25", "analytics", "No analytics"),
        ),
        Scenario(
            key="product_create",
            area="product",
            agent="product_catalog_admin",
            text="Create a product called Smart LED Bulb in category LED Bulbs and make it POS ready.",
            expect_any=("Smart LED Bulb", "created", "already exists"),
            reject_any=COMMON_FAILURE_MARKERS,
        ),
        Scenario(
            key="product_update",
            area="product",
            agent="product_catalog_admin",
            text="Update the product Smart LED Bulb description to Wi-Fi enabled smart lighting bulb.",
            expect_any=("Smart LED Bulb", "updated", "Wi-Fi enabled"),
            reject_any=COMMON_FAILURE_MARKERS,
        ),
        Scenario(
            key="product_create_variant",
            area="product",
            agent="product_catalog_admin",
            text="Create a product variant for Smart LED Bulb named Warm White 9W.",
            expect_any=("Warm White 9W", "created", "already exists"),
            reject_any=COMMON_FAILURE_MARKERS,
        ),
        Scenario(
            key="product_pricing_rules",
            area="product",
            agent="product_pricing",
            text="Show pricing rules for Samsung Galaxy S25.",
            expect_any=("pricing rule", "No pricing rule", "no pricing rule"),
        ),
        Scenario(
            key="product_price_history",
            area="product",
            agent="product_pricing",
            text="Show price history for Samsung Galaxy S25.",
            expect_any=("price history", "Samsung Galaxy S25", "No price history"),
        ),
        Scenario(
            key="product_price_trends",
            area="product",
            agent="product_pricing",
            text="Show price trends for Samsung Galaxy S25.",
            expect_any=("price trend", "Samsung Galaxy S25", "No price trend"),
        ),
        Scenario(
            key="product_create_pricing_strategy",
            area="product",
            agent="product_pricing",
            text="Create a pricing strategy for Smart LED Bulb called Intro Margin Strategy.",
            expect_any=("pricing strategy", "created", "already exists"),
            reject_any=COMMON_FAILURE_MARKERS,
        ),
        Scenario(
            key="product_create_pricing_rule",
            area="product",
            agent="product_pricing",
            text="Create a pricing rule for Smart LED Bulb called Launch Discount Rule.",
            expect_any=("pricing rule", "created", "already exists"),
            reject_any=COMMON_FAILURE_MARKERS,
        ),
        Scenario(
            key="marketplace_search",
            area="product",
            agent="marketplace_sourcing",
            text="Search online marketplaces for smart LED bulbs and compare offers.",
            expect_any=("marketplace", "smart LED bulbs", "Marketplace results"),
        ),
        Scenario(
            key="marketplace_search_china",
            area="product",
            agent="marketplace_sourcing",
            text="check shoes on chinese websites",
            expect_any=("marketplace", "Alibaba", "AliExpress", "DHgate", "Temu"),
        ),
        Scenario(
            key="pos_configuration",
            area="pos",
            agent="pos_admin",
            text="Show the active POS configuration.",
            expect_any=("POS configuration", "No POS configuration", "no POS configuration"),
            reject_any=COMMON_FAILURE_MARKERS,
        ),
        Scenario(
            key="pos_create_configuration",
            area="pos",
            agent="pos_admin",
            text="Create a POS configuration for the electronics showroom.",
            expect_any=("POS configuration", "created", "already exists"),
            continue_texts=("Use those defaults and create it.",),
        ),
        Scenario(
            key="pos_terminals",
            area="pos",
            agent="pos_admin",
            text="List POS terminals.",
            expect_any=("terminal", "No POS terminal", "no POS terminal"),
        ),
        Scenario(
            key="pos_create_terminal",
            area="pos",
            agent="pos_admin",
            text="Create a POS terminal called Showroom Counter 1.",
            expect_any=("Showroom Counter 1", "created", "already exists"),
        ),
        Scenario(
            key="pos_customers_search",
            area="pos",
            agent="pos_admin",
            text="Search POS customers.",
            expect_any=("customer", "No POS customer", "no POS customer"),
        ),
        Scenario(
            key="pos_create_customer",
            area="pos",
            agent="pos_admin",
            text="Create a POS customer named Emeka Electronics Buyer.",
            expect_any=("Emeka Electronics Buyer", "created", "already exists"),
        ),
        Scenario(
            key="pos_tables",
            area="pos",
            agent="pos_admin",
            text="List POS tables.",
            expect_any=("table", "No POS table", "no POS table"),
        ),
        Scenario(
            key="pos_create_table",
            area="pos",
            agent="pos_admin",
            text="Create a POS table called Counter Pickup 1.",
            expect_any=("Counter Pickup 1", "created", "already exists"),
        ),
        Scenario(
            key="pos_discounts",
            area="pos",
            agent="pos_admin",
            text="List POS discounts.",
            expect_any=("discount", "No POS discount", "no POS discount"),
        ),
        Scenario(
            key="pos_create_discount",
            area="pos",
            agent="pos_admin",
            text="Create a POS discount called Launch Weekend Discount.",
            expect_any=("Launch Weekend Discount", "created", "already exists"),
        ),
        Scenario(
            key="pos_daily_summary",
            area="pos",
            agent="pos_admin",
            text="Summarize today's POS sales.",
            expect_any=("POS", "sales", "summary"),
        ),
        Scenario(
            key="pos_current_session",
            area="pos",
            agent="pos_live",
            text="What POS session is currently open for me?",
            expect_any=("session", "No open POS session", "no open POS session"),
        ),
        Scenario(
            key="pos_draft_order",
            area="pos",
            agent="pos_live",
            text="Create or get my current POS draft order.",
            expect_any=("draft order", "POS order", "created"),
        ),
        Scenario(
            key="pos_held_orders",
            area="pos",
            agent="pos_live",
            text="Show held POS carts.",
            expect_any=("held", "No held POS", "no held POS"),
        ),
        Scenario(
            key="pos_order_search",
            area="pos",
            agent="pos_live",
            text="Search POS orders.",
            expect_any=("POS order", "No POS order", "no POS order"),
        ),
        *[Scenario(**payload) for payload in GENERATED_SCENARIOS],
    ]


def _sanitize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _extract_message_parts(parts: list[dict[str, Any]] | None) -> tuple[str | None, dict[str, Any] | None]:
    if not parts:
        return None, None
    text_parts: list[str] = []
    data_part: dict[str, Any] | None = None
    for part in parts:
        kind = str(part.get("kind") or "").strip().lower()
        if kind == "text":
            text = str(part.get("text") or "").strip()
            if text:
                text_parts.append(text)
        elif kind == "data" and isinstance(part.get("data"), dict):
            data_part = part["data"]
    text_value = "\n".join(item for item in text_parts if item).strip() or None
    return text_value, data_part


def _interaction_summary_text(data_value: dict[str, Any] | None) -> str | None:
    if not isinstance(data_value, dict):
        return None
    interaction_type = str(data_value.get("interaction_type") or "").strip().lower()
    title = str(data_value.get("title") or "").strip()
    description = str(data_value.get("description") or "").strip()
    if interaction_type == "marketplace_results":
        query = str(data_value.get("query") or "").strip()
        products = data_value.get("products") if isinstance(data_value.get("products"), list) else []
        if query and products:
            count = len(products)
            return f"Marketplace search found {count} result{'s' if count != 1 else ''} for {query}."
    if title and description:
        return f"{title}\n{description}"
    if title:
        return title
    if description:
        return description
    return None


def _coalesce_stream_text(text_value: str | None, data_value: dict[str, Any] | None) -> str | None:
    summary = _interaction_summary_text(data_value)
    raw_text = str(text_value or "").strip()
    if summary and (not raw_text or raw_text.lower() in {"working", "completed", "submitted"}):
        return summary
    return raw_text or summary or None


def _run_stream(*, base_url: str, token: str, scenario: Scenario, timeout_s: float) -> dict[str, Any]:
    body = {
        "agent_name": scenario.agent,
        "text": scenario.text,
    }
    if scenario.history_length is not None:
        body["history_length"] = scenario.history_length
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    final_text: str | None = None
    final_data: dict[str, Any] | None = None
    final_state: str | None = None
    task_id: str | None = None
    errors: list[str] = []
    events_seen: list[str] = []
    req = request.Request(
        f"{base_url.rstrip('/')}/stream",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout_s) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data: "):
                    continue
                payload = json.loads(line[6:])
                kind = str(payload.get("kind") or "").strip()
                if kind:
                    events_seen.append(kind)
                if kind == "task":
                    task_id = str(payload.get("id") or "").strip() or task_id
                elif kind == "artifact-update":
                    artifact = payload.get("artifact") or {}
                    text_value, data_value = _extract_message_parts(artifact.get("parts"))
                    if data_value:
                        final_data = data_value
                    coalesced_text = _coalesce_stream_text(text_value, data_value)
                    if coalesced_text:
                        final_text = coalesced_text
                elif kind == "status-update":
                    status = payload.get("status") or {}
                    final_state = str(status.get("state") or "").strip() or final_state
                    text_value, data_value = _extract_message_parts((status.get("message") or {}).get("parts"))
                    if data_value:
                        final_data = data_value
                    coalesced_text = _coalesce_stream_text(text_value, data_value)
                    if coalesced_text:
                        final_text = coalesced_text
                    if payload.get("final"):
                        break
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    return {
        "task_id": task_id,
        "final_state": final_state,
        "final_text": final_text,
        "final_data": final_data,
        "errors": errors,
        "events_seen": events_seen,
    }


def _continue_stream(
    *,
    base_url: str,
    token: str,
    task_id: str,
    agent: str,
    text: str,
    history_length: int | None,
    timeout_s: float,
) -> dict[str, Any]:
    body = {"text": text}
    if history_length is not None:
        body["history_length"] = history_length
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    final_text: str | None = None
    final_data: dict[str, Any] | None = None
    final_state: str | None = None
    errors: list[str] = []
    events_seen: list[str] = []
    query = parse.urlencode({"agent_name": agent})
    req = request.Request(
        f"{base_url.rstrip('/')}/tasks/{task_id}/continue/stream?{query}",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout_s) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data: "):
                    continue
                payload = json.loads(line[6:])
                kind = str(payload.get("kind") or "").strip()
                if kind:
                    events_seen.append(kind)
                if kind == "artifact-update":
                    artifact = payload.get("artifact") or {}
                    text_value, data_value = _extract_message_parts(artifact.get("parts"))
                    if data_value:
                        final_data = data_value
                    coalesced_text = _coalesce_stream_text(text_value, data_value)
                    if coalesced_text:
                        final_text = coalesced_text
                elif kind == "status-update":
                    status = payload.get("status") or {}
                    final_state = str(status.get("state") or "").strip() or final_state
                    text_value, data_value = _extract_message_parts((status.get("message") or {}).get("parts"))
                    if data_value:
                        final_data = data_value
                    coalesced_text = _coalesce_stream_text(text_value, data_value)
                    if coalesced_text:
                        final_text = coalesced_text
                    if payload.get("final"):
                        break
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    return {
        "task_id": task_id,
        "final_state": final_state,
        "final_text": final_text,
        "final_data": final_data,
        "errors": errors,
        "events_seen": events_seen,
    }


def _option_match_value(options: list[dict[str, Any]], text: str) -> str | None:
    normalized = _sanitize_text(text).lower()
    for option in options:
        label = _sanitize_text(str(option.get("label") or "")).lower()
        if label and label in normalized:
            return str(option.get("value") or "").strip() or None
    return None


def _build_dynamic_form_response(*, scenario: Scenario, payload: dict[str, Any]) -> dict[str, Any] | None:
    fields = payload.get("fields")
    if not isinstance(fields, list):
        return None
    current_values = payload.get("current_values")
    values = dict(current_values) if isinstance(current_values, dict) else {}
    scenario_text = scenario.text

    for field in fields:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").strip()
        if not name:
            continue
        current = values.get(name)
        if current not in (None, "", [], {}):
            continue
        field_type = str(field.get("type") or "").strip().lower()
        if field_type == "checkbox":
            values[name] = False
            continue
        if field_type == "select":
            options = field.get("options")
            if isinstance(options, list):
                matched = _option_match_value(options, scenario_text)
                if matched:
                    values[name] = matched
                    continue
                if len(options) == 1:
                    option_value = str(options[0].get("value") or "").strip()
                    if option_value:
                        values[name] = option_value
                        continue
            continue
        if "quantity" in name:
            match = re.search(r"\b(\d+(?:\.\d+)?)\b", scenario_text)
            values[name] = match.group(1) if match else "1"
            continue
        if "external_order_type" == name:
            values[name] = "sales_order"
            continue
        if "external_order_id" == name:
            values[name] = "smoke-test-order"
            continue
        if "external_order_line_id" == name:
            values[name] = "line-1"
            continue
        placeholder = str(field.get("placeholder") or "").strip()
        if placeholder:
            values[name] = placeholder

    return {"type": "form_response", "data": values}


def _run_scenario_with_auto_continue(*, base_url: str, token: str, scenario: Scenario, timeout_s: float) -> dict[str, Any]:
    result = _run_stream(base_url=base_url, token=token, scenario=scenario, timeout_s=timeout_s)
    continue_index = 0
    for _ in range(4):
        if result.get("final_state") != "input-required":
            break
        payload = result.get("final_data")
        task_id = str(result.get("task_id") or "").strip()
        if not task_id:
            break
        if isinstance(payload, dict) and str(payload.get("interaction_type") or "").strip().lower() == "dynamic_form":
            response_payload = _build_dynamic_form_response(scenario=scenario, payload=payload)
            if not response_payload:
                break
            continue_text = json.dumps(response_payload)
        elif continue_index < len(scenario.continue_texts):
            continue_text = scenario.continue_texts[continue_index]
            continue_index += 1
        else:
            break
        result = _continue_stream(
            base_url=base_url,
            token=token,
            task_id=task_id,
            agent=scenario.agent,
            text=continue_text,
            history_length=scenario.history_length,
            timeout_s=timeout_s,
        )
    return result


def _scenario_passed(result: dict[str, Any], scenario: Scenario) -> tuple[bool, str]:
    text = _sanitize_text(result.get("final_text") or "")
    data = result.get("final_data")
    haystacks = [text]
    if data is not None:
        haystacks.append(_sanitize_text(json.dumps(data, sort_keys=True)))
    full_text = " ".join(item for item in haystacks if item).strip()
    for rejected in scenario.reject_any:
        if rejected.lower() in full_text.lower():
            return False, f"rejected text matched: {rejected}"
    missing_required = [expected for expected in scenario.expect_all if expected.lower() not in full_text.lower()]
    if missing_required:
        return False, f"missing required markers: {tuple(missing_required)}"
    if scenario.expect_any:
        for expected in scenario.expect_any:
            if expected.lower() in full_text.lower():
                return True, f"matched: {expected}"
        return False, f"none of expected markers were found: {scenario.expect_any}"
    return True, "no expectation markers configured"


def _run_workspace_reset(*, profile_id: str, access_token: str | None, include_chat: bool, dry_run: bool, reset_script: Path) -> None:
    cmd = [str(reset_script), "--profile-id", profile_id]
    if include_chat:
        if not access_token:
            raise RuntimeError("Reset with chat requires an access token.")
        cmd.extend(["--include-chat", "--access-token", access_token])
    if dry_run:
        cmd.append("--dry-run")
    completed = subprocess.run(cmd, cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    if completed.stdout.strip():
        print(completed.stdout.strip(), flush=True)
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        raise RuntimeError(f"Workspace reset failed: {stderr or completed.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live A2A smoke scenarios against the gateway stream endpoint.")
    parser.add_argument("--token", default=os.environ.get("A2A_ACCESS_TOKEN"), help="Bearer access token")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Gateway base URL")
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="Scenario key to run. Repeat to run multiple. Defaults to all scenarios.",
    )
    parser.add_argument(
        "--area",
        action="append",
        default=[],
        help="Filter by area. Repeat to include multiple areas.",
    )
    parser.add_argument(
        "--suite",
        action="append",
        default=[],
        help="Named scenario suite to run. Repeat to include multiple suites.",
    )
    parser.add_argument("--list", action="store_true", help="List available scenarios and exit")
    parser.add_argument("--list-suites", action="store_true", help="List available suites and exit")
    parser.add_argument("--json-out", help="Optional JSON file to write full results to")
    parser.add_argument("--reset-profile-id", help="Reset this workspace profile before running scenarios.")
    parser.add_argument("--reset-with-chat", action="store_true", help="Also delete AI chat history during workspace reset.")
    parser.add_argument("--reset-dry-run", action="store_true", help="Preview the reset instead of deleting data.")
    parser.add_argument("--reset-script", default=str(DEFAULT_RESET_SCRIPT), help="Path to the workspace reset script.")
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"Per stream request timeout in seconds. Default: {DEFAULT_TIMEOUT_S}.",
    )
    args = parser.parse_args()

    scenarios = _scenario_matrix()
    scenario_map = {item.key: item for item in scenarios}

    if args.list:
        for item in scenarios:
            print(f"{item.key}\t{item.area}\t{item.agent}", flush=True)
        return 0
    if args.list_suites:
        for suite_name, suite_keys in SCENARIO_SUITES.items():
            print(f"{suite_name}\t{len(suite_keys)}", flush=True)
        return 0

    if not args.token:
        print("Missing token. Pass --token or set A2A_ACCESS_TOKEN.", file=sys.stderr)
        return 2
    if args.reset_with_chat and not args.token:
        print("Reset with chat requires a token.", file=sys.stderr)
        return 2

    if args.reset_profile_id:
        _run_workspace_reset(
            profile_id=str(args.reset_profile_id),
            access_token=str(args.token or "").strip() or None,
            include_chat=bool(args.reset_with_chat),
            dry_run=bool(args.reset_dry_run),
            reset_script=Path(args.reset_script),
        )
        if args.reset_dry_run:
            return 0

    selected_keys: list[str] = []
    for suite_name in args.suite:
        if suite_name not in SCENARIO_SUITES:
            raise KeyError(f"Unknown suite: {suite_name}")
        for key in SCENARIO_SUITES[suite_name]:
            if key not in selected_keys:
                selected_keys.append(key)
    for key in args.scenario:
        if key not in selected_keys:
            selected_keys.append(key)
    selected = scenarios if not selected_keys else [scenario_map[key] for key in selected_keys]
    if args.area:
        allowed_areas = {item.strip().lower() for item in args.area if item.strip()}
        selected = [item for item in selected if item.area.lower() in allowed_areas]

    results: list[dict[str, Any]] = []
    failures = 0
    for scenario in selected:
        print(f"== {scenario.key} [{scenario.area}] via {scenario.agent}", flush=True)
        try:
            result = _run_scenario_with_auto_continue(
                base_url=args.base_url,
                token=args.token,
                scenario=scenario,
                timeout_s=max(1.0, float(args.timeout_s)),
            )
            ok, reason = _scenario_passed(result, scenario)
        except Exception as exc:  # pragma: no cover - live runner
            ok = False
            reason = f"{type(exc).__name__}: {exc}"
            result = {
                "task_id": None,
                "final_state": None,
                "final_text": None,
                "final_data": None,
                "events_seen": [],
            }
        summary = _sanitize_text(result.get("final_text") or "")
        if len(summary) > 220:
            summary = f"{summary[:217]}..."
        status = "PASS" if ok else "FAIL"
        print(f"{status}: {reason}", flush=True)
        if summary:
            print(f"final: {summary}", flush=True)
        elif result.get("final_data") is not None:
            print(f"final_data: {json.dumps(result['final_data'], sort_keys=True)[:220]}", flush=True)
        if not ok:
            failures += 1
        results.append(
            {
                "scenario": scenario.key,
                "area": scenario.area,
                "agent": scenario.agent,
                "ok": ok,
                "reason": reason,
                **result,
            }
        )
        print(flush=True)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2), encoding="utf-8")

    passed = len(results) - failures
    print(f"Summary: {passed}/{len(results)} passed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
