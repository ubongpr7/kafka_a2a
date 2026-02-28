from __future__ import annotations

import os

from kafka_a2a.server.gateway import GatewayConfig, create_gateway_app


app = create_gateway_app(
    GatewayConfig(
        bootstrap_servers=os.getenv("KA2A_BOOTSTRAP_SERVERS", "localhost:9092"),
        default_agent=os.getenv("KA2A_DEFAULT_AGENT", "echo"),
        client_id=os.getenv("KA2A_GATEWAY_CLIENT_ID"),
    )
)

