from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

from kafka_a2a.credentials import resolve_tavily_credentials_from_metadata
from kafka_a2a.secrets import decrypt_fernet_secret
from kafka_a2a.tavily import tavily_search_raw
from kafka_a2a.tools import ToolContext, ToolExecutor, ToolSpec


_PRICE_PATTERN = re.compile(
    r"(?P<currency>[$€£¥₦]|USD|EUR|GBP|JPY|CNY|RMB|NGN)\s*(?P<amount>\d[\d,]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_RATING_PATTERN = re.compile(r"(?P<rating>\d(?:\.\d)?)\s*(?:/|out of)\s*5", re.IGNORECASE)
_STAR_RATING_PATTERN = re.compile(r"(?P<rating>\d(?:\.\d)?)\s*stars?", re.IGNORECASE)
_FREE_SHIPPING_PATTERN = re.compile(r"\bfree shipping\b", re.IGNORECASE)
_SHIPPING_PATTERN = re.compile(
    r"(?:shipping|delivery)\s*(?:from|at|for)?\s*(?P<currency>[$€£¥₦]|USD|EUR|GBP|JPY|CNY|RMB|NGN)\s*(?P<amount>\d[\d,]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_WHITESPACE_PATTERN = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class MarketplaceDefinition:
    key: str
    label: str
    domains: tuple[str, ...]
    query_hint: str


MARKETPLACES: dict[str, MarketplaceDefinition] = {
    "amazon": MarketplaceDefinition("amazon", "Amazon", ("amazon.com",), "site:amazon.com"),
    "ebay": MarketplaceDefinition("ebay", "eBay", ("ebay.com",), "site:ebay.com"),
    "aliexpress": MarketplaceDefinition("aliexpress", "AliExpress", ("aliexpress.com",), "site:aliexpress.com"),
    "alibaba": MarketplaceDefinition("alibaba", "Alibaba", ("alibaba.com",), "site:alibaba.com"),
    "temu": MarketplaceDefinition("temu", "Temu", ("temu.com",), "site:temu.com"),
    "dhgate": MarketplaceDefinition("dhgate", "DHgate", ("dhgate.com",), "site:dhgate.com"),
}

DEFAULT_MARKETPLACE_KEYS: tuple[str, ...] = ("amazon", "ebay", "aliexpress", "alibaba", "temu", "dhgate")


MARKETPLACE_TOOL_SPECS: dict[str, ToolSpec] = {
    "search_marketplace_products": ToolSpec(
        name="search_marketplace_products",
        description="Search multiple online marketplaces with the workspace Tavily API key and return normalized product cards.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "marketplaces": {"type": "array", "items": {"type": "string"}},
                "max_results": {"type": "integer"},
                "country": {"type": "string"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    "compare_marketplace_products": ToolSpec(
        name="compare_marketplace_products",
        description="Build a side-by-side comparison view for selected marketplace products.",
        input_schema={
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "comparison_fields": {"type": "array", "items": {"type": "string"}},
                "title": {"type": "string"},
            },
            "required": ["items"],
            "additionalProperties": False,
        },
    ),
}


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return _WHITESPACE_PATTERN.sub(" ", value).strip()


def _as_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", ""))
    except Exception:
        return None


def _currency_code(value: str | None) -> str | None:
    if not value:
        return None
    token = value.strip().upper()
    mapping = {
        "$": "USD",
        "€": "EUR",
        "£": "GBP",
        "¥": "JPY",
        "₦": "NGN",
        "RMB": "CNY",
    }
    return mapping.get(token, token)


def _extract_price(text: str) -> tuple[str | None, float | None, str | None]:
    match = _PRICE_PATTERN.search(text)
    if not match:
        return None, None, None
    currency = _currency_code(match.group("currency"))
    amount = _as_float(match.group("amount"))
    if amount is None:
        return None, None, currency
    return f"{currency or ''} {amount:,.2f}".strip(), amount, currency


def _extract_shipping(text: str) -> tuple[str | None, float | None]:
    if _FREE_SHIPPING_PATTERN.search(text):
        return "Free shipping", 0.0
    match = _SHIPPING_PATTERN.search(text)
    if not match:
        return None, None
    currency = _currency_code(match.group("currency")) or ""
    amount = _as_float(match.group("amount"))
    if amount is None:
        return None, None
    return f"{currency} {amount:,.2f}".strip(), amount


def _extract_rating(text: str) -> str | None:
    match = _RATING_PATTERN.search(text) or _STAR_RATING_PATTERN.search(text)
    if not match:
        return None
    return f"{match.group('rating')}/5"


def _extract_domain(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.netloc or "").lower().removeprefix("www.")


def _favicon_for_url(url: str) -> str:
    domain = _extract_domain(url)
    if not domain:
        return ""
    return f"https://www.google.com/s2/favicons?sz=64&domain_url={domain}"


def _first_image(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("url", "src", "image_url"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None
    if isinstance(value, list):
        for item in value:
            resolved = _first_image(item)
            if resolved:
                return resolved
    return None


def _result_identifier(url: str, title: str) -> str:
    token = url.strip() or title.strip()
    return str(uuid5(NAMESPACE_URL, token))


def _normalize_marketplace_keys(marketplaces: list[str] | None) -> list[str]:
    if not marketplaces:
        return list(DEFAULT_MARKETPLACE_KEYS)
    out: list[str] = []
    aliases = {
        "amazon": "amazon",
        "ebay": "ebay",
        "aliexpress": "aliexpress",
        "alibaba": "alibaba",
        "temu": "temu",
        "dhgate": "dhgate",
    }
    for item in marketplaces:
        raw = _clean_text(item).lower().replace(" ", "").replace("-", "")
        key = aliases.get(raw, raw)
        if key in MARKETPLACES and key not in out:
            out.append(key)
    return out or list(DEFAULT_MARKETPLACE_KEYS)


async def _search_single_marketplace(
    *,
    api_key: str,
    marketplace: MarketplaceDefinition,
    query: str,
    max_results: int,
    country: str | None,
) -> list[dict[str, Any]]:
    raw = await tavily_search_raw(
        api_key=api_key,
        query=f"{query} {marketplace.query_hint}",
        max_results=max(1, min(max_results, 6)),
        search_depth="advanced",
        include_images=True,
        include_favicon=True,
        include_domains=list(marketplace.domains),
        topic="general",
        country=country,
    )
    raw_results = raw.get("results") if isinstance(raw, dict) else []
    if not isinstance(raw_results, list):
        return []
    top_level_images = raw.get("images") if isinstance(raw, dict) and isinstance(raw.get("images"), list) else []
    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_results):
        if not isinstance(entry, dict):
            continue
        title = _clean_text(entry.get("title"))
        url = _clean_text(entry.get("url"))
        snippet = _clean_text(entry.get("content"))
        if not title or not url:
            continue
        joined_text = " ".join(part for part in (title, snippet) if part)
        price_display, price_value, currency = _extract_price(joined_text)
        shipping_display, shipping_value = _extract_shipping(joined_text)
        total_value = None
        total_display = None
        if price_value is not None:
            total_value = price_value + (shipping_value or 0.0)
            total_display = f"{currency or ''} {total_value:,.2f}".strip()
        normalized.append(
            {
                "id": _result_identifier(url, title),
                "title": title,
                "description": snippet,
                "marketplace": marketplace.label,
                "marketplace_key": marketplace.key,
                "product_url": url,
                "image_url": _first_image(entry.get("images")) or _first_image(top_level_images[index:index + 1]),
                "favicon_url": _clean_text(entry.get("favicon")) or _favicon_for_url(url),
                "price": price_display,
                "price_value": price_value,
                "currency": currency,
                "shipping": shipping_display,
                "shipping_value": shipping_value,
                "total_price": total_display or price_display,
                "total_price_value": total_value if total_value is not None else price_value,
                "rating": _extract_rating(joined_text),
                "score": float(entry.get("score")) if isinstance(entry.get("score"), (int, float)) else None,
                "source_domain": _extract_domain(url),
            }
        )
    return normalized


def _dedupe_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for item in results:
        key = (str(item.get("product_url") or "").strip().lower(), str(item.get("title") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _sort_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        results,
        key=lambda item: (
            item.get("price_value") is None,
            item.get("price_value") if isinstance(item.get("price_value"), (int, float)) else float("inf"),
            -float(item.get("score") or 0.0),
        ),
    )


def _search_summary(*, query: str, products: list[dict[str, Any]], marketplaces: list[MarketplaceDefinition]) -> dict[str, Any]:
    cheapest = next(
        (
            item
            for item in products
            if isinstance(item.get("total_price_value"), (int, float)) or isinstance(item.get("price_value"), (int, float))
        ),
        None,
    )
    return {
        "query": query,
        "marketplaces": [item.label for item in marketplaces],
        "result_count": len(products),
        "cheapest_offer": (
            {
                "title": cheapest.get("title"),
                "marketplace": cheapest.get("marketplace"),
                "price": cheapest.get("total_price") or cheapest.get("price"),
                "product_url": cheapest.get("product_url"),
            }
            if cheapest
            else None
        ),
    }


async def search_marketplace_products_tool(
    *,
    query: str,
    api_key: str,
    marketplaces: list[str] | None = None,
    max_results: int = 12,
    country: str | None = None,
) -> dict[str, Any]:
    query_value = _clean_text(query)
    if not query_value:
        raise ValueError("query is required.")
    selected_keys = _normalize_marketplace_keys(marketplaces)
    selected = [MARKETPLACES[key] for key in selected_keys]
    per_marketplace = max(1, min(6, max_results // max(len(selected), 1) or 1))
    gathered: list[dict[str, Any]] = []
    for marketplace in selected:
        gathered.extend(
            await _search_single_marketplace(
                api_key=api_key,
                marketplace=marketplace,
                query=query_value,
                max_results=per_marketplace,
                country=country,
            )
        )
    products = _sort_results(_dedupe_results(gathered))[: max(1, min(max_results, 24))]
    description = (
        f"Found {len(products)} marketplace matches across {', '.join(item.label for item in selected)}."
        if products
        else f"No marketplace matches were found for “{query_value}”. Try a broader product name or fewer marketplaces."
    )
    return {
        "interaction_type": "marketplace_results",
        "title": f"Marketplace results for “{query_value}”",
        "description": description,
        "query": query_value,
        "products": products,
        "summary": _search_summary(query=query_value, products=products, marketplaces=selected),
        "available_marketplaces": [item.label for item in selected],
        "allow_selection": True,
        "allow_compare": True,
        "max_selection": 4,
        "workflow": "marketplace_sourcing",
        "workflow_stage": "results",
    }


def compare_marketplace_products_tool(
    *,
    items: list[dict[str, Any]],
    comparison_fields: list[str] | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    normalized_items = [item for item in items if isinstance(item, dict) and _clean_text(item.get("title"))]
    if len(normalized_items) < 2:
        raise ValueError("Select at least two marketplace products to compare.")
    fields = comparison_fields or ["marketplace", "price", "shipping", "total_price", "rating", "source_domain"]
    return {
        "interaction_type": "comparison_view",
        "title": title or "Compare marketplace products",
        "description": "Review the selected marketplace offers side by side.",
        "items": [
            {
                "id": item.get("id") or _result_identifier(_clean_text(item.get("product_url")), _clean_text(item.get("title"))),
                "name": _clean_text(item.get("title")),
                "marketplace": _clean_text(item.get("marketplace")),
                "price": _clean_text(item.get("price")),
                "shipping": _clean_text(item.get("shipping")),
                "total_price": _clean_text(item.get("total_price")),
                "rating": _clean_text(item.get("rating")),
                "source_domain": _clean_text(item.get("source_domain")),
            }
            for item in normalized_items[:6]
        ],
        "comparison_fields": fields,
        "allow_selection": True,
        "highlight_differences": True,
        "workflow": "marketplace_sourcing",
        "workflow_stage": "comparison",
    }


class MarketplaceSourcingToolExecutor(ToolExecutor):
    async def list_tools(self, *, ctx: ToolContext) -> list[ToolSpec]:
        _ = ctx
        return list(MARKETPLACE_TOOL_SPECS.values())

    async def call_tool(self, *, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        payload = arguments or {}
        if name == "search_marketplace_products":
            credentials = resolve_tavily_credentials_from_metadata(
                metadata=ctx.metadata,
                decrypt=decrypt_fernet_secret,
            )
            if credentials is None or not getattr(credentials, "api_key", "").strip():
                raise ValueError("Tavily API key is not configured for this workspace agent.")
            return await search_marketplace_products_tool(
                query=str(payload.get("query") or "").strip(),
                api_key=credentials.api_key,
                marketplaces=[str(item) for item in payload.get("marketplaces") or []] or None,
                max_results=int(payload.get("max_results") or 12),
                country=str(payload.get("country") or "").strip() or None,
            )
        if name == "compare_marketplace_products":
            items = payload.get("items")
            if not isinstance(items, list):
                raise ValueError("items must be an array.")
            return compare_marketplace_products_tool(
                items=[item for item in items if isinstance(item, dict)],
                comparison_fields=[str(item) for item in payload.get("comparison_fields") or []] or None,
                title=str(payload.get("title") or "").strip() or None,
            )
        raise ValueError(f"Unknown marketplace sourcing tool: {name}")


def build_marketplace_sourcing_tool_executor(*, agent_name: str | None = None) -> ToolExecutor:
    _ = agent_name
    return MarketplaceSourcingToolExecutor()
