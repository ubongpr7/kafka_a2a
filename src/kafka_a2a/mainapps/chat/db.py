from __future__ import annotations

import asyncio
from datetime import datetime
from threading import RLock
from typing import Any
from uuid import uuid4

from .models import (
    ConversationActivityRecord,
    ConversationDetail,
    ConversationMessageKind,
    ConversationMessageRecord,
    ConversationRecord,
)
from .storage import _preview_text, _utc_now_iso


class DatabaseConversationStore:
    def __init__(self, database_url: str) -> None:
        self._database_url = self._normalize_database_url(database_url)
        self._engine = None
        self._metadata = None
        self._lock = RLock()

    @staticmethod
    def _normalize_database_url(value: str) -> str:
        normalized = value.strip()
        if normalized.startswith("postgresql://"):
            return normalized.replace("postgresql://", "postgresql+psycopg://", 1)
        return normalized

    def _ensure_runtime(self) -> None:
        if self._engine is not None and self._metadata is not None:
            return
        try:
            import sqlalchemy as sa
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("SQLAlchemy is required for DATABASE_URL-backed A2A chat storage.") from exc

        metadata = sa.MetaData()
        sa.Table(
            "a2a_chat_conversations",
            metadata,
            sa.Column("id", sa.String(length=80), primary_key=True),
            sa.Column("profile_id", sa.String(length=80), nullable=False, index=True),
            sa.Column("user_id", sa.String(length=80), nullable=False, index=True),
            sa.Column("status", sa.String(length=40), nullable=False, index=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.UniqueConstraint("id", name="uq_a2a_chat_conversations_id"),
        )
        sa.Table(
            "a2a_chat_messages",
            metadata,
            sa.Column("id", sa.String(length=80), primary_key=True),
            sa.Column("conversation_id", sa.String(length=80), nullable=False, index=True),
            sa.Column("profile_id", sa.String(length=80), nullable=False, index=True),
            sa.Column("user_id", sa.String(length=80), nullable=False, index=True),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.UniqueConstraint("conversation_id", "sequence", name="uq_a2a_chat_messages_conversation_sequence"),
        )
        sa.Table(
            "a2a_chat_activities",
            metadata,
            sa.Column("id", sa.String(length=80), primary_key=True),
            sa.Column("conversation_id", sa.String(length=80), nullable=False, index=True),
            sa.Column("profile_id", sa.String(length=80), nullable=False, index=True),
            sa.Column("user_id", sa.String(length=80), nullable=False, index=True),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("payload", sa.JSON(), nullable=False),
        )
        engine = sa.create_engine(self._database_url, future=True, pool_pre_ping=True)
        metadata.create_all(engine)
        self._engine = engine
        self._metadata = metadata

    @property
    def _conversations_table(self) -> Any:
        assert self._metadata is not None
        return self._metadata.tables["a2a_chat_conversations"]

    @property
    def _messages_table(self) -> Any:
        assert self._metadata is not None
        return self._metadata.tables["a2a_chat_messages"]

    @property
    def _activities_table(self) -> Any:
        assert self._metadata is not None
        return self._metadata.tables["a2a_chat_activities"]

    @staticmethod
    def _parse_dt(value: str | None) -> datetime:
        return datetime.fromisoformat(value or _utc_now_iso())

    @staticmethod
    def _conversation_from_row(row: Any) -> ConversationRecord:
        return ConversationRecord.model_validate(row.payload or {})

    @staticmethod
    def _message_from_row(row: Any) -> ConversationMessageRecord:
        return ConversationMessageRecord.model_validate(row.payload or {})

    @staticmethod
    def _activity_from_row(row: Any) -> ConversationActivityRecord:
        return ConversationActivityRecord.model_validate(row.payload or {})

    def _conversation_row(self, conversation: ConversationRecord) -> dict[str, Any]:
        payload = conversation.model_dump(mode="json")
        return {
            "id": conversation.id,
            "profile_id": conversation.profile_id,
            "user_id": conversation.user_id,
            "status": conversation.status,
            "updated_at": self._parse_dt(conversation.updated_at),
            "payload": payload,
        }

    def _message_row(self, message: ConversationMessageRecord, *, profile_id: str, user_id: str) -> dict[str, Any]:
        payload = message.model_dump(mode="json")
        return {
            "id": message.id,
            "conversation_id": message.conversation_id,
            "profile_id": profile_id,
            "user_id": user_id,
            "sequence": message.sequence,
            "created_at": self._parse_dt(message.created_at),
            "payload": payload,
        }

    def _activity_row(self, activity: ConversationActivityRecord, *, profile_id: str, user_id: str) -> dict[str, Any]:
        payload = activity.model_dump(mode="json")
        return {
            "id": activity.id,
            "conversation_id": activity.conversation_id,
            "profile_id": profile_id,
            "user_id": user_id,
            "received_at": self._parse_dt(activity.received_at),
            "payload": payload,
        }

    async def _run_blocking(self, fn):
        return await asyncio.to_thread(fn)

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
        def _op() -> ConversationRecord:
            self._ensure_runtime()
            import sqlalchemy as sa

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
            row = self._conversation_row(conversation)
            with self._lock:
                assert self._engine is not None
                with self._engine.begin() as conn:
                    conn.execute(sa.insert(self._conversations_table).values(**row))
            return conversation.model_copy(deep=True)

        return await self._run_blocking(_op)

    async def list_conversations(
        self,
        *,
        profile_id: str,
        user_id: str,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[ConversationRecord]:
        def _op() -> list[ConversationRecord]:
            self._ensure_runtime()
            import sqlalchemy as sa

            query = (
                sa.select(self._conversations_table.c.payload)
                .where(self._conversations_table.c.profile_id == profile_id)
                .where(self._conversations_table.c.user_id == user_id)
                .order_by(self._conversations_table.c.updated_at.desc(), self._conversations_table.c.id.desc())
            )
            if status:
                query = query.where(self._conversations_table.c.status == status)
            if limit is not None and limit >= 0:
                query = query.limit(limit)
            with self._lock:
                assert self._engine is not None
                with self._engine.begin() as conn:
                    rows = conn.execute(query).all()
            return [ConversationRecord.model_validate(row[0] or {}) for row in rows]

        return await self._run_blocking(_op)

    async def get_conversation(self, *, conversation_id: str, profile_id: str, user_id: str) -> ConversationRecord | None:
        def _op() -> ConversationRecord | None:
            self._ensure_runtime()
            import sqlalchemy as sa

            query = (
                sa.select(self._conversations_table.c.payload)
                .where(self._conversations_table.c.id == conversation_id)
                .where(self._conversations_table.c.profile_id == profile_id)
                .where(self._conversations_table.c.user_id == user_id)
            )
            with self._lock:
                assert self._engine is not None
                with self._engine.begin() as conn:
                    row = conn.execute(query).first()
            return None if row is None else ConversationRecord.model_validate(row[0] or {})

        return await self._run_blocking(_op)

    async def get_conversation_detail(
        self,
        *,
        conversation_id: str,
        profile_id: str,
        user_id: str,
        message_limit: int | None = None,
    ) -> ConversationDetail | None:
        def _op() -> ConversationDetail | None:
            self._ensure_runtime()
            import sqlalchemy as sa

            conversation_query = (
                sa.select(self._conversations_table.c.payload)
                .where(self._conversations_table.c.id == conversation_id)
                .where(self._conversations_table.c.profile_id == profile_id)
                .where(self._conversations_table.c.user_id == user_id)
            )
            with self._lock:
                assert self._engine is not None
                with self._engine.begin() as conn:
                    conversation_row = conn.execute(conversation_query).first()
                    if conversation_row is None:
                        return None
                    conversation = ConversationRecord.model_validate(conversation_row[0] or {})

                    query = (
                        sa.select(self._messages_table.c.payload)
                        .where(self._messages_table.c.conversation_id == conversation_id)
                        .where(self._messages_table.c.profile_id == profile_id)
                        .where(self._messages_table.c.user_id == user_id)
                        .order_by(self._messages_table.c.sequence.asc())
                    )
                    if message_limit is not None and message_limit >= 0:
                        recent = (
                            sa.select(self._messages_table.c.payload)
                            .where(self._messages_table.c.conversation_id == conversation_id)
                            .where(self._messages_table.c.profile_id == profile_id)
                            .where(self._messages_table.c.user_id == user_id)
                            .order_by(self._messages_table.c.sequence.desc())
                            .limit(message_limit)
                            .subquery()
                        )
                        query = sa.select(recent.c.payload)
                    rows = conn.execute(query).all()
                    activity_query = (
                        sa.select(self._activities_table.c.payload)
                        .where(self._activities_table.c.conversation_id == conversation_id)
                        .where(self._activities_table.c.profile_id == profile_id)
                        .where(self._activities_table.c.user_id == user_id)
                        .order_by(self._activities_table.c.received_at.desc(), self._activities_table.c.id.desc())
                        .limit(40)
                    )
                    activity_rows = conn.execute(activity_query).all()
            messages = [ConversationMessageRecord.model_validate(row[0] or {}) for row in rows]
            activities = [ConversationActivityRecord.model_validate(row[0] or {}) for row in activity_rows]
            if message_limit is not None and message_limit >= 0:
                messages.sort(key=lambda item: item.sequence)
            return ConversationDetail(conversation=conversation, messages=messages, activities=activities)

        return await self._run_blocking(_op)

    async def save_conversation(self, conversation: ConversationRecord) -> ConversationRecord:
        def _op() -> ConversationRecord:
            self._ensure_runtime()
            import sqlalchemy as sa

            stored = conversation.model_copy(deep=True)
            stored.updated_at = _utc_now_iso()
            row = self._conversation_row(stored)
            with self._lock:
                assert self._engine is not None
                with self._engine.begin() as conn:
                    result = conn.execute(
                        sa.update(self._conversations_table)
                        .where(self._conversations_table.c.id == stored.id)
                        .values(**row)
                    )
                    if not result.rowcount:
                        raise KeyError(stored.id)
            return stored.model_copy(deep=True)

        return await self._run_blocking(_op)

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
        def _op() -> ConversationMessageRecord:
            self._ensure_runtime()
            import sqlalchemy as sa

            now = _utc_now_iso()
            with self._lock:
                assert self._engine is not None
                with self._engine.begin() as conn:
                    row = conn.execute(
                        sa.select(self._conversations_table.c.payload)
                        .where(self._conversations_table.c.id == conversation_id)
                        .where(self._conversations_table.c.profile_id == profile_id)
                        .where(self._conversations_table.c.user_id == user_id)
                    ).first()
                    if row is None:
                        raise KeyError(conversation_id)
                    conversation = ConversationRecord.model_validate(row[0] or {})

                    next_sequence = int(
                        conn.execute(
                            sa.select(sa.func.coalesce(sa.func.max(self._messages_table.c.sequence), 0))
                            .where(self._messages_table.c.conversation_id == conversation_id)
                            .where(self._messages_table.c.profile_id == profile_id)
                            .where(self._messages_table.c.user_id == user_id)
                        ).scalar_one()
                    ) + 1

                    message = ConversationMessageRecord(
                        id=str(uuid4()),
                        conversation_id=conversation_id,
                        sequence=next_sequence,
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
                    conversation.message_count = next_sequence
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

                    conn.execute(sa.insert(self._messages_table).values(**self._message_row(message, profile_id=profile_id, user_id=user_id)))
                    conn.execute(
                        sa.update(self._conversations_table)
                        .where(self._conversations_table.c.id == conversation_id)
                        .values(**self._conversation_row(conversation))
                    )
            return message.model_copy(deep=True)

        return await self._run_blocking(_op)

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
        def _op() -> ConversationActivityRecord | None:
            self._ensure_runtime()
            import sqlalchemy as sa

            normalized_label = label.strip()
            normalized_detail = (detail or "").strip() or None
            normalized_state = (state or "").strip() or None
            normalized_specialist = (specialist_slug or "").strip() or None
            now = _utc_now_iso()
            with self._lock:
                assert self._engine is not None
                with self._engine.begin() as conn:
                    row = conn.execute(
                        sa.select(self._conversations_table.c.id)
                        .where(self._conversations_table.c.id == conversation_id)
                        .where(self._conversations_table.c.profile_id == profile_id)
                        .where(self._conversations_table.c.user_id == user_id)
                    ).first()
                    if row is None:
                        raise KeyError(conversation_id)
                    previous_row = conn.execute(
                        sa.select(self._activities_table.c.payload)
                        .where(self._activities_table.c.conversation_id == conversation_id)
                        .where(self._activities_table.c.profile_id == profile_id)
                        .where(self._activities_table.c.user_id == user_id)
                        .order_by(self._activities_table.c.received_at.desc(), self._activities_table.c.id.desc())
                        .limit(1)
                    ).first()
                    previous = None if previous_row is None else ConversationActivityRecord.model_validate(previous_row[0] or {})
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
                    conn.execute(sa.insert(self._activities_table).values(**self._activity_row(activity, profile_id=profile_id, user_id=user_id)))
                    stale_ids = [
                        item[0]
                        for item in conn.execute(
                            sa.select(self._activities_table.c.id)
                            .where(self._activities_table.c.conversation_id == conversation_id)
                            .where(self._activities_table.c.profile_id == profile_id)
                            .where(self._activities_table.c.user_id == user_id)
                            .order_by(self._activities_table.c.received_at.desc(), self._activities_table.c.id.desc())
                            .offset(40)
                        ).all()
                    ]
                    if stale_ids:
                        conn.execute(sa.delete(self._activities_table).where(self._activities_table.c.id.in_(stale_ids)))
            return activity.model_copy(deep=True)

        return await self._run_blocking(_op)

    async def delete_conversation(self, *, conversation_id: str, profile_id: str, user_id: str) -> bool:
        def _op() -> bool:
            self._ensure_runtime()
            import sqlalchemy as sa

            with self._lock:
                assert self._engine is not None
                with self._engine.begin() as conn:
                    deleted_messages = conn.execute(
                        sa.delete(self._messages_table)
                        .where(self._messages_table.c.conversation_id == conversation_id)
                        .where(self._messages_table.c.profile_id == profile_id)
                        .where(self._messages_table.c.user_id == user_id)
                    )
                    conn.execute(
                        sa.delete(self._activities_table)
                        .where(self._activities_table.c.conversation_id == conversation_id)
                        .where(self._activities_table.c.profile_id == profile_id)
                        .where(self._activities_table.c.user_id == user_id)
                    )
                    deleted_conversation = conn.execute(
                        sa.delete(self._conversations_table)
                        .where(self._conversations_table.c.id == conversation_id)
                        .where(self._conversations_table.c.profile_id == profile_id)
                        .where(self._conversations_table.c.user_id == user_id)
                    )
            return bool((deleted_conversation.rowcount or 0) or (deleted_messages.rowcount or 0))

        return await self._run_blocking(_op)

    async def aclose(self) -> None:
        def _op() -> None:
            with self._lock:
                if self._engine is not None:
                    self._engine.dispose()

        await self._run_blocking(_op)
