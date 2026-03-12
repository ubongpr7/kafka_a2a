from __future__ import annotations

import os

from kafka_a2a.server.gateway import GatewayConfig, create_gateway_app


def _optional_timeout_s(*values: str | None) -> float | None:
    for value in values:
        if value is None:
            continue
        raw = value.strip()
        if not raw:
            continue
        timeout_s = float(raw)
        if timeout_s <= 0:
            return None
        return timeout_s
    return None


app = create_gateway_app(
    GatewayConfig(
        bootstrap_servers=os.getenv("KA2A_BOOTSTRAP_SERVERS", "localhost:9092"),
        default_agent=os.getenv("KA2A_DEFAULT_AGENT", "echo"),
        client_id=os.getenv("KA2A_GATEWAY_CLIENT_ID"),
        request_timeout_s=_optional_timeout_s(
            os.getenv("KA2A_GATEWAY_REQUEST_TIMEOUT_S"),
            os.getenv("KA2A_REQUEST_TIMEOUT_S"),
        ),
    )
)
