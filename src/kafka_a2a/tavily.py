from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kafka_a2a.llms.controls import RetryConfig, backoff_delay_s


TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class TavilyHttpError(RuntimeError):
    def __init__(self, *, status: int, body: str) -> None:
        super().__init__(f"Tavily HTTP {status}")
        self.status = int(status)
        self.body = body


@dataclass(slots=True)
class TavilySearchResult:
    title: str
    url: str
    content: str | None = None
    score: float | None = None
    published_date: str | None = None


def _parse_results(obj: Any) -> list[TavilySearchResult]:
    if not isinstance(obj, dict):
        return []
    raw_results = obj.get("results")
    if not isinstance(raw_results, list):
        return []
    out: list[TavilySearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        url = item.get("url")
        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(url, str) or not url.strip():
            continue
        content = item.get("content")
        score = item.get("score")
        published_date = item.get("published_date") or item.get("publishedDate")
        out.append(
            TavilySearchResult(
                title=title.strip(),
                url=url.strip(),
                content=content.strip() if isinstance(content, str) and content.strip() else None,
                score=float(score) if isinstance(score, (int, float)) else None,
                published_date=str(published_date).strip()
                if isinstance(published_date, str) and published_date.strip()
                else None,
            )
        )
    return out


async def tavily_search(
    *,
    api_key: str,
    query: str,
    max_results: int = 5,
    search_depth: str = "basic",
    include_answer: bool = False,
    include_raw_content: bool = False,
    timeout_s: float = 30.0,
    retry: RetryConfig | None = None,
) -> list[TavilySearchResult]:
    """
    Call Tavily Search API (REST) using stdlib urllib.

    Notes:
    - Tavily requires `api_key` in the JSON body.
    - We keep this dependency-free so K-A2A stays installable without extra client libs.
    """

    query = (query or "").strip()
    if not query:
        return []

    payload: dict[str, Any] = {
        "api_key": api_key,
        "query": query,
        "max_results": max(1, min(int(max_results), 10)),
        "search_depth": (search_depth or "basic").strip(),
        "include_answer": bool(include_answer),
        "include_raw_content": bool(include_raw_content),
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"content-type": "application/json"}

    def _post() -> dict[str, Any]:
        req = Request(TAVILY_SEARCH_URL, data=body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=float(timeout_s)) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            raise TavilyHttpError(status=int(getattr(exc, "code", 0) or 0), body=err_body) from exc

    cfg = retry or RetryConfig.from_env()
    attempt = 0
    while True:
        try:
            obj = await asyncio.to_thread(_post)
            return _parse_results(obj)
        except TavilyHttpError as exc:
            retryable = exc.status in (408, 409, 425, 429, 500, 502, 503, 504)
            if attempt >= max(0, cfg.max_retries) or not retryable:
                raise RuntimeError(f"Tavily search failed ({exc.status}): {exc.body or str(exc)}") from exc
            await asyncio.sleep(backoff_delay_s(attempt=attempt, cfg=cfg))
            attempt += 1

