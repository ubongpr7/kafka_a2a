from __future__ import annotations

import pytest

from kafka_a2a.control_plane import ControlPlaneError
from kafka_a2a.runtime.shared_runtime import SharedRuntimeService


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
