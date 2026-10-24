"""Canonical, presentation-independent analytics evidence for A2A responses.

Insight widgets are an API for the frontend.  They must not become the implicit
source of truth for a later conversational turn.  This module snapshots the
small, typed facts an insight exposes and provides deterministic answers for
supported analytical follow-ups.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from typing import Any


FACT_PACKET_KEY = "fact_packet"
FACT_PACKET_SCHEMA_VERSION = 1


def _normalise(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _numeric(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _label(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            candidate = str(value).strip()
            if candidate:
                return candidate
    return None


def _value(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        parsed = _numeric(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _flatten_widgets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []

    def append_widgets(candidates: Any) -> None:
        if not isinstance(candidates, list):
            return
        for widget in candidates:
            if not isinstance(widget, dict):
                continue
            flattened.append(widget)
            if str(widget.get("type") or "").strip().lower() != "section_stack":
                continue
            for section in widget.get("sections") if isinstance(widget.get("sections"), list) else []:
                if isinstance(section, dict):
                    append_widgets(section.get("widgets"))

    append_widgets(payload.get("widgets"))
    return flattened


def _currency_code(payload: dict[str, Any], widgets: list[dict[str, Any]]) -> str | None:
    for candidate in [payload, *widgets]:
        for key in ("currency_code", "currencyCode", "currency"):
            value = candidate.get(key)
            if value:
                return str(value).strip().upper()
    return None


def _timeframe(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("timeframe") if isinstance(payload.get("timeframe"), dict) else {}
    values = {
        key: str(raw.get(key) or payload.get(f"_{key}") or "").strip()
        for key in ("label", "start_date", "end_date")
    }
    return {key: value for key, value in values.items() if value}


def _fact(
    *,
    dimension: str,
    entity_label: str,
    metric: str,
    value: float,
    currency_code: str | None,
    source_widget: str,
    unit: str,
    entity_id: str | None = None,
    coverage: str = "complete",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "dimension": dimension,
        "entity_label": entity_label,
        "metric": metric,
        "value": value,
        "unit": unit,
        "source_widget": source_widget,
        "coverage": coverage,
    }
    if entity_id:
        result["entity_id"] = entity_id
    if currency_code and unit == "currency":
        result["currency_code"] = currency_code
    return result


def classify_analytic_request(user_text: str) -> dict[str, Any]:
    """Return a bounded frame used for fact lookup and validation, never routing."""
    text = _normalise(user_text)
    asks_product_quantity = (
        any(term in text for term in ("how many", "amount", "quantity", "number of", "total"))
        and any(term in text for term in ("sell", "sold", "selling"))
        and any(term in text for term in ("goods", "products", "product", "items", "item", "units", "unit"))
    )
    if any(term in text for term in ("purchase order", "procurement", "receiving", "supplier")):
        domain = "procurement"
    elif any(term in text for term in ("stock", "inventory", "reorder", "out of stock", "expiry")):
        domain = "inventory"
    elif any(term in text for term in ("catalog", "global product", "import product", "barcode", "sku")):
        domain = "product_catalog"
    elif any(term in text for term in ("sale", "revenue", "turnover", "pos", "order", "sold")):
        domain = "pos"
    else:
        domain = "unknown"

    if any(term in text for term in ("location", "branch", "store", "outlet", "warehouse", "site")):
        entity = "location"
    elif any(term in text for term in ("product", "products", "item", "items", "sku", "variant", "seller")):
        entity = "product"
    elif "order" in text:
        entity = "order"
    else:
        entity = "workspace"

    if asks_product_quantity or any(term in text for term in ("unit", "units", "quantity", "goods", "items sold")):
        metric = "units_sold"
    elif any(term in text for term in ("revenue", "sales", "turnover", "takings")):
        metric = "sales_revenue"
    elif any(term in text for term in ("order", "orders", "transaction", "transactions")):
        metric = "order_count"
    elif any(term in text for term in ("out of stock", "low stock", "stock", "inventory")):
        metric = "stock_count"
    else:
        metric = "unknown"

    if any(term in text for term in ("no sales", "zero sales", "no revenue")):
        direction = "zero"
    elif any(term in text for term in ("lowest", "least", "bottom", "weakest", "worst", "lagging", "underperforming")):
        direction = "minimum"
    elif any(term in text for term in ("highest", "most", "top", "leading", "leader", "best", "strongest", "largest")):
        direction = "maximum"
    else:
        direction = None

    has_new_time_scope = any(
        term in text
        for term in (
            "today",
            "yesterday",
            "this week",
            "this month",
            "this year",
            "last week",
            "last month",
            "last quarter",
            "last year",
            "past ",
            "between ",
            "from ",
        )
    )
    return {
        "domain": domain,
        "entity": entity,
        "metric": metric,
        "ranking_direction": direction,
        "has_new_time_scope": has_new_time_scope,
    }


def resolve_analytic_follow_up_frame(*, user_text: str, packet: dict[str, Any]) -> dict[str, Any]:
    """Resolve an elliptical ranking follow-up from unambiguous saved evidence.

    ``Which one sold least?`` is meaningful after a location-sales analysis,
    but should not guess between product and location rows in a broad business
    review.  Only the previous query frame or a single evidence dimension may
    fill an omitted entity or metric.
    """
    frame = dict(classify_analytic_request(user_text))
    if frame["has_new_time_scope"] or frame["ranking_direction"] is None:
        return frame

    facts = packet.get("facts") if isinstance(packet.get("facts"), list) else []
    supported_facts = [
        fact
        for fact in facts
        if isinstance(fact, dict)
        and str(fact.get("dimension") or "") in {"location", "product"}
        and str(fact.get("metric") or "") in {"sales_revenue", "units_sold", "order_count"}
        and _numeric(fact.get("value")) is not None
    ]
    prior = packet.get("query_frame") if isinstance(packet.get("query_frame"), dict) else {}
    available_dimensions = {str(fact.get("dimension")) for fact in supported_facts}

    if frame["entity"] == "workspace":
        prior_entity = str(prior.get("entity") or "")
        if prior_entity in available_dimensions:
            frame["entity"] = prior_entity
        elif len(available_dimensions) == 1:
            frame["entity"] = next(iter(available_dimensions))

    if frame["metric"] == "unknown" and frame["entity"] in {"location", "product"}:
        available_metrics = {
            str(fact.get("metric"))
            for fact in supported_facts
            if fact.get("dimension") == frame["entity"]
        }
        prior_metric = str(prior.get("metric") or "")
        if prior_metric in available_metrics:
            frame["metric"] = prior_metric
        elif len(available_metrics) == 1:
            frame["metric"] = next(iter(available_metrics))
    return frame


def validate_analytic_selection(
    *,
    frame: dict[str, Any],
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
) -> dict[str, Any]:
    """Verify that a rendered ranking is consistent with the canonical facts."""
    direction = str(frame.get("ranking_direction") or "")
    values = [_numeric(fact.get("value")) for fact in candidates]
    selected_values = [_numeric(fact.get("value")) for fact in selected]
    if not candidates or not selected or any(value is None for value in values + selected_values):
        return {"valid": False, "reason": "Missing numeric evidence for the ranking response."}

    numeric_values = [float(value) for value in values if value is not None]
    numeric_selected = [float(value) for value in selected_values if value is not None]
    if direction == "minimum":
        valid = all(value == min(numeric_values) for value in numeric_selected)
    elif direction == "maximum":
        valid = all(value == max(numeric_values) for value in numeric_selected)
    elif direction == "zero":
        zero_values = [value for value in numeric_values if value == 0]
        valid = (
            all(value == 0 for value in numeric_selected)
            if zero_values
            else all(value == min(numeric_values) for value in numeric_selected)
        )
    else:
        valid = False
    return {
        "valid": valid,
        "reason": None if valid else "Selected facts do not satisfy the requested ranking direction.",
    }


def build_fact_packet(*, user_text: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Create a versioned fact packet from a completed structured insight.

    The packet intentionally contains only values already sent to the user.  It
    is safe to persist with conversation state and cannot expose raw MCP output.
    """
    if str(payload.get("kind") or "").strip() != "insight_response":
        return None
    widgets = _flatten_widgets(payload)
    currency = _currency_code(payload, widgets)
    facts: list[dict[str, Any]] = []

    for widget in widgets:
        title = _normalise(widget.get("title"))
        widget_type = str(widget.get("type") or "").strip().lower()
        rows = widget.get("rows") if isinstance(widget.get("rows"), list) else widget.get("data")
        if not isinstance(rows, list):
            rows = widget.get("items") if isinstance(widget.get("items"), list) else []
        # Metric grids such as "Inventory risk posture - All locations" use
        # location as scope, not as the row dimension. Treating their labels
        # as locations creates false sales facts (for example, "Needs Reorder"
        # with value 0) and corrupts ranking follow-ups.
        is_location = widget_type != "metric_grid" and (
            any(isinstance(row, dict) and "location" in row for row in rows)
            or (
                "location" in title
                and any(
                    isinstance(row, dict)
                    and any(key in row for key in ("sales", "sales_total", "total_sales", "revenue", "orders"))
                    for row in rows
                )
            )
        )
        is_product = any(term in title for term in ("product", "seller", "variant"))
        coverage = "ranked_subset" if any(term in title for term in ("top ", "best ", "leading ")) else "complete"

        for row in rows:
            if not isinstance(row, dict):
                continue
            if is_location:
                entity_label = _label(row, "location", "label", "name", "store", "branch")
                entity_id = _label(row, "location_id", "id")
                revenue = _value(row, "sales", "total_sales", "revenue", "sales_total", "amount", "value")
                orders = _value(row, "orders", "order_count", "count", "transaction_count")
                if entity_label and revenue is not None:
                    facts.append(
                        _fact(
                            dimension="location",
                            entity_label=entity_label,
                            entity_id=entity_id,
                            metric="sales_revenue",
                            value=revenue,
                            currency_code=currency,
                            source_widget=str(widget.get("title") or "Location analysis"),
                            unit="currency",
                            coverage="complete",
                        )
                    )
                if entity_label and orders is not None:
                    facts.append(
                        _fact(
                            dimension="location",
                            entity_label=entity_label,
                            entity_id=entity_id,
                            metric="order_count",
                            value=orders,
                            currency_code=None,
                            source_widget=str(widget.get("title") or "Location analysis"),
                            unit="count",
                            coverage="complete",
                        )
                    )
            if is_product:
                entity_label = _label(row, "product", "product_name", "variant_name", "label", "name", "title")
                entity_id = _label(row, "product_id", "variant_id", "id")
                revenue = _value(row, "sales", "total_sales", "revenue", "sales_total", "amount", "value")
                units = _value(row, "quantity_sold", "units_sold", "quantity", "units", "count")
                if entity_label and revenue is not None:
                    facts.append(
                        _fact(
                            dimension="product",
                            entity_label=entity_label,
                            entity_id=entity_id,
                            metric="sales_revenue",
                            value=revenue,
                            currency_code=currency,
                            source_widget=str(widget.get("title") or "Product analysis"),
                            unit="currency",
                            coverage=coverage,
                        )
                    )
                if entity_label and units is not None:
                    facts.append(
                        _fact(
                            dimension="product",
                            entity_label=entity_label,
                            entity_id=entity_id,
                            metric="units_sold",
                            value=units,
                            currency_code=None,
                            source_widget=str(widget.get("title") or "Product analysis"),
                            unit="count",
                            coverage=coverage,
                        )
                    )

        if str(widget.get("type") or "").strip() == "metric_grid":
            for item in widget.get("data") if isinstance(widget.get("data"), list) else []:
                if not isinstance(item, dict):
                    continue
                label = _normalise(item.get("label"))
                value = _numeric(item.get("value"))
                if value is None:
                    continue
                if any(term in label for term in ("revenue", "sales", "turnover")):
                    metric, unit = "sales_revenue", "currency"
                elif any(term in label for term in ("order", "transaction")):
                    metric, unit = "order_count", "count"
                elif any(term in label for term in ("unit", "quantity", "items sold")):
                    metric, unit = "units_sold", "count"
                elif any(term in label for term in ("low stock", "out of stock", "reorder", "expiring")):
                    metric, unit = "stock_count", "count"
                else:
                    continue
                facts.append(
                    _fact(
                        dimension="workspace",
                        entity_label="workspace",
                        metric=metric,
                        value=value,
                        currency_code=currency,
                        source_widget=str(widget.get("title") or "Summary"),
                        unit=unit,
                        coverage="complete",
                    )
                )

    deduplicated: dict[tuple[str, str, str, float], dict[str, Any]] = {}
    for fact in facts:
        key = (str(fact["dimension"]), str(fact["entity_label"]), str(fact["metric"]), float(fact["value"]))
        deduplicated.setdefault(key, fact)
    if not deduplicated:
        return None
    return {
        "schema_version": FACT_PACKET_SCHEMA_VERSION,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "query_frame": classify_analytic_request(user_text),
        "timeframe": _timeframe(payload),
        "sources": deepcopy(payload.get("data_sources") if isinstance(payload.get("data_sources"), list) else []),
        "facts": list(deduplicated.values()),
    }


