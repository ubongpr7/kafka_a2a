from __future__ import annotations

import asyncio

import pytest

from kafka_a2a.models import Artifact, Message, TaskState, TaskStatus, TextPart
from kafka_a2a.runtime.task_store import InMemoryTaskStore, extract_task_scope_metadata
from kafka_a2a.models import PushNotificationAuthenticationInfo, PushNotificationConfig


@pytest.mark.asyncio
async def test_task_store_records_and_fanout() -> None:
    store = InMemoryTaskStore()
    initial = Message(role="user", parts=[TextPart(text="hi")])
    task = await store.create_task(initial_message=initial)

    queue, history = await store.subscribe(task.id)
    assert history[0].sequence == 0

    await store.append_status(task_id=task.id, status=TaskStatus(state=TaskState.working))
    record = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert record.sequence == 1

    await store.append_artifact(task_id=task.id, artifact=Artifact(name="a", parts=[TextPart(text="x")]))
    record2 = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert record2.sequence == 2

    await store.unsubscribe(task.id, queue)


@pytest.mark.asyncio
async def test_task_store_preserves_context_id_and_owns_task_id() -> None:
    store = InMemoryTaskStore()
    initial = Message(role="user", parts=[TextPart(text="hi")], task_id="client", context_id="ctx1")
    task = await store.create_task(initial_message=initial)

    assert task.context_id == "ctx1"
    assert initial.context_id == "ctx1"

    assert task.id != "client"
    assert initial.task_id == task.id


@pytest.mark.asyncio
async def test_task_store_lists_tasks_by_context() -> None:
    store = InMemoryTaskStore()
    ctx = "ctx-a"
    t1 = await store.create_task(initial_message=Message(role="user", parts=[TextPart(text="1")], context_id=ctx))
    t2 = await store.create_task(initial_message=Message(role="user", parts=[TextPart(text="2")], context_id=ctx))
    t3 = await store.create_task(initial_message=Message(role="user", parts=[TextPart(text="3")], context_id=ctx))

    tasks = await store.list_tasks_by_context(ctx)
    assert [t.id for t in tasks] == [t1.id, t2.id, t3.id]

    tasks2 = await store.list_tasks_by_context(ctx, limit=2)
    assert [t.id for t in tasks2] == [t2.id, t3.id]


@pytest.mark.asyncio
async def test_task_store_push_notification_configs_crud() -> None:
    store = InMemoryTaskStore()
    initial = Message(role="user", parts=[TextPart(text="hi")])
    task = await store.create_task(initial_message=initial)

    cfg = PushNotificationConfig(
        id="c1",
        url="https://example.invalid/cb",
        token="t",
        authentication=PushNotificationAuthenticationInfo(schemes=["Bearer"], credentials="x"),
    )
    saved = await store.set_push_notification_config(task_id=task.id, config=cfg)
    assert saved.task_id == task.id
    assert saved.push_notification_config.id == "c1"

    got = await store.get_push_notification_config(task_id=task.id, config_id="c1")
    assert got is not None
    assert got.push_notification_config.url == "https://example.invalid/cb"

    all_cfgs = await store.list_push_notification_configs(task_id=task.id)
    assert len(all_cfgs) == 1

    await store.delete_push_notification_config(task_id=task.id, config_id="c1")
    assert await store.get_push_notification_config(task_id=task.id, config_id="c1") is None


def test_extract_task_scope_metadata_filters_non_scope_values() -> None:
    metadata = {
        "scope_mode": "terminal_location",
        "structural_location_id": "struct-1",
        "structural_location_ids": ["struct-1", "struct-2"],
        "stock_location_id": "leaf-7",
        "sync_key": "sync-123",
        "entity_ids": ["inventory_item_id:item-4"],
        "trace_id": "trace-9",
        "profile_id": "1",
    }

    assert extract_task_scope_metadata(metadata) == {
        "scope_mode": "terminal_location",
        "structural_location_id": "struct-1",
        "structural_location_ids": ["struct-1", "struct-2"],
        "stock_location_id": "leaf-7",
        "sync_key": "sync-123",
        "entity_ids": ["inventory_item_id:item-4"],
    }


@pytest.mark.asyncio
async def test_task_store_persists_scope_metadata_on_status_and_artifact_events() -> None:
    store = InMemoryTaskStore()
    initial = Message(role="user", parts=[TextPart(text="hi")])
    task = await store.create_task(
        initial_message=initial,
        metadata={
            "scope_mode": "terminal_location",
            "structural_location_id": "struct-1",
            "stock_location_id": "leaf-3",
            "sync_key": "sync-abc",
            "entity_ids": ["inventory_item_id:item-9"],
            "trace_id": "trace-ignore",
        },
    )

    status_event = await store.append_status(task_id=task.id, status=TaskStatus(state=TaskState.working))
    artifact_event = await store.append_artifact(
        task_id=task.id,
        artifact=Artifact(name="receipt", parts=[TextPart(text="ok")]),
    )

    expected_scope = {
        "scope_mode": "terminal_location",
        "structural_location_id": "struct-1",
        "stock_location_id": "leaf-3",
        "sync_key": "sync-abc",
        "entity_ids": ["inventory_item_id:item-9"],
    }

    assert status_event.metadata == expected_scope
    assert artifact_event.metadata == expected_scope

    records = []
    async for record in store.iter_events(task.id, replay_history=True):
        records.append(record)
        if len(records) == 3:
            break

    assert records[0].event.kind == "task"
    assert records[0].event.metadata["structural_location_id"] == "struct-1"
    assert records[1].event.metadata == expected_scope
    assert records[2].event.metadata == expected_scope
