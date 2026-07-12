"""
Deterministic insight widget helpers for the A2A frontend contract.

These helpers return validated dictionaries that the frontend can render
without parsing arbitrary model prose.
"""

from __future__ import annotations

from typing import Any


def _clean_mapping(value: dict[str, Any] | None) -> dict[str, Any]:
    return {key: item for key, item in (value or {}).items() if item is not None}


def _clean_widget(widget_type: str, *, title: str | None = None, **payload: Any) -> dict[str, Any]:
    widget = {
        "type": widget_type,
        **({"title": title} if title else {}),
        **_clean_mapping(payload),
    }
    return widget


def render_metric_grid(metrics: list[dict[str, Any]], title: str | None = None) -> dict[str, Any]:
    return _clean_widget("metric_grid", title=title, data=metrics or [])


def render_bar_chart(
    series: list[dict[str, Any]],
    title: str | None = None,
    x_key: str = "label",
    y_key: str = "value",
) -> dict[str, Any]:
    return _clean_widget("bar_chart", title=title, data=series or [], x_key=x_key, y_key=y_key)


def render_donut_chart(
    series: list[dict[str, Any]],
    title: str | None = None,
    value_key: str = "value",
    label_key: str = "label",
) -> dict[str, Any]:
    return _clean_widget("donut_chart", title=title, data=series or [], value_key=value_key, label_key=label_key)


def render_line_chart(
    points: list[dict[str, Any]],
    title: str | None = None,
    x_key: str = "label",
    y_key: str = "value",
) -> dict[str, Any]:
    return _clean_widget("line_chart", title=title, data=points or [], x_key=x_key, y_key=y_key)


def render_ranked_list(
    items: list[dict[str, Any]],
    title: str | None = None,
    ordered_by: str | None = None,
) -> dict[str, Any]:
    return _clean_widget("ranked_list", title=title, items=items or [], ordered_by=ordered_by)


def render_risk_panel(
    risks: list[dict[str, Any]],
    title: str | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    return _clean_widget("risk_panel", title=title, items=risks or [], severity=severity)


def render_comparison_table(
    columns: list[dict[str, Any]] | list[str],
    rows: list[dict[str, Any]],
    title: str | None = None,
) -> dict[str, Any]:
    return _clean_widget("comparison_table", title=title, columns=columns or [], rows=rows or [])


def render_timeline(
    events: list[dict[str, Any]],
    title: str | None = None,
) -> dict[str, Any]:
    return _clean_widget("timeline", title=title, events=events or [])


def render_progress_tracker(
    steps: list[dict[str, Any]],
    title: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    return _clean_widget("progress_tracker", title=title, steps=steps or [], status=status)


def render_action_form(
    schema: list[dict[str, Any]],
    defaults: dict[str, Any] | None = None,
    title: str | None = None,
    submit_label: str | None = None,
) -> dict[str, Any]:
    return _clean_widget(
        "action_form",
        title=title,
        fields=schema or [],
        defaults=defaults or {},
        submit_label=submit_label,
    )


def render_confirmation_card(
    action: str,
    summary: str,
    risk_level: str = "medium",
    title: str | None = None,
    confirm_payload: dict[str, Any] | None = None,
    cancel_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _clean_widget(
        "confirmation_card",
        title=title,
        action=action,
        summary=summary,
        risk_level=risk_level,
        confirm_payload=confirm_payload or {},
        cancel_payload=cancel_payload or {},
    )


def render_entity_preview(
    entity: dict[str, Any],
    title: str | None = None,
) -> dict[str, Any]:
    return _clean_widget("entity_preview", title=title, entity=entity or {})


def create_insight_response(
    summary: str,
    widgets: list[dict[str, Any]],
    suggested_actions: list[dict[str, Any]] | None = None,
    data_sources: list[dict[str, Any]] | None = None,
    permissions_checked: list[str] | None = None,
    confidence: str = "high",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "kind": "insight_response",
        "summary": str(summary or "").strip(),
        "widgets": widgets or [],
        "suggested_actions": suggested_actions or [],
        "data_sources": data_sources or [],
        "permissions_checked": permissions_checked or [],
        "confidence": confidence or "high",
        "warnings": warnings or [],
    }
