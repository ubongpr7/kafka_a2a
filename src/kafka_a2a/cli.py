from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import signal
import socket
from pathlib import Path
from typing import Any, Callable

from kafka_a2a.models import AgentCard
from kafka_a2a.processors import TaskProcessor, echo_processor, make_prompted_echo_processor
from kafka_a2a.registry.kafka_registry import KafkaAgentRegistry
from kafka_a2a.runtime.agent import Ka2aAgent, Ka2aAgentConfig
from kafka_a2a.server.a2a_http import A2AHttpProxyConfig, create_a2a_http_proxy_app
from kafka_a2a.server.auth import JwtBearerConfig
from kafka_a2a.server.gateway import GatewayConfig, create_gateway_app
from kafka_a2a.transport.kafka import KafkaAgentRegistryConfig, KafkaConfig, KafkaDlqConfig, KafkaTransport, TopicNamer


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    value = value.strip().lower()
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off"):
        return False
    return default


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_optional_timeout_s(*values: str | None) -> float | None:
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


def _read_text_file(path: str | None) -> str | None:
    if not path:
        return None
    return Path(path).read_text(encoding="utf-8")


def _load_agent_card(path: str) -> AgentCard:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return AgentCard.model_validate(data)


def _resolve_processor(value: str | None) -> TaskProcessor:
    name = (value or os.getenv("KA2A_AGENT_PROCESSOR") or "echo").strip()
    if name in ("echo", "echo_processor"):
        return echo_processor
    if name in ("prompted-echo", "prompted_echo", "prompted_echo_processor"):
        system_prompt = os.getenv("KA2A_SYSTEM_PROMPT") or os.getenv("KA2A_AGENT_SYSTEM_PROMPT")
        return make_prompted_echo_processor(system_prompt=system_prompt)
    if name in ("langgraph-chat", "langgraph_chat", "langgraph"):
        from kafka_a2a.langgraph_processor import make_langgraph_chat_processor_from_env

        return make_langgraph_chat_processor_from_env()
    if name in ("router", "host-router", "router-agent", "router_agent"):
        from kafka_a2a.router_processor import make_router_processor_from_env

        return make_router_processor_from_env()
    if name in ("weather", "weather-agent", "weather_agent"):
        from kafka_a2a.specialized_agents import make_weather_agent_processor_from_env

        return make_weather_agent_processor_from_env()
    if name in ("sports", "sports-agent", "sports_agent", "sports-journalist", "sports_journalist"):
        from kafka_a2a.specialized_agents import make_sports_journalist_agent_processor_from_env

        return make_sports_journalist_agent_processor_from_env()
    if name in ("finance", "finance-agent", "finance_agent", "financial", "financial-analysis", "financial_analysis"):
        from kafka_a2a.specialized_agents import make_financial_analysis_agent_processor_from_env

        return make_financial_analysis_agent_processor_from_env()

    if ":" in name:
        module_name, attr = name.split(":", 1)
        mod = importlib.import_module(module_name)
        proc = getattr(mod, attr, None)
        if proc is None:
            raise SystemExit(f"Processor not found: {name}")
        if not callable(proc):
            raise SystemExit(f"Processor is not callable: {name}")
        return proc  # type: ignore[return-value]

    raise SystemExit(
        "Unknown processor. Use one of: echo, prompted-echo, langgraph-chat, or an import path like 'pkg.module:callable'."
    )


