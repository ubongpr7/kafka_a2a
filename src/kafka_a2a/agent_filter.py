from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from kafka_a2a.models import AgentCard


def _card_extra(card: AgentCard, key: str) -> Any:
    extra = getattr(card, "__pydantic_extra__", None) or {}
    if key in extra:
        return extra.get(key)
    return getattr(card, key, None)


def _runtime_metadata(card: AgentCard) -> dict[str, Any]:
    metadata = _card_extra(card, "metadata")
    if not isinstance(metadata, dict):
        return {}
    runtime = metadata.get("ka2aRuntime")
    return runtime if isinstance(runtime, dict) else {}


def card_public_slug(card: AgentCard) -> str:
    runtime = _runtime_metadata(card)
    public_slug = runtime.get("publicSlug")
    if isinstance(public_slug, str) and public_slug.strip():
        return public_slug.strip()
    return (card.name or "").strip()


def card_profile_id(card: AgentCard) -> str | None:
    runtime = _runtime_metadata(card)
    profile_id = runtime.get("profileId")
    if profile_id is None:
        return None
    value = str(profile_id).strip()
    return value or None


def allowed_agent_names_from_env() -> set[str]:
    raw = (
        os.getenv("KA2A_ALLOWED_DOWNSTREAM_AGENTS")
        or os.getenv("KA2A_ALLOWED_AGENT_NAMES")
        or ""
    ).strip()
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def required_profile_id_from_env() -> str | None:
    raw = (os.getenv("KA2A_WORKSPACE_PROFILE_ID") or os.getenv("KA2A_RUNTIME_PROFILE_ID") or "").strip()
    return raw or None


def _matches_slug_allowlist(value: str, allowlist: set[str]) -> bool:
    normalized = str(value or "").strip()
    if not normalized:
        return False
    if normalized in allowlist:
        return True
    return any(
        normalized.startswith(f"{allowed}_") or normalized.startswith(f"{allowed}-")
        for allowed in allowlist
    )


def filter_agent_cards(
    cards: Iterable[AgentCard],
    *,
    exclude_names: set[str] | None = None,
    include_names: set[str] | None = None,
    required_profile_id: str | None = None,
    allowed_public_slugs: set[str] | None = None,
) -> list[AgentCard]:
    excluded = set(exclude_names or set())
    included = set(include_names or set())
    allowed = allowed_agent_names_from_env()
    allowed_public = set(allowed_public_slugs or set())
    required_profile = required_profile_id or required_profile_id_from_env()

    out: list[AgentCard] = []
    for card in cards:
        name = (card.name or "").strip()
        public_slug = card_public_slug(card)
        if not name or name in excluded or public_slug in excluded:
            continue
        if required_profile:
            profile_id = card_profile_id(card)
            if profile_id is None or profile_id != required_profile:
                continue
        if (
            allowed
            and not _matches_slug_allowlist(name, allowed)
            and not _matches_slug_allowlist(public_slug, allowed)
            and name not in included
            and public_slug not in included
        ):
            continue
        if (
            allowed_public
            and not _matches_slug_allowlist(public_slug, allowed_public)
            and name not in included
            and public_slug not in included
        ):
            continue
        out.append(card)
    return out
