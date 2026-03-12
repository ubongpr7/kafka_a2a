from __future__ import annotations

import asyncio

import pytest

from kafka_a2a.client import Ka2aClient, Ka2aClientConfig
from kafka_a2a.protocol import RpcResponse


class _FakeTransport:
    def __init__(self, on_send=None):
        self._on_send = on_send

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, *, topic: str, envelope, key=None, headers=None) -> None:
        if self._on_send is not None:
            await self._on_send(topic=topic, envelope=envelope)


@pytest.mark.asyncio
async def test_call_without_timeout_waits_for_result() -> None:
    client: Ka2aClient | None = None

    async def _on_send(*, topic: str, envelope) -> None:
        assert client is not None
        loop = asyncio.get_running_loop()
        response = RpcResponse(id=str(envelope.correlation_id), result={"ok": True})
        loop.call_later(0.01, client._pending[str(envelope.correlation_id)].set_result, response)

    transport = _FakeTransport(on_send=_on_send)
    client = Ka2aClient(transport=transport, config=Ka2aClientConfig(client_id="test-client", request_timeout_s=None))

    result = await client.call(agent_name="echo", method="ping", params={"ok": True})

    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_call_with_timeout_still_times_out() -> None:
    client = Ka2aClient(transport=_FakeTransport(), config=Ka2aClientConfig(client_id="test-client", request_timeout_s=0.01))

    with pytest.raises(asyncio.TimeoutError):
        await client.call(agent_name="echo", method="ping", params={"ok": True})
