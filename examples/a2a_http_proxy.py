from __future__ import annotations

import os

from kafka_a2a.server.a2a_http import A2AHttpProxyConfig, create_a2a_http_proxy_app


app = create_a2a_http_proxy_app(
    A2AHttpProxyConfig(
        bootstrap_servers=os.getenv("KA2A_BOOTSTRAP_SERVERS", "localhost:9092"),
        agent_name=os.getenv("KA2A_AGENT_NAME", "echo"),
        client_id=os.getenv("KA2A_PROXY_CLIENT_ID"),
    )
)

