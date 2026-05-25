from __future__ import annotations

from kafka_a2a.mainapps.chat.db import DatabaseConversationStore
from kafka_a2a.mainapps.chat.models import ConversationMessageRole


async def _build_detail(store: DatabaseConversationStore, conversation_id: str) -> tuple[object, object]:
    conversation = await store.get_conversation(conversation_id=conversation_id, profile_id="1", user_id="7")
    detail = await store.get_conversation_detail(conversation_id=conversation_id, profile_id="1", user_id="7")
    return conversation, detail


def test_database_chat_store_persists_conversations_and_messages(tmp_path) -> None:
    import asyncio

    store = DatabaseConversationStore(f"sqlite:///{tmp_path / 'chat.sqlite3'}")

    async def scenario() -> None:
        conversation = await store.create_conversation(
            profile_id="1",
            user_id="7",
            agent_slug="host",
            agent_name="Host",
            runtime_agent_name="runtime.profile-1.host",
            title="Ops review",
            history_length=14,
        )

        message = await store.append_message(
            conversation_id=conversation.id,
            profile_id="1",
            user_id="7",
            role=ConversationMessageRole.user.value,
            content="Hello there",
        )
        activity = await store.append_activity(
            conversation_id=conversation.id,
            profile_id="1",
            user_id="7",
            kind="user-message",
            label="User message submitted",
            detail="Hello there",
            state="working",
        )
        duplicate = await store.append_activity(
            conversation_id=conversation.id,
            profile_id="1",
            user_id="7",
            kind="user-message",
            label="User message submitted",
            detail="Hello there",
            state="working",
        )

        loaded_conversation, detail = await _build_detail(store, conversation.id)
        assert loaded_conversation is not None
        assert detail is not None
        assert loaded_conversation.title == "Ops review"
        assert loaded_conversation.message_count == 1
        assert loaded_conversation.last_message_preview == "Hello there"
        assert detail.messages[0].id == message.id
        assert detail.messages[0].sequence == 1
        assert activity is not None
        assert duplicate is None
        assert detail.activities[0].id == activity.id
        assert detail.activities[0].kind == "user-message"
        assert detail.activities[0].state == "working"

        deleted = await store.delete_conversation(conversation_id=conversation.id, profile_id="1", user_id="7")
        assert deleted is True
        assert await store.get_conversation(conversation_id=conversation.id, profile_id="1", user_id="7") is None

        await store.aclose()

    asyncio.run(scenario())