def attach_fact_packet(*, user_text: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Attach immutable evidence without changing the widget contract."""
    if str(payload.get("kind") or "").strip() != "insight_response":
        return payload
    packet = payload.get(FACT_PACKET_KEY)
    if isinstance(packet, dict) and isinstance(packet.get("facts"), list):
        return payload
    built = build_fact_packet(user_text=user_text, payload=payload)
    if built is None:
        return payload
    enriched = dict(payload)
    enriched[FACT_PACKET_KEY] = built
    return enriched


def fact_packet_from_payload(*, user_text: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    candidate = payload.get(FACT_PACKET_KEY)
    if isinstance(candidate, dict) and isinstance(candidate.get("facts"), list):
        return candidate
    return build_fact_packet(user_text=user_text, payload=payload)


def _format_value(value: float, *, unit: str, currency_code: str | None) -> str:
    if unit == "currency":
        symbol = {"NGN": "₦", "USD": "$", "EUR": "EUR ", "GBP": "GBP "}.get(
            currency_code or "", f"{currency_code} " if currency_code else ""
        )
        return f"{symbol}{value:,.2f}"
    return f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}"


def answer_follow_up_from_fact_packet(*, user_text: str, packet: dict[str, Any]) -> str | None:
    """Answer an explicit ranking follow-up only from matching canonical facts."""
    frame = resolve_analytic_follow_up_frame(user_text=user_text, packet=packet)
    if frame["has_new_time_scope"] or frame["ranking_direction"] is None:
        return None
    if frame["entity"] not in {"location", "product"} or frame["metric"] == "unknown":
        return None
    facts = packet.get("facts") if isinstance(packet.get("facts"), list) else []
    matching = [
        fact
        for fact in facts
        if isinstance(fact, dict)
        and fact.get("dimension") == frame["entity"]
        and fact.get("metric") == frame["metric"]
        and not _is_scope_only_location_fact(fact)
        and _numeric(fact.get("value")) is not None
        and str(fact.get("entity_label") or "").strip()
    ]
    if not matching:
        return None

    if frame["ranking_direction"] in {"minimum", "zero"} and any(
        fact.get("coverage") != "complete" for fact in matching
    ):
        # A "top sellers" list proves the leader but cannot prove the global
        # minimum or which catalog product had no sales.
        return None

    values_by_entity: dict[str, dict[str, Any]] = {}
    for fact in matching:
        label = str(fact["entity_label"]).strip()
        values_by_entity.setdefault(label, fact)
    candidates = list(values_by_entity.values())
    direction = str(frame["ranking_direction"])
    if direction == "zero":
        selected = [fact for fact in candidates if _numeric(fact.get("value")) == 0]
        if not selected:
            lowest = min(candidates, key=lambda item: _numeric(item.get("value")) or 0)
            if not validate_analytic_selection(frame=frame, candidates=candidates, selected=[lowest])["valid"]:
                return None
            value = _numeric(lowest.get("value")) or 0
            unit = str(lowest.get("unit") or "count")
            currency = str(lowest.get("currency_code") or "").upper() or None
            timeframe = packet.get("timeframe") if isinstance(packet.get("timeframe"), dict) else {}
            timeframe_label = str(timeframe.get("label") or "").strip()
            return (
                f"From the saved analysis{f' for {timeframe_label}' if timeframe_label else ''}, "
                f"no location recorded zero sales. The lowest was {lowest['entity_label']} at "
                f"{_format_value(value, unit=unit, currency_code=currency)}."
            )
        else:
            if not validate_analytic_selection(frame=frame, candidates=candidates, selected=selected)["valid"]:
                return None
            labels = ", ".join(str(fact["entity_label"]) for fact in selected)
            timeframe = packet.get("timeframe") if isinstance(packet.get("timeframe"), dict) else {}
            timeframe_label = str(timeframe.get("label") or "").strip()
            return (
                f"From the saved analysis{f' for {timeframe_label}' if timeframe_label else ''}, "
                f"{labels} recorded no sales."
            )
    else:
        selector = min if direction == "minimum" else max
        selected_value = selector(_numeric(fact.get("value")) or 0 for fact in candidates)
        selected = [fact for fact in candidates if (_numeric(fact.get("value")) or 0) == selected_value]
        if not validate_analytic_selection(frame=frame, candidates=candidates, selected=selected)["valid"]:
            return None
        qualifier = "lowest" if direction == "minimum" else "highest"
        prefix = f"had the {qualifier}"

    labels = ", ".join(str(fact["entity_label"]) for fact in selected)
    value = _numeric(selected[0].get("value")) or 0
    unit = str(selected[0].get("unit") or "count")
    currency = str(selected[0].get("currency_code") or "").upper() or None
    timeframe = packet.get("timeframe") if isinstance(packet.get("timeframe"), dict) else {}
    timeframe_label = str(timeframe.get("label") or "").strip()
    metric_label = {
        "sales_revenue": "sales",
        "units_sold": "units sold",
        "order_count": "orders",
    }.get(str(frame["metric"]), str(frame["metric"]).replace("_", " "))
    comparison_suffix = ""
    if direction == "minimum" and len(selected) == 1:
        leading = max(candidates, key=lambda item: _numeric(item.get("value")) or 0)
        leading_value = _numeric(leading.get("value")) or 0
        if leading_value > value:
            comparison_suffix = (
                f" It trailed {leading['entity_label']} by "
                f"{_format_value(leading_value - value, unit=unit, currency_code=currency)}."
            )
    return (
        f"From the saved analysis{f' for {timeframe_label}' if timeframe_label else ''}, "
        f"{labels} {prefix} {metric_label} at {_format_value(value, unit=unit, currency_code=currency)}."
        f"{comparison_suffix}"
    )


def _is_scope_only_location_fact(fact: dict[str, Any]) -> bool:
    """Reject legacy false facts created from inventory scope metric grids.

    Older persisted packets could infer a location dimension from titles such
    as ``Inventory risk posture - All locations`` and expose labels like
    ``Needs Reorder`` as zero-sales locations. Keep those packets readable,
    but never allow them to answer a sales ranking.
    """
    if str(fact.get("dimension") or "") != "location":
        return False
    source = _normalise(fact.get("source_widget"))
    return "inventory" in source or "risk posture" in source or "stock risk" in source


def validate_fact_packet(*, packet: dict[str, Any]) -> dict[str, Any]:
    """Expose deterministic evidence quality for observability and composition."""
    facts = packet.get("facts") if isinstance(packet.get("facts"), list) else []
    valid = [fact for fact in facts if isinstance(fact, dict) and _numeric(fact.get("value")) is not None]
    return {
        "valid": bool(valid),
        "fact_count": len(valid),
        "schema_version": packet.get("schema_version"),
        "reason": None if valid else "No numeric canonical facts were available for this insight.",
    }


def validate_insight_response_contract(*, payload: dict[str, Any]) -> dict[str, Any]:
    """Check the consistency of a structured insight before it is observed.

    This is intentionally an observation contract first.  Existing insight
    builders may be text-only, so the absence of a fact packet is not itself a
    failure; malformed widgets, contradictory review coverage, and invalid
    attached packets are failures that must remain visible to operators and
    decisioning.
    """
    errors: list[str] = []
    if str(payload.get("kind") or "").strip() != "insight_response":
        errors.append("Payload is not an insight response.")

    widgets = payload.get("widgets") if isinstance(payload.get("widgets"), list) else None
    if widgets is None:
        errors.append("Insight response is missing its widget list.")

    coverage = payload.get("review_coverage")
    if isinstance(coverage, dict):
        status = str(coverage.get("status") or "").strip().lower()
        attempted = _numeric(coverage.get("sources_attempted"))
        available = _numeric(coverage.get("sources_available"))
        if status not in {"complete", "partial", "unavailable"}:
            errors.append("Insight review coverage has an unknown status.")
        if attempted is not None and available is not None and available > attempted:
            errors.append("Insight review coverage reports more available sources than attempted sources.")
        if status == "complete" and attempted is not None and available is not None and available < attempted:
            errors.append("Insight review is marked complete although a source is unavailable.")

    packet = payload.get(FACT_PACKET_KEY)
    packet_validation: dict[str, Any] | None = None
    if isinstance(packet, dict):
        packet_validation = validate_fact_packet(packet=packet)
        if not packet_validation["valid"]:
            errors.append("Attached analytics evidence contains no valid numeric facts.")

    return {
        "valid": not errors,
        "status": "valid" if not errors else "invalid",
        "errors": errors,
        "fact_validation": packet_validation,
    }


def _deduplicate_insight_widgets(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Remove only byte-for-byte equivalent widgets, preserving their order.

    Distinct stock views can legitimately describe the same product, so this
    deliberately does not attempt semantic deduplication. Exact duplicates
    are a composition defect and are safe to collapse at the response boundary.
    """
    widgets = payload.get("widgets")
    if not isinstance(widgets, list):
        return [], 0

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    removed = 0
    for widget in widgets:
        if not isinstance(widget, dict):
            unique.append(widget)
            continue
        fingerprint = json.dumps(widget, sort_keys=True, separators=(",", ":"), default=str)
        if fingerprint in seen:
            removed += 1
            continue
        seen.add(fingerprint)
        unique.append(widget)
    return unique, removed


def normalize_insight_response_contract(*, payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize response quality before an insight reaches a client."""
    validation = validate_insight_response_contract(payload=payload)
    coverage = payload.get("review_coverage")
    widgets, duplicates_removed = _deduplicate_insight_widgets(payload)
    if validation["valid"] and not duplicates_removed:
        return payload

    normalized = dict(payload)
    if duplicates_removed:
        normalized["widgets"] = widgets
        warnings = [
            str(item).strip()
            for item in (payload.get("warnings") if isinstance(payload.get("warnings"), list) else [])
            if str(item).strip()
        ]
        warnings.append(f"Removed {duplicates_removed} exact duplicate insight widget(s).")
        normalized["warnings"] = list(dict.fromkeys(warnings))[:12]

    if validation["valid"] or not isinstance(coverage, dict):
        return normalized

    attempted = _numeric(coverage.get("sources_attempted"))
    available = _numeric(coverage.get("sources_available"))
    status = str(coverage.get("status") or "").strip().lower()
    if status != "complete" or attempted is None or available is None or available >= attempted:
        return payload

    normalized["review_coverage"] = {**coverage, "status": "partial"}
    normalized["confidence"] = "low"
    warnings = [
        str(item).strip()
        for item in (payload.get("warnings") if isinstance(payload.get("warnings"), list) else [])
        if str(item).strip()
    ]
    warnings.append("This review is marked partial because one or more data checks were unavailable.")
    normalized["warnings"] = list(dict.fromkeys(warnings))[:12]
    return normalized