def _jwt_from_env(prefix: str = "KA2A_JWT_") -> JwtBearerConfig | None:
    enabled = _parse_bool(os.getenv(f"{prefix}ENABLED"), default=False)
    key = os.getenv(f"{prefix}KEY")
    key_path = os.getenv(f"{prefix}KEY_PATH")
    jwks_url = os.getenv(f"{prefix}JWKS_URL")

    if not enabled and not key and not key_path and not jwks_url:
        return None

    secret = _read_text_file(key_path) if key_path else key
    if not (secret or jwks_url):
        raise SystemExit(
            f"{prefix}KEY/{prefix}KEY_PATH or {prefix}JWKS_URL is required when JWT auth is enabled"
        )

    algs = _split_csv(os.getenv(f"{prefix}ALGORITHMS")) or ["HS256"]

    include_claims_env = os.getenv(f"{prefix}INCLUDE_CLAIMS")
    if include_claims_env is None:
        # Default to including claims when credentials come from JWT (SaaS mode).
        creds_source = (
            os.getenv("KA2A_LLM_CREDENTIALS_SOURCE") or os.getenv("KA2A_CREDENTIALS_SOURCE") or "env"
        ).strip()
        include_claims = creds_source.lower() != "env"
    else:
        include_claims = _parse_bool(include_claims_env, default=False)

    jwks_headers_raw = os.getenv(f"{prefix}JWKS_HEADERS") or ""
    jwks_headers: dict[str, str] | None = None
    if jwks_headers_raw.strip():
        try:
            jwks_headers_obj = json.loads(jwks_headers_raw)
            if isinstance(jwks_headers_obj, dict):
                jwks_headers = {str(k): str(v) for k, v in jwks_headers_obj.items()}
        except Exception:
            jwks_headers = None

    return JwtBearerConfig(
        secret=secret or "",
        algorithms=algs,
        audience=os.getenv(f"{prefix}AUDIENCE") or None,
        issuer=os.getenv(f"{prefix}ISSUER") or None,
        leeway_s=int(os.getenv(f"{prefix}LEEWAY_S") or "0"),
        user_claim=os.getenv(f"{prefix}USER_CLAIM") or "sub",
        tenant_claim=os.getenv(f"{prefix}TENANT_CLAIM") or None,
        forward_bearer_token=_parse_bool(os.getenv(f"{prefix}FORWARD_BEARER_TOKEN"), default=False),
        include_claims=include_claims,
        jwks_url=(jwks_url or "").strip() or None,
        jwks_cache_lifespan_s=float(os.getenv(f"{prefix}JWKS_CACHE_LIFESPAN_S") or "300"),
        jwks_timeout_s=float(os.getenv(f"{prefix}JWKS_TIMEOUT_S") or "30"),
        jwks_headers=jwks_headers,
    )


