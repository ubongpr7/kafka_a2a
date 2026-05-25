from __future__ import annotations

import pytest

from kafka_a2a.marketplace_tools import MarketplaceSourcingToolExecutor, compare_marketplace_products_tool
from kafka_a2a.tools import ToolContext


@pytest.mark.asyncio
async def test_marketplace_search_returns_structured_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_search_raw(**kwargs):
        query = kwargs["query"]
        if "amazon" in query:
            return {
                "results": [
                    {
                        "title": "Wireless Barcode Scanner $24.99 Free shipping",
                        "url": "https://amazon.com/example-scanner",
                        "content": "4.7 out of 5 stars handheld scanner",
                        "score": 0.82,
                        "images": ["https://cdn.example.com/scanner.jpg"],
                        "favicon": "https://cdn.example.com/amazon.ico",
                    }
                ]
            }
        return {
            "results": [
                {
                    "title": "Wireless Barcode Scanner USD 21.50",
                    "url": "https://ebay.com/example-scanner",
                    "content": "shipping $3.50 and rating 4.5/5",
                    "score": 0.8,
                }
            ]
        }

    monkeypatch.setattr("kafka_a2a.marketplace_tools.tavily_search_raw", fake_search_raw)
    monkeypatch.setattr(
        "kafka_a2a.marketplace_tools.resolve_tavily_credentials_from_metadata",
        lambda **kwargs: type("Cred", (), {"api_key": "tvly-user"})(),
    )

    executor = MarketplaceSourcingToolExecutor()
    payload = await executor.call_tool(
        name="search_marketplace_products",
        arguments={"query": "wireless barcode scanner", "marketplaces": ["amazon", "ebay"], "max_results": 6},
        ctx=ToolContext(metadata={"urn:ka2a:principal": {}}),
    )

    assert payload["interaction_type"] == "marketplace_results"
    assert len(payload["products"]) == 2
    assert payload["products"][0]["marketplace"] in {"Amazon", "eBay"}
    assert any(item["image_url"] for item in payload["products"])


def test_compare_marketplace_products_returns_comparison_view() -> None:
    payload = compare_marketplace_products_tool(
        items=[
            {
                "id": "1",
                "title": "Offer A",
                "marketplace": "Amazon",
                "price": "USD 25.00",
                "shipping": "Free shipping",
                "total_price": "USD 25.00",
                "rating": "4.7/5",
                "source_domain": "amazon.com",
            },
            {
                "id": "2",
                "title": "Offer B",
                "marketplace": "eBay",
                "price": "USD 23.00",
                "shipping": "USD 3.00",
                "total_price": "USD 26.00",
                "rating": "4.5/5",
                "source_domain": "ebay.com",
            },
        ]
    )

    assert payload["interaction_type"] == "comparison_view"
    assert payload["items"][0]["name"] == "Offer A"
    assert "total_price" in payload["comparison_fields"]


@pytest.mark.asyncio
async def test_marketplace_search_requires_workspace_tavily_key() -> None:
    executor = MarketplaceSourcingToolExecutor()
    with pytest.raises(ValueError, match="Tavily API key is not configured"):
        await executor.call_tool(
            name="search_marketplace_products",
            arguments={"query": "thermal printer"},
            ctx=ToolContext(metadata={}),
        )
