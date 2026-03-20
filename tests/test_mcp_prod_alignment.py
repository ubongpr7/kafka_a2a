from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[2]
KA2A_ROOT = ROOT / "kafka_a2a"

SERVICE_ENV_FILES = {
    "products": {
        "example_env_path": ROOT / "product_service" / ".env.example",
        "prod_env_path": ROOT / "product_service" / ".env.prod",
        "host_var": "PRODUCT_MCP_ALLOWED_HOSTS",
        "origin_var": "PRODUCT_MCP_ALLOWED_ORIGINS",
    },
    "inventory": {
        "example_env_path": ROOT / "intera_inventory" / ".env.example",
        "prod_env_path": ROOT / "intera_inventory" / ".env.prod",
        "host_var": "INVENTORY_MCP_ALLOWED_HOSTS",
        "origin_var": "INVENTORY_MCP_ALLOWED_ORIGINS",
    },
    "users": {
        "example_env_path": ROOT / "intera_users" / ".env.example",
        "prod_env_path": ROOT / "intera_users" / ".env.prod",
        "host_var": "USERS_MCP_ALLOWED_HOSTS",
        "origin_var": "USERS_MCP_ALLOWED_ORIGINS",
    },
    "pos": {
        "example_env_path": ROOT / "pos_backend_service" / ".env.example",
        "prod_env_path": ROOT / "pos_backend_service" / ".env.prod",
        "host_var": "POS_MCP_ALLOWED_HOSTS",
        "origin_var": "POS_MCP_ALLOWED_ORIGINS",
    },
}


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _split_csv(raw: str | None) -> list[str]:
    if raw is None:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _assert_mcp_host_alignment(*, env_path: Path, host_var: str, origin_var: str, server_id: str, server_url: str) -> None:
    host = urlparse(server_url).hostname
    assert host, f"Missing hostname in MCP server URL for {server_id}: {server_url}"

    env = _parse_env_file(env_path)
    allowed_hosts = _split_csv(env.get(host_var) or env.get("ALLOWED_HOSTS"))
    allowed_origins = _split_csv(env.get(origin_var) or env.get("CORS_ALLOWED_ORIGINS"))
    expected_origin = f"https://{host}"

    assert host in allowed_hosts, (
        f"{env_path} does not allow the MCP host '{host}' "
        f"via {host_var} or ALLOWED_HOSTS."
    )
    assert expected_origin in allowed_origins, (
        f"{env_path} does not allow the MCP origin '{expected_origin}' "
        f"via {origin_var} or CORS_ALLOWED_ORIGINS."
    )


def test_versioned_service_examples_allow_their_mcp_hosts() -> None:
    config = json.loads((KA2A_ROOT / "mcp-tools.prod.json").read_text(encoding="utf-8"))
    shared_servers = {
        str(item["id"]): item
        for item in config.get("sharedServers", [])
    }

    for server_id, service in SERVICE_ENV_FILES.items():
        server_url = str(shared_servers[server_id]["serverUrl"])
        _assert_mcp_host_alignment(
            env_path=service["example_env_path"],
            host_var=service["host_var"],
            origin_var=service["origin_var"],
            server_id=server_id,
            server_url=server_url,
        )


def test_local_prod_envs_allow_their_mcp_hosts_when_present() -> None:
    config = json.loads((KA2A_ROOT / "mcp-tools.prod.json").read_text(encoding="utf-8"))
    shared_servers = {
        str(item["id"]): item
        for item in config.get("sharedServers", [])
    }

    for server_id, service in SERVICE_ENV_FILES.items():
        env_path = service["prod_env_path"]
        if not env_path.exists():
            continue
        _assert_mcp_host_alignment(
            env_path=env_path,
            host_var=service["host_var"],
            origin_var=service["origin_var"],
            server_id=server_id,
            server_url=str(shared_servers[server_id]["serverUrl"]),
        )