async def _run_agent(args: argparse.Namespace) -> None:
    bootstrap = args.bootstrap_servers or os.getenv("KA2A_BOOTSTRAP_SERVERS", "localhost:9092")

    card: AgentCard | None = None
    card_path = args.agent_card_path or os.getenv("KA2A_AGENT_CARD_PATH")
    if card_path:
        card = _load_agent_card(card_path)

    name = args.agent_name or os.getenv("KA2A_AGENT_NAME") or (card.name if card and card.name else "")
    if not name:
        prefix = os.getenv("KA2A_AGENT_NAME_PREFIX", "")
        name = f"{prefix}{socket.gethostname()}"

    transport = KafkaTransport(KafkaConfig.from_env(bootstrap_servers=bootstrap, client_id=f"ka2a-agent-{name}"))
    registry = KafkaAgentRegistry(transport=transport, sender=name)

    push_notifications = (
        args.push_notifications
        if args.push_notifications is not None
        else _parse_bool(os.getenv("KA2A_AGENT_PUSH_NOTIFICATIONS"), default=False)
    )
    creds_source = (os.getenv("KA2A_LLM_CREDENTIALS_SOURCE") or os.getenv("KA2A_CREDENTIALS_SOURCE") or "env").strip()
    default_tenant_isolation = creds_source.lower() != "env"
    tenant_isolation = (
        args.tenant_isolation
        if args.tenant_isolation is not None
        else _parse_bool(os.getenv("KA2A_TENANT_ISOLATION"), default=default_tenant_isolation)
    )
    require_tenant_match = (
        args.require_tenant_match
        if args.require_tenant_match is not None
        else _parse_bool(os.getenv("KA2A_REQUIRE_TENANT_MATCH"), default=True)
    )
    store_principal_secrets = (
        args.store_principal_secrets
        if args.store_principal_secrets is not None
        else _parse_bool(os.getenv("KA2A_STORE_PRINCIPAL_SECRETS"), default=False)
    )

    cfg = Ka2aAgentConfig(
        agent_name=name,
        description=args.description or os.getenv("KA2A_AGENT_DESCRIPTION"),
        url=args.url or os.getenv("KA2A_AGENT_URL"),
        version=args.version or os.getenv("KA2A_AGENT_VERSION", "0.1.0"),
        push_notifications=push_notifications,
        push_delivery_timeout_s=float(os.getenv("KA2A_PUSH_DELIVERY_TIMEOUT_S") or "5.0"),
        push_queue_maxsize=int(os.getenv("KA2A_PUSH_QUEUE_MAXSIZE") or "1000"),
        tenant_isolation=tenant_isolation,
        require_tenant_match=require_tenant_match,
        principal_metadata_key=os.getenv("KA2A_PRINCIPAL_METADATA_KEY") or "urn:ka2a:principal",
        store_principal_secrets=store_principal_secrets,
        registry_heartbeat_s=(
            float(os.getenv("KA2A_REGISTRY_HEARTBEAT_S")) if os.getenv("KA2A_REGISTRY_HEARTBEAT_S") else 60.0
        ),
        request_group_id=os.getenv("KA2A_AGENT_REQUEST_GROUP_ID") or None,
        max_concurrency=int(os.getenv("KA2A_MAX_CONCURRENCY") or "50"),
        context_history_turns=int(os.getenv("KA2A_CONTEXT_HISTORY_TURNS") or "20"),
    )

    processor = _resolve_processor(args.processor)

    if card is not None and card.name and card.name != cfg.agent_name:
        # Prefer the card name (it defines the A2A addressable identity).
        cfg.agent_name = card.name

    task_store = None
    store_kind = (args.task_store or os.getenv("KA2A_TASK_STORE") or "memory").strip().lower()
    if store_kind not in ("memory", "redis"):
        raise SystemExit("Invalid task store. Use: memory | redis (via --task-store or KA2A_TASK_STORE).")
    if store_kind == "redis":
        from kafka_a2a.runtime.redis_task_store import RedisTaskStore

        task_store = RedisTaskStore.from_env()

    agent = Ka2aAgent(
        config=cfg,
        transport=transport,
        registry=registry,
        processor=processor,
        card=card,
        task_store=task_store,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass

    await agent.start()
    try:
        await stop.wait()
    finally:
        await agent.stop()


def _require_uvicorn() -> Any:
    try:
        import uvicorn  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise SystemExit("Server extras not installed. Install the `server` extra (e.g. `uv sync --extra server`).") from exc
    return uvicorn


async def _ensure_topics(args: argparse.Namespace) -> None:
    bootstrap = args.bootstrap_servers or os.getenv("KA2A_BOOTSTRAP_SERVERS", "localhost:9092")
    partitions = int(args.partitions or os.getenv("KA2A_KAFKA_TOPIC_PARTITIONS") or "1")
    repl = int(args.replication_factor or os.getenv("KA2A_KAFKA_TOPIC_REPLICATION_FACTOR") or "1")

    agents = _split_csv(args.agents or os.getenv("KA2A_AGENT_NAMES"))
    client_ids = _split_csv(args.client_ids or os.getenv("KA2A_CLIENT_IDS"))

    # Common service IDs that should use stable reply topics when Kafka auto-create is disabled.
    for var in ("KA2A_GATEWAY_CLIENT_ID", "KA2A_PROXY_CLIENT_ID", "KA2A_ROUTER_CLIENT_ID"):
        value = (os.getenv(var) or "").strip()
        if value:
            client_ids.append(value)

    topics = TopicNamer.from_env()
    registry_cfg = KafkaAgentRegistryConfig.from_env()
    dlq_cfg = KafkaDlqConfig.from_env()

    topic_names: set[str] = {registry_cfg.topic}
    for name in agents:
        topic_names.add(topics.agent_requests(name))
    for cid in client_ids:
        topic_names.add(topics.client_replies(cid))
    if dlq_cfg.enabled:
        topic_names.add(dlq_cfg.topic)

    if not topic_names:
        raise SystemExit("No topics to create.")

    try:
        from aiokafka.admin import AIOKafkaAdminClient, NewTopic  # type: ignore
        from aiokafka.errors import TopicAlreadyExistsError  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise SystemExit("aiokafka admin client is required (aiokafka>=0.11.0).") from exc

    admin_cfg = KafkaConfig.from_env(
        bootstrap_servers=bootstrap,
        client_id=os.getenv("KA2A_ADMIN_CLIENT_ID") or "ka2a-admin",
    )
    admin = AIOKafkaAdminClient(**admin_cfg.aiokafka_kwargs())
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        to_create = [
            NewTopic(
                name=t,
                num_partitions=partitions,
                replication_factor=repl,
                topic_configs={"cleanup.policy": "compact"} if t == registry_cfg.topic else None,
            )
            for t in sorted(topic_names)
            if t not in existing
        ]
        if not to_create:
            print("All topics already exist.")
            return
        try:
            await admin.create_topics(new_topics=to_create, validate_only=False)
        except TopicAlreadyExistsError:
            # Race: topic created by another process.
            pass
        print("Ensured topics:")
        for t in sorted(topic_names):
            print(f" - {t}")
    finally:
        await admin.close()


def _run_gateway(args: argparse.Namespace) -> None:
    bootstrap = args.bootstrap_servers or os.getenv("KA2A_BOOTSTRAP_SERVERS", "localhost:9092")
    default_agent = args.default_agent or os.getenv("KA2A_DEFAULT_AGENT", "echo")
    host = args.host or os.getenv("KA2A_GATEWAY_HOST", "0.0.0.0")
    port = int(args.port or os.getenv("KA2A_GATEWAY_PORT", "8000"))

    jwt = _jwt_from_env()

    app = create_gateway_app(
        GatewayConfig(
            bootstrap_servers=bootstrap,
            default_agent=default_agent,
            client_id=os.getenv("KA2A_GATEWAY_CLIENT_ID"),
            request_timeout_s=_parse_optional_timeout_s(
                os.getenv("KA2A_GATEWAY_REQUEST_TIMEOUT_S"),
                os.getenv("KA2A_REQUEST_TIMEOUT_S"),
            ),
            jwt=jwt,
        )
    )

    uvicorn = _require_uvicorn()
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("KA2A_LOG_LEVEL", "info"))


