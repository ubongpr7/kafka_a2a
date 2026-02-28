from __future__ import annotations

import asyncio

import pytest

from kafka_a2a.models import Artifact, Message, TaskState, TaskStatus, TextPart
from kafka_a2a.runtime.task_store import InMemoryTaskStore
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
