from __future__ import annotations

import logging

from kafka_a2a.decisioning.config import DecisioningConfig
from kafka_a2a.decisioning.facade import AgentDecisionFacade
from kafka_a2a.decisioning.provider_typesafe import TypeSafeDecisionProvider
from kafka_a2a.decisioning.service import DecisionService

logger = logging.getLogger("kafka_a2a.decisioning")


class DecisionRuntime:
    """Process-scoped runtime shared by tenant agents without sharing tenant credentials."""

    def __init__(self, *, config: DecisioningConfig, service: DecisionService) -> None:
        self.config = config
        self._service = service

    def facade_for(self, agent_slug: str | None) -> AgentDecisionFacade:
        return AgentDecisionFacade(
            service=self._service,
            config=self.config,
            agent_slug=str(agent_slug or "host").strip() or "host",
        )

    async def aclose(self, *, drain_timeout_s: float | None = None) -> None:
        await self._service.aclose(drain_timeout_s=drain_timeout_s)


def build_decision_runtime_from_env() -> DecisionRuntime:
    """Build a safe no-op runtime when platform decisioning is not configured."""

    try:
        config = DecisioningConfig.from_env()
    except ValueError:
        logger.warning("invalid decisioning configuration; disabling decisioning", exc_info=True)
        config = DecisioningConfig.disabled()

    return _build_runtime(config)


def build_voice_decision_runtime_from_env() -> DecisionRuntime:
    """Build a LiveKit-safe runtime without exposing provider details to voice code."""

    try:
        config = DecisioningConfig.from_env().for_voice()
    except ValueError:
        logger.warning("invalid voice decisioning configuration; disabling decisioning", exc_info=True)
        config = DecisioningConfig.disabled()
    return _build_runtime(config)


def _build_runtime(config: DecisioningConfig) -> DecisionRuntime:
    provider = None
    if config.is_active:
        provider = TypeSafeDecisionProvider(config=config)
    elif config.enabled and config.provider != "typesafe":
        logger.warning("unsupported decisioning provider configured; decisioning is disabled", extra={"provider": config.provider})
    return DecisionRuntime(config=config, service=DecisionService(config=config, provider=provider))
