from __future__ import annotations

import asyncio

import pytest

from kafka_a2a.control_plane import ControlPlaneError
from kafka_a2a.runtime.shared_runtime import SharedRuntimeService, _workspace_instruction


def test_workspace_instruction_composes_all_saved_instruction_layers() -> None:
    prompt = _workspace_instruction(
        {
            "system_instruction": "Operate as the workspace host.",
            "special_instruction": "This is a provision store.",
            "assistant_instruction": "Use Naira and do not invent stock.",
        }
    )

    assert prompt == (
        "Operate as the workspace host.\n\n"
        "This is a provision store.\n\n"
        "Use Naira and do not invent stock."
    )


@pytest.mark.asyncio
async def test_shared_runtime_waits_for_initial_control_plane_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SharedRuntimeService(bootstrap_servers="localhost:9092", poll_interval_s=5.0)

    attempts = {"count": 0}

    async def fake_reconcile() -> None:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("gateway not ready")

    created_tasks: list[object] = []

    def fake_create_task(coro):
        created_tasks.append(coro)

        class _DummyTask:
            def cancel(self) -> None:
                return None

        return _DummyTask()

    service._control_plane._cfg.base_url = "http://ka2a_gateway:7006"
    monkeypatch.setattr(service, "_reconcile", fake_reconcile)
    monkeypatch.setattr("asyncio.create_task", fake_create_task)

    await service.start()

    assert attempts["count"] == 3
    assert len(created_tasks) == 1

    sync_coro = created_tasks[0]
    sync_coro.close()


@pytest.mark.asyncio
async def test_shared_runtime_reuses_last_registry_when_control_plane_refresh_times_out() -> None:
    service = SharedRuntimeService(bootstrap_servers="localhost:9092", poll_interval_s=5.0)
    service._control_plane._cfg.base_url = "http://ka2a_gateway:7006"

    calls = {"count": 0}

    def fake_registry() -> dict[str, object]:
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "agents": [
                    {
                        "runtime_name": "wa-p1-host-123",
                        "slug": "host",
                    }
                ]
            }
        raise ControlPlaneError("Control-plane request timed out after 30.0s.")

    service._control_plane.list_internal_runtime_registry = fake_registry  # type: ignore[method-assign]

    first = await service._load_registry()
    second = await service._load_registry()

    assert first == [{"runtime_name": "wa-p1-host-123", "slug": "host"}]
    assert second == first


@pytest.mark.asyncio
async def test_shared_runtime_starts_pending_workers_concurrently() -> None:
    service = SharedRuntimeService(bootstrap_servers="localhost:9092", poll_interval_s=5.0)
    worker_started = asyncio.Event()
    release_workers = asyncio.Event()
    started: list[str] = []

    async def fake_load_registry() -> list[dict[str, object]]:
        return [
            {"runtime_name": "agent-a", "runtime_card_payload": {}},
            {"runtime_name": "agent-b", "runtime_card_payload": {}},
            {"runtime_name": "agent-c", "runtime_card_payload": {}},
        ]

    async def fake_start_agent(*, runtime_name: str, fingerprint: str, agent_payload: dict[str, object]) -> None:
        _ = fingerprint, agent_payload
        started.append(runtime_name)
        if len(started) == 3:
            worker_started.set()
        await release_workers.wait()

    service._load_registry = fake_load_registry  # type: ignore[method-assign]
    service._start_agent = fake_start_agent  # type: ignore[method-assign]
    reconcile_task = asyncio.create_task(service._reconcile())

    await asyncio.wait_for(worker_started.wait(), timeout=0.5)
    release_workers.set()
    await reconcile_task

    assert set(started) == {"agent-a", "agent-b", "agent-c"}


@pytest.mark.asyncio
async def test_shared_runtime_provisions_workspace_request_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SharedRuntimeService(bootstrap_servers="kafka:9092", poll_interval_s=5.0)
    captured: dict[str, object] = {}

    async def fake_ensure_kafka_topics(**kwargs) -> list[str]:
        captured.update(kwargs)
        return ["inventory.ka2a.req.wa-p4-host-123"]

    monkeypatch.setenv("KA2A_TOPIC_NAMESPACE", "inventory")
    monkeypatch.setenv("KA2A_KAFKA_TOPIC_PARTITIONS", "3")
    monkeypatch.setenv("KA2A_KAFKA_TOPIC_REPLICATION_FACTOR", "1")
    monkeypatch.setattr("kafka_a2a.runtime.shared_runtime.ensure_kafka_topics", fake_ensure_kafka_topics)

    await service._ensure_agent_request_topic("wa-p4-host-123")

    config = captured["config"]
    assert config.bootstrap_servers == "kafka:9092"
    assert config.client_id == "ka2a-runtime-topic-wa-p4-host-123"
    assert captured["topic_names"] == ["inventory.ka2a.req.wa-p4-host-123"]
    assert captured["partitions"] == 3
    assert captured["replication_factor"] == 1
