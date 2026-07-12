from __future__ import annotations

import pytest

from kafka_a2a.protocol import RpcResponse
from kafka_a2a.transport.kafka import KafkaEnvelope, _extract_envelope_scope_fields


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


def test_kafka_envelope_roundtrip_preserves_scope_metadata() -> None:
    env = KafkaEnvelope(
        type="request",
        correlation_id="c2",
        scope_mode="terminal_location",
        structural_location_id="struct-1",
        structural_location_ids=["struct-1", "struct-2"],
        stock_location_id="leaf-4",
        sync_key="op=inventory.adjust_inventory_item_stock|struct=struct-1|leaf=leaf-4|digest=abc123",
        entity_ids=["inventory_item_id:item-1", "adjustment_id:adj-1"],
        payload={"x": 2},
    )

    rebuilt = KafkaEnvelope.from_bytes(env.to_bytes())

    assert rebuilt.scope_mode == "terminal_location"
    assert rebuilt.structural_location_id == "struct-1"
    assert rebuilt.structural_location_ids == ["struct-1", "struct-2"]
    assert rebuilt.stock_location_id == "leaf-4"
    assert rebuilt.sync_key == "op=inventory.adjust_inventory_item_stock|struct=struct-1|leaf=leaf-4|digest=abc123"
    assert rebuilt.entity_ids == ["inventory_item_id:item-1", "adjustment_id:adj-1"]
    assert rebuilt.scope_fields() == {
        "scope_mode": "terminal_location",
        "structural_location_id": "struct-1",
        "structural_location_ids": ["struct-1", "struct-2"],
        "stock_location_id": "leaf-4",
        "sync_key": "op=inventory.adjust_inventory_item_stock|struct=struct-1|leaf=leaf-4|digest=abc123",
        "entity_ids": ["inventory_item_id:item-1", "adjustment_id:adj-1"],
    }


def test_extract_envelope_scope_fields_reads_request_metadata_payload() -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": "req-1",
        "method": "message/send",
        "params": {
            "message": {"kind": "message"},
            "metadata": {
                "scope_mode": "terminal_location",
                "primary_structural_location_id": "struct-4",
                "stock_location_id": "leaf-2",
                "sync_key": "sync-123",
                "entity_ids": ["inventory_item_id:item-9"],
            },
        },
    }

    assert _extract_envelope_scope_fields(payload) == {
        "scope_mode": "terminal_location",
        "structural_location_id": "struct-4",
        "stock_location_id": "leaf-2",
        "sync_key": "sync-123",
        "entity_ids": ["inventory_item_id:item-9"],
    }