def _run_proxy(args: argparse.Namespace) -> None:
    bootstrap = args.bootstrap_servers or os.getenv("KA2A_BOOTSTRAP_SERVERS", "localhost:9092")
    agent_name = args.agent_name or os.getenv("KA2A_AGENT_NAME", "echo")
    host = args.host or os.getenv("KA2A_PROXY_HOST", "0.0.0.0")
    port = int(args.port or os.getenv("KA2A_PROXY_PORT", "8001"))

    jwt = _jwt_from_env()

    app = create_a2a_http_proxy_app(
        A2AHttpProxyConfig(
            bootstrap_servers=bootstrap,
            agent_name=agent_name,
            client_id=os.getenv("KA2A_PROXY_CLIENT_ID"),
            request_timeout_s=_parse_optional_timeout_s(
                os.getenv("KA2A_PROXY_REQUEST_TIMEOUT_S"),
                os.getenv("KA2A_REQUEST_TIMEOUT_S"),
            ),
            jwt=jwt,
        )
    )

    uvicorn = _require_uvicorn()
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("KA2A_LOG_LEVEL", "info"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ka2a", description="K-A2A (Kafka A2A) CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_agent = sub.add_parser("agent", help="Run an A2A agent (Kafka transport)")
    p_agent.add_argument("--bootstrap-servers", dest="bootstrap_servers")
    p_agent.add_argument("--agent-name", dest="agent_name")
    p_agent.add_argument("--description")
    p_agent.add_argument("--url")
    p_agent.add_argument("--version")
    p_agent.add_argument(
        "--push-notifications",
        dest="push_notifications",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable/disable tasks push notifications. Defaults to KA2A_AGENT_PUSH_NOTIFICATIONS.",
    )
    p_agent.add_argument(
        "--tenant-isolation",
        dest="tenant_isolation",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable/disable multi-tenant task isolation. Defaults to KA2A_TENANT_ISOLATION.",
    )
    p_agent.add_argument(
        "--require-tenant-match",
        dest="require_tenant_match",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Require tenant match for task access. Defaults to KA2A_REQUIRE_TENANT_MATCH (true).",
    )
    p_agent.add_argument(
        "--store-principal-secrets",
        dest="store_principal_secrets",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Persist bearer token/claims to the task store. Defaults to KA2A_STORE_PRINCIPAL_SECRETS (false).",
    )
    p_agent.add_argument(
        "--processor",
        help="echo | prompted-echo | langgraph-chat | import path (pkg.module:callable). Defaults to KA2A_AGENT_PROCESSOR or echo.",
    )
    p_agent.add_argument(
        "--task-store",
        dest="task_store",
        help="Task store backend: memory | redis. Defaults to KA2A_TASK_STORE or memory.",
    )
    p_agent.add_argument("--agent-card-path", help="Path to an AgentCard JSON file", dest="agent_card_path")
    p_agent.set_defaults(func=lambda ns: asyncio.run(_run_agent(ns)))

    p_gw = sub.add_parser("gateway", help="Run the FastAPI gateway (chat + upload + stream)")
    p_gw.add_argument("--bootstrap-servers", dest="bootstrap_servers")
    p_gw.add_argument("--default-agent", dest="default_agent")
    p_gw.add_argument("--host")
    p_gw.add_argument("--port")
    p_gw.set_defaults(func=_run_gateway)

    p_px = sub.add_parser("proxy", help="Run the A2A JSON-RPC over HTTP proxy")
    p_px.add_argument("--bootstrap-servers", dest="bootstrap_servers")
    p_px.add_argument("--agent-name", dest="agent_name")
    p_px.add_argument("--host")
    p_px.add_argument("--port")
    p_px.set_defaults(func=_run_proxy)

    p_et = sub.add_parser("ensure-topics", help="Ensure required Kafka topics exist (for clusters with auto-create disabled)")
    p_et.add_argument("--bootstrap-servers", dest="bootstrap_servers")
    p_et.add_argument("--agents", help="Comma-separated agent names to ensure request topics for (ka2a.req.<agent>).")
    p_et.add_argument("--client-ids", help="Comma-separated client IDs to ensure reply topics for (ka2a.reply.<clientId>).")
    p_et.add_argument("--partitions", help="Number of partitions for created topics (default 1).")
    p_et.add_argument("--replication-factor", dest="replication_factor", help="Replication factor for created topics (default 1).")
    p_et.set_defaults(func=lambda ns: asyncio.run(_ensure_topics(ns)))

    return parser


def main(argv: list[str] | None = None) -> None:
    # Local/dev convenience: if a `.env` file exists, load it (without overriding real env vars).
    try:  # pragma: no cover
        from kafka_a2a.settings import load_dotenv

        load_dotenv(".env", override=False)
    except Exception:
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    func: Callable[[argparse.Namespace], Any] = args.func
    func(args)


if __name__ == "__main__":  # pragma: no cover
    main()
