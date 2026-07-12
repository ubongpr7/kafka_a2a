from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "mcp-tools.prod.json"
TARGET = ROOT / "mcp-tools.local.json"

LOCAL_SERVER_URLS = {
    "users": "http://users_mcp:8000/mcp/",
    "products": "http://product_mcp:8000/mcp/",
    "inventory": "http://inventory_mcp:8000/mcp/",
    "pos": "http://pos_mcp:8000/mcp/",
    "audit": "http://audit_mcp:8000/mcp/",
    "notifications": "http://notifications_mcp:8000/mcp/",
    "subscriptions": "http://subscriptions_mcp:8000/mcp/",
    "purchasing": "http://inventory_mcp:8000/mcp/",
}


def main() -> None:
    config = json.loads(SOURCE.read_text(encoding="utf-8"))
    shared_servers = config.get("sharedServers") or []

    for server in shared_servers:
        server_id = server.get("id")
        if server_id in LOCAL_SERVER_URLS:
            server["serverUrl"] = LOCAL_SERVER_URLS[server_id]

    TARGET.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
