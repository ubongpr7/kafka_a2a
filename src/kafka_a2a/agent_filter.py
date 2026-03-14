from __future__ import annotations

import os
from collections.abc import Iterable

from kafka_a2a.models import AgentCard


def allowed_agent_names_from_env() -> set[str]:
    raw = (
        os.getenv("KA2A_ALLOWED_DOWNSTREAM_AGENTS")
        or os.getenv("KA2A_ALLOWED_AGENT_NAMES")
        or ""
    ).strip()
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def filter_agent_cards(
    cards: Iterable[AgentCard],
    *,
    exclude_names: set[str] | None = None,
    include_names: set[str] | None = None,
) -> list[AgentCard]:
    excluded = set(exclude_names or set())
    included = set(include_names or set())
    allowed = allowed_agent_names_from_env()

    out: list[AgentCard] = []
    for card in cards:
        name = (card.name or "").strip()
        if not name or name in excluded:
            continue
        if allowed and name not in allowed and name not in included:
            continue
        out.append(card)
    return out
