from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kafka_a2a.models import AgentCard
from kafka_a2a.registry.kafka_registry import KafkaAgentRegistry


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(slots=True)
class KafkaAgentDirectoryConfig:
    group_id: str = "ka2a.directory"
    auto_offset_reset: str = "latest"
    entry_ttl_s: float | None = 300.0
    prune_interval_s: float = 30.0


@dataclass(slots=True)
class DirectoryEntry:
    card: AgentCard
    last_seen: datetime


class KafkaAgentDirectory:
    """
    Maintains an in-memory view of agent cards published to the registry topic.

    This is a convenience layer over `KafkaAgentRegistry.watch()` that adds caching
    and optional TTL-based pruning.
    """

    def __init__(self, *, registry: KafkaAgentRegistry, config: KafkaAgentDirectoryConfig | None = None):
        self._registry = registry
        self._cfg = config or KafkaAgentDirectoryConfig()
        self._entries: dict[str, DirectoryEntry] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._prune_task: asyncio.Task[None] | None = None

    def get(self, agent_name: str) -> AgentCard | None:
        entry = self._entries.get(agent_name)
        return entry.card if entry else None

    def list(self) -> list[AgentCard]:
        return [entry.card for entry in self._entries.values()]

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()

        async def _consume() -> None:
            async for entry in self._registry.watch(
                group_id=self._cfg.group_id,
                auto_offset_reset=self._cfg.auto_offset_reset,
                stop_event=self._stop,
            ):
                self._entries[entry.agent_name] = DirectoryEntry(card=entry.card, last_seen=_utc_now())
                if self._stop.is_set():
                    break

        async def _prune() -> None:
            if self._cfg.entry_ttl_s is None:
                return
            ttl = timedelta(seconds=self._cfg.entry_ttl_s)
            while not self._stop.is_set():
                cutoff = _utc_now() - ttl
                for name, entry in list(self._entries.items()):
                    if entry.last_seen < cutoff:
                        self._entries.pop(name, None)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._cfg.prune_interval_s)
                except asyncio.TimeoutError:
                    continue

        self._task = asyncio.create_task(_consume())
        self._prune_task = asyncio.create_task(_prune())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._prune_task is not None:
            self._prune_task.cancel()
            try:
                await self._prune_task
            except asyncio.CancelledError:
                pass
            self._prune_task = None
