from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from kafka_a2a.core.config import A2AAppSettings

from .models import (
    ConversationActivityRecord,
    ConversationDetail,
    ConversationMessageKind,
    ConversationMessageRecord,
    ConversationMessageRole,
    ConversationRecord,
)


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _preview_text(*, content: str = "", structured_payload: dict[str, Any] | None = None, role: str = "") -> str:
    text = (content or "").strip()
    if text:
        return text[:400]
    payload = structured_payload or {}
    if payload:
        title = str(payload.get("title") or payload.get("type") or "").strip()
        if title:
            return title[:120]
        return "Structured response"
    if role == ConversationMessageRole.user.value:
        return "User message"
    if role == ConversationMessageRole.assistant.value:
        return "Assistant response"
    return "System update"


class ConversationStore(Protocol):
    async def create_conversation(
        self,
        *,
        profile_id: str,
        user_id: str,
        agent_slug: str,
        agent_name: str,
        agent_icon_url: str = "",
        runtime_agent_name: str,
        title: str = "",
        history_length: int = 10,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationRecord: ...

    async def list_conversations(
        self,
        *,
        profile_id: str,
        user_id: str,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[ConversationRecord]: ...

    async def get_conversation(self, *, conversation_id: str, profile_id: str, user_id: str) -> ConversationRecord | None: ...

    async def get_conversation_detail(
        self,
        *,
        conversation_id: str,
        profile_id: str,
        user_id: str,
        message_limit: int | None = None,
    ) -> ConversationDetail | None: ...

    async def save_conversation(self, conversation: ConversationRecord) -> ConversationRecord: ...

    async def append_message(
        self,
        *,
        conversation_id: str,
        profile_id: str,
        user_id: str,
        role: str,
        kind: str = ConversationMessageKind.text.value,
        content: str = "",
        structured_payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        server_message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationMessageRecord: ...

    async def append_activity(
        self,
        *,
        conversation_id: str,
        profile_id: str,
        user_id: str,
        kind: str,
        label: str,
        detail: str | None = None,
        state: str | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        specialist_slug: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationActivityRecord | None: ...

    async def delete_conversation(self, *, conversation_id: str, profile_id: str, user_id: str) -> bool: ...

    async def aclose(self) -> None: ...


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._conversations: dict[str, ConversationRecord] = {}
        self._messages: dict[str, list[ConversationMessageRecord]] = {}
        self._activities: dict[str, list[ConversationActivityRecord]] = {}
        self._indexes: dict[tuple[str, str], list[str]] = {}

    async def create_conversation(
        self,
        *,
        profile_id: str,
        user_id: str,
        agent_slug: str,
        agent_name: str,
        agent_icon_url: str = "",
        runtime_agent_name: str,
        title: str = "",
        history_length: int = 10,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationRecord:
        now = _utc_now_iso()
        conversation = ConversationRecord(
            id=str(uuid4()),
            profile_id=profile_id,
            user_id=user_id,
            title=title.strip(),
            agent_slug=agent_slug,
            agent_name=agent_name,
            agent_icon_url=agent_icon_url,
            runtime_agent_name=runtime_agent_name,
            history_length=max(0, min(int(history_length), 100)),
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._conversations[conversation.id] = conversation
            self._messages[conversation.id] = []
            self._activities[conversation.id] = []
            self._indexes.setdefault((profile_id, user_id), []).insert(0, conversation.id)
        return conversation.model_copy(deep=True)

    async def list_conversations(
        self,
        *,
        profile_id: str,
        user_id: str,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[ConversationRecord]:
        async with self._lock:
            ids = list(self._indexes.get((profile_id, user_id), []))
            records = [self._conversations[item_id].model_copy(deep=True) for item_id in ids if item_id in self._conversations]
        if status:
            records = [record for record in records if record.status == status]
        if limit is not None and limit >= 0:
            records = records[:limit]
        return records

    async def get_conversation(self, *, conversation_id: str, profile_id: str, user_id: str) -> ConversationRecord | None:
        async with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None or conversation.profile_id != profile_id or conversation.user_id != user_id:
                return None
            return conversation.model_copy(deep=True)

    async def get_conversation_detail(
        self,
        *,
        conversation_id: str,
        profile_id: str,
        user_id: str,
        message_limit: int | None = None,
    ) -> ConversationDetail | None:
        async with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None or conversation.profile_id != profile_id or conversation.user_id != user_id:
                return None
            messages = [message.model_copy(deep=True) for message in self._messages.get(conversation_id, [])]
            activities = [item.model_copy(deep=True) for item in self._activities.get(conversation_id, [])]
        if message_limit is not None and message_limit >= 0:
            messages = messages[-message_limit:]
        return ConversationDetail(
            conversation=conversation.model_copy(deep=True),
            messages=messages,
            activities=activities[:40],
        )

    async def save_conversation(self, conversation: ConversationRecord) -> ConversationRecord:
        stored = conversation.model_copy(deep=True)
        stored.updated_at = _utc_now_iso()
        async with self._lock:
            if stored.id not in self._conversations:
                raise KeyError(stored.id)
            self._conversations[stored.id] = stored
            ids = self._indexes.get((stored.profile_id, stored.user_id), [])
            if stored.id in ids:
                ids.remove(stored.id)
            ids.insert(0, stored.id)
        return stored.model_copy(deep=True)

    async def append_message(
        self,
        *,
        conversation_id: str,
        profile_id: str,
        user_id: str,
        role: str,
        kind: str = ConversationMessageKind.text.value,
        content: str = "",
        structured_payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        server_message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationMessageRecord:
        now = _utc_now_iso()
        async with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None or conversation.profile_id != profile_id or conversation.user_id != user_id:
                raise KeyError(conversation_id)
            sequence = len(self._messages.setdefault(conversation_id, [])) + 1
            message = ConversationMessageRecord(
                id=str(uuid4()),
                conversation_id=conversation_id,
                sequence=sequence,
                role=role,
                kind=kind,
                content=content,
                structured_payload=dict(structured_payload or {}),
                task_id=task_id,
                context_id=context_id,
                server_message_id=server_message_id,
                metadata=dict(metadata or {}),
                created_at=now,
                updated_at=now,
            )
            self._messages[conversation_id].append(message)
            conversation.message_count = sequence
            conversation.last_message_preview = _preview_text(
                content=content,
                structured_payload=structured_payload,
                role=role,
            )
            conversation.last_message_at = now
            conversation.updated_at = now
            if task_id:
                conversation.last_task_id = task_id
            if context_id:
                conversation.last_context_id = context_id
            ids = self._indexes.get((profile_id, user_id), [])
            if conversation_id in ids:
                ids.remove(conversation_id)
            ids.insert(0, conversation_id)
        return message.model_copy(deep=True)

    async def append_activity(
        self,
        *,
        conversation_id: str,
        profile_id: str,
        user_id: str,
        kind: str,
        label: str,
        detail: str | None = None,
        state: str | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        specialist_slug: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationActivityRecord | None:
        normalized_label = label.strip()
        normalized_detail = (detail or "").strip() or None
        normalized_state = (state or "").strip() or None
        normalized_specialist = (specialist_slug or "").strip() or None
        now = _utc_now_iso()
        async with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None or conversation.profile_id != profile_id or conversation.user_id != user_id:
                raise KeyError(conversation_id)
            history = self._activities.setdefault(conversation_id, [])
            previous = history[0] if history else None
            if previous is not None:
                if (
                    previous.kind == kind
                    and previous.label == normalized_label
                    and (previous.detail or None) == normalized_detail
                    and (previous.state or None) == normalized_state
                    and (previous.specialist_slug or None) == normalized_specialist
                ):
                    return None
            activity = ConversationActivityRecord(
                id=str(uuid4()),
                conversation_id=conversation_id,
                kind=kind,
                label=normalized_label,
                detail=normalized_detail,
                state=normalized_state,
                task_id=(task_id or "").strip() or None,
                context_id=(context_id or "").strip() or None,
                specialist_slug=normalized_specialist,
                metadata=dict(metadata or {}),
                received_at=now,
            )
            history.insert(0, activity)
            del history[40:]
        return activity.model_copy(deep=True)

    async def delete_conversation(self, *, conversation_id: str, profile_id: str, user_id: str) -> bool:
        async with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None or conversation.profile_id != profile_id or conversation.user_id != user_id:
                return False
            self._conversations.pop(conversation_id, None)
            self._messages.pop(conversation_id, None)
            self._activities.pop(conversation_id, None)
            ids = self._indexes.get((profile_id, user_id), [])
            if conversation_id in ids:
                ids.remove(conversation_id)
        return True

    async def aclose(self) -> None:
        return None


def _require_redis() -> Any:
    try:
        import redis.asyncio as redis_async  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Redis conversation store requires the `redis` extra (e.g. `uv sync --extra redis`)."
        ) from exc
    return redis_async


@dataclass(slots=True)
class RedisConversationStoreConfig:
    url: str = "redis://localhost:6379/0"
    namespace: str = "ka2a"


class RedisConversationStore:
    def __init__(self, *, redis: Any, config: RedisConversationStoreConfig | None = None) -> None:
        self._redis = redis
        self._cfg = config or RedisConversationStoreConfig()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RedisConversationStore":
        import os

        env_map = env or os.environ
        defaults = RedisConversationStoreConfig()
        cfg = RedisConversationStoreConfig(
            url=(env_map.get("KA2A_REDIS_URL") or defaults.url).strip(),
            namespace=(env_map.get("KA2A_REDIS_NAMESPACE") or defaults.namespace).strip(),
        )
        redis_async = _require_redis()
        client = redis_async.from_url(cfg.url, decode_responses=True)
        return cls(redis=client, config=cfg)

    def _conversation_key(self, conversation_id: str) -> str:
        return f"{self._cfg.namespace}:chat:conversation:{conversation_id}"

    def _conversation_messages_key(self, conversation_id: str) -> str:
        return f"{self._cfg.namespace}:chat:conversation:{conversation_id}:messages"

    def _conversation_activities_key(self, conversation_id: str) -> str:
        return f"{self._cfg.namespace}:chat:conversation:{conversation_id}:activities"

    def _conversation_seq_key(self, conversation_id: str) -> str:
        return f"{self._cfg.namespace}:chat:conversation:{conversation_id}:seq"

    def _conversation_index_key(self, profile_id: str, user_id: str) -> str:
        return f"{self._cfg.namespace}:chat:profile:{profile_id}:user:{user_id}:conversations"

    async def create_conversation(
        self,
        *,
        profile_id: str,
        user_id: str,
        agent_slug: str,
        agent_name: str,
        agent_icon_url: str = "",
        runtime_agent_name: str,
        title: str = "",
        history_length: int = 10,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationRecord:
        now = _utc_now_iso()
        conversation = ConversationRecord(
            id=str(uuid4()),
            profile_id=profile_id,
            user_id=user_id,
            title=title.strip(),
            agent_slug=agent_slug,
            agent_name=agent_name,
            agent_icon_url=agent_icon_url,
            runtime_agent_name=runtime_agent_name,
            history_length=max(0, min(int(history_length), 100)),
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        score = datetime.fromisoformat(now).timestamp()
        pipe = self._redis.pipeline(transaction=True)
        pipe.set(self._conversation_key(conversation.id), conversation.model_dump_json(by_alias=True, exclude_none=True))
        pipe.zadd(self._conversation_index_key(profile_id, user_id), {conversation.id: score})
        pipe.set(self._conversation_seq_key(conversation.id), 0)
        await pipe.execute()
        return conversation

    async def list_conversations(
        self,
        *,
        profile_id: str,
        user_id: str,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[ConversationRecord]:
        index_key = self._conversation_index_key(profile_id, user_id)
        if limit is not None and limit > 0:
            conversation_ids = await self._redis.zrevrange(index_key, 0, limit - 1)
        else:
            conversation_ids = await self._redis.zrevrange(index_key, 0, -1)
        if not conversation_ids:
            return []
        raws = await self._redis.mget([self._conversation_key(item_id) for item_id in conversation_ids])
        conversations: list[ConversationRecord] = []
        for raw in raws:
            if raw is None:
                continue
            try:
                conversation = ConversationRecord.model_validate_json(raw)
            except Exception:
                continue
            if status and conversation.status != status:
                continue
            conversations.append(conversation)
        return conversations

    async def get_conversation(self, *, conversation_id: str, profile_id: str, user_id: str) -> ConversationRecord | None:
        raw = await self._redis.get(self._conversation_key(conversation_id))
        if raw is None:
            return None
        try:
            conversation = ConversationRecord.model_validate_json(raw)
        except Exception:
            return None
        if conversation.profile_id != profile_id or conversation.user_id != user_id:
            return None
        return conversation

    async def get_conversation_detail(
        self,
        *,
        conversation_id: str,
        profile_id: str,
        user_id: str,
        message_limit: int | None = None,
    ) -> ConversationDetail | None:
        conversation = await self.get_conversation(conversation_id=conversation_id, profile_id=profile_id, user_id=user_id)
        if conversation is None:
            return None
        if message_limit is not None and message_limit >= 0:
            raws = await self._redis.lrange(self._conversation_messages_key(conversation_id), -message_limit, -1)
        else:
            raws = await self._redis.lrange(self._conversation_messages_key(conversation_id), 0, -1)
        activity_raws = await self._redis.lrange(self._conversation_activities_key(conversation_id), 0, 39)
        messages: list[ConversationMessageRecord] = []
        for raw in raws:
            try:
                messages.append(ConversationMessageRecord.model_validate_json(raw))
            except Exception:
                continue
        activities: list[ConversationActivityRecord] = []
        for raw in activity_raws:
            try:
                activities.append(ConversationActivityRecord.model_validate_json(raw))
            except Exception:
                continue
        return ConversationDetail(conversation=conversation, messages=messages, activities=activities)

    async def save_conversation(self, conversation: ConversationRecord) -> ConversationRecord:
        conversation = conversation.model_copy(deep=True)
        conversation.updated_at = _utc_now_iso()
        score = datetime.fromisoformat(conversation.updated_at).timestamp()
        pipe = self._redis.pipeline(transaction=True)
        pipe.set(self._conversation_key(conversation.id), conversation.model_dump_json(by_alias=True, exclude_none=True))
        pipe.zadd(self._conversation_index_key(conversation.profile_id, conversation.user_id), {conversation.id: score})
        await pipe.execute()
        return conversation

    async def append_message(
        self,
        *,
        conversation_id: str,
        profile_id: str,
        user_id: str,
        role: str,
        kind: str = ConversationMessageKind.text.value,
        content: str = "",
        structured_payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        server_message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationMessageRecord:
        conversation = await self.get_conversation(conversation_id=conversation_id, profile_id=profile_id, user_id=user_id)
        if conversation is None:
            raise KeyError(conversation_id)

        now = _utc_now_iso()
        sequence = int(await self._redis.incr(self._conversation_seq_key(conversation_id)))
        message = ConversationMessageRecord(
            id=str(uuid4()),
            conversation_id=conversation_id,
            sequence=sequence,
            role=role,
            kind=kind,
            content=content,
            structured_payload=dict(structured_payload or {}),
            task_id=task_id,
            context_id=context_id,
            server_message_id=server_message_id,
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )

        conversation.message_count = sequence
        conversation.last_message_preview = _preview_text(
            content=content,
            structured_payload=structured_payload,
            role=role,
        )
        conversation.last_message_at = now
        conversation.updated_at = now
        if task_id:
            conversation.last_task_id = task_id
        if context_id:
            conversation.last_context_id = context_id

        score = datetime.fromisoformat(now).timestamp()
        pipe = self._redis.pipeline(transaction=True)
        pipe.rpush(self._conversation_messages_key(conversation_id), message.model_dump_json(by_alias=True, exclude_none=True))
        pipe.set(self._conversation_key(conversation_id), conversation.model_dump_json(by_alias=True, exclude_none=True))
        pipe.zadd(self._conversation_index_key(profile_id, user_id), {conversation_id: score})
        await pipe.execute()
        return message

    async def append_activity(
        self,
        *,
        conversation_id: str,
        profile_id: str,
        user_id: str,
        kind: str,
        label: str,
        detail: str | None = None,
        state: str | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        specialist_slug: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationActivityRecord | None:
        conversation = await self.get_conversation(conversation_id=conversation_id, profile_id=profile_id, user_id=user_id)
        if conversation is None:
            raise KeyError(conversation_id)
        normalized_label = label.strip()
        normalized_detail = (detail or "").strip() or None
        normalized_state = (state or "").strip() or None
        normalized_specialist = (specialist_slug or "").strip() or None
        previous_raw = await self._redis.lindex(self._conversation_activities_key(conversation_id), 0)
        if previous_raw:
            try:
                previous = ConversationActivityRecord.model_validate_json(previous_raw)
            except Exception:
                previous = None
            if previous is not None:
                if (
                    previous.kind == kind
                    and previous.label == normalized_label
                    and (previous.detail or None) == normalized_detail
                    and (previous.state or None) == normalized_state
                    and (previous.specialist_slug or None) == normalized_specialist
                ):
                    return None
        activity = ConversationActivityRecord(
            id=str(uuid4()),
            conversation_id=conversation_id,
            kind=kind,
            label=normalized_label,
            detail=normalized_detail,
            state=normalized_state,
            task_id=(task_id or "").strip() or None,
            context_id=(context_id or "").strip() or None,
            specialist_slug=normalized_specialist,
            metadata=dict(metadata or {}),
            received_at=_utc_now_iso(),
        )
        pipe = self._redis.pipeline(transaction=True)
        pipe.lpush(self._conversation_activities_key(conversation_id), activity.model_dump_json(by_alias=True, exclude_none=True))
        pipe.ltrim(self._conversation_activities_key(conversation_id), 0, 39)
        await pipe.execute()
        return activity

    async def delete_conversation(self, *, conversation_id: str, profile_id: str, user_id: str) -> bool:
        conversation = await self.get_conversation(conversation_id=conversation_id, profile_id=profile_id, user_id=user_id)
        if conversation is None:
            return False
        pipe = self._redis.pipeline(transaction=True)
        pipe.delete(self._conversation_key(conversation_id))
        pipe.delete(self._conversation_messages_key(conversation_id))
        pipe.delete(self._conversation_activities_key(conversation_id))
        pipe.delete(self._conversation_seq_key(conversation_id))
        pipe.zrem(self._conversation_index_key(profile_id, user_id), conversation_id)
        await pipe.execute()
        return True

    async def aclose(self) -> None:
        try:
            close = getattr(self._redis, "close", None)
            if close is not None:
                res = close()
                if asyncio.iscoroutine(res):
                    await res
            pool = getattr(self._redis, "connection_pool", None)
            if pool is not None:
                disconnect = getattr(pool, "disconnect", None)
                if disconnect is not None:
                    res = disconnect()
                    if asyncio.iscoroutine(res):
                        await res
        except Exception:
            return None


def build_conversation_store(settings: A2AAppSettings, env: dict[str, str] | None = None) -> ConversationStore:
    import os

    env_map = env or os.environ
    kind = (env_map.get("KA2A_CHAT_STORE") or env_map.get("KA2A_TASK_STORE") or "").strip().lower()
    if kind in {"redis"}:
        return RedisConversationStore.from_env(env_map)
    if kind in {"memory"}:
        return InMemoryConversationStore()
    if kind in {"database", "db"}:
        if not settings.database_url:
            raise RuntimeError("KA2A_CHAT_STORE=database requires DATABASE_URL or KA2A_DATABASE_URL.")
        from .db import DatabaseConversationStore

        return DatabaseConversationStore(settings.database_url)
    if settings.database_url:
        from .db import DatabaseConversationStore

        return DatabaseConversationStore(settings.database_url)
    return InMemoryConversationStore()
