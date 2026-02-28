from __future__ import annotations

import pytest

from kafka_a2a.protocol import RpcResponse
from kafka_a2a.transport.kafka import KafkaEnvelope


def test_rpc_response_requires_result_xor_error() -> None:
    with pytest.raises(Exception):
        RpcResponse(id="1", result=None, error=None)
    with pytest.raises(Exception):
        RpcResponse(id="1", result={"ok": True}, error={"code": 1, "message": "x"})
    with pytest.raises(Exception):
        RpcResponse(id="1", error=None)

    ok = RpcResponse(id="1", result={"ok": True})
    assert ok.result == {"ok": True}

    null_ok = RpcResponse(id="1", result=None).to_jsonrpc_dict()
    assert null_ok["id"] == "1"
    assert "result" in null_ok and null_ok["result"] is None
    assert "error" not in null_ok

    err = RpcResponse(id="1", error={"code": 123, "message": "nope"}).to_jsonrpc_dict()
    assert err["id"] == "1"
    assert "error" in err and err["error"]["code"] == 123
    assert "result" not in err


def test_kafka_envelope_roundtrip() -> None:
    env = KafkaEnvelope(type="request", correlation_id="c1", payload={"x": 1})
    rebuilt = KafkaEnvelope.from_bytes(env.to_bytes())
    assert rebuilt.type.value == "request"
    assert rebuilt.correlation_id == "c1"
    assert rebuilt.payload["x"] == 1
